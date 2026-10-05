"""Read-only startup preflight for the backend's SQLite database (OPS-2).

``python -m chronos.cli db-preflight`` reports, without repairing anything, what the backend's
own startup would do with the configured database: the admission checks of
:mod:`chronos.persistence.database` (identity by ``lstat`` — symbolic link, non-regular file,
foreign owner, more than one name — then mode) and the ``Database.initialize()`` schema checks, read
through a WAL-aware read-only connection so a schema that a crashed writer committed only into
the write-ahead log is seen. Only on request it inspects an explicit platform ledger path with
the ledger's own rules (the backend never opens that ledger) and estimates the evidence-stream
verification window.

Exit codes: 0 CLEAR — none of the checks that ran would refuse; 78 REFUSE; 1 UNDECIDED — a
check could not run, or an unexpected error. Never 255. CLEAR is not a promise that the backend
will start: the verdict line names what was not checked. The command never chmods, unlinks,
creates a database, migrates or writes a row; the one mutation SQLite itself may perform is to
create or recreate the ``-shm``/``-wal`` sidecars of a WAL database for the read-only open.
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import sqlite3
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from chronos.execution import sqlite_ledger as _ledger
from chronos.persistence import database as _database

EXIT_CLEAR = 0
EXIT_UNDECIDED = 1
#: ``EX_CONFIG``: what refuses is the configured database, not this command.
EXIT_REFUSED = 78

_PLATFORM_SENTENCE = (
    "the platform process that opens this ledger would refuse; the backend does not open it"
)
_BACKEND_DOES_NOT_OPEN = "the backend does not open this ledger"
_SIDECAR_NOTE = (
    "SQLite may create or recreate -shm/-wal with the main file's mode for a read-only WAL "
    "open; they are the runtime's own sidecars"
)
_NOT_CHECKED = (
    "connect-time pragmas (the journal_mode/synchronous refusals in persistence/database.py), "
    "filesystem writes at create/mkdir, mandate/registry/recovery hold (campaign preflight), "
    "FU2 process state, another process holding the database, "
    "path stability between this check and the backend's open"
)
_OPERATIONS_POINTER = 'see docs/OPERATIONS.md "Database startup refusals"'
_DB_REPAIR = "Chronos will chmod 0600 at its first construction"
_LEDGER_REPAIR = "the platform process will chmod 0600 at its first construction"


@dataclass(frozen=True)
class Line:
    """One observation: ``CLEAR | WARN | REFUSE | UNDECIDED | INFO [scope] text``."""

    level: str
    scope: str
    text: str

    def __str__(self) -> str:
        return f"{self.level} [{self.scope}] {self.text}"


@dataclass
class Report:
    """Everything the preflight observed, in the order the runtime would meet it."""

    lines: list[Line] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    def add(self, level: str, scope: str, message: str) -> None:
        self.lines.append(Line(level, scope, message))

    def count(self, level: str, scopes: Iterable[str]) -> int:
        wanted = set(scopes)
        return sum(1 for line in self.lines if line.level == level and line.scope in wanted)

    def verdict(self) -> tuple[str, int]:
        backend_refusals = self.count("REFUSE", ("db", "schema"))
        ledger_refusals = self.count("REFUSE", ("ledger",))
        undecided = [line.scope for line in self.lines if line.level == "UNDECIDED"]
        if backend_refusals:
            tail = ""
            if ledger_refusals:
                tail = f"; the ledger diagnostic also refused ({ledger_refusals})"
            return (
                f"DB-PREFLIGHT REFUSED ({backend_refusals}) — the backend would refuse to "
                f"start; {_OPERATIONS_POINTER}{tail}",
                EXIT_REFUSED,
            )
        if ledger_refusals:
            return (
                "DB-PREFLIGHT REFUSED (ledger) — the platform process would refuse; "
                f"{_BACKEND_DOES_NOT_OPEN}",
                EXIT_REFUSED,
            )
        if undecided:
            return (
                f"DB-PREFLIGHT UNDECIDED ({len(undecided)}) — a check could not run; nothing "
                f"is known about: {', '.join(undecided)}",
                EXIT_UNDECIDED,
            )
        return (
            f"DB-PREFLIGHT CLEAR — checked: {', '.join(self.checked)}; NOT checked: {_NOT_CHECKED}",
            EXIT_CLEAR,
        )


def _parent_lines(report: Report, path: Path, scope: str, *, exists: bool, tail: str) -> bool:
    """What ``mkdir(parents=True, exist_ok=True)`` and the private create would meet."""

    parent = path.parent
    try:
        metadata = os.stat(parent)
    except FileNotFoundError:
        report.add("CLEAR", scope, f"parent directory absent: created at first start ({parent})")
        return True
    if not stat.S_ISDIR(metadata.st_mode):
        report.add("REFUSE", scope, f"parent is not a directory: {parent}{tail}")
        return False
    if not os.access(parent, os.W_OK):
        if not exists:
            report.add(
                "REFUSE",
                scope,
                f"parent directory is not writable and the file is absent, so the first start "
                f"cannot create it: {parent}{tail}",
            )
            return False
        report.add(
            "WARN",
            scope,
            f"parent directory is not writable ({parent}): SQLite cannot create -wal/-shm "
            "there, and a WAL database without its -shm cannot be opened",
        )
    return True


def _identity_lines(
    report: Report,
    namespace: tuple[Path, ...],
    scope: str,
    check: Callable[[Path], None],
    tail: str,
) -> bool:
    """The runtime's own identity check on every name of the namespace; ``lstat`` only."""

    ok = True
    for path in namespace:
        try:
            check(path)
        except RuntimeError as error:
            report.add("REFUSE", scope, f"{error}{tail}")
            ok = False
    if ok:
        _stray_lines(report, namespace[0], scope)
    return ok


def _stray_lines(report: Report, path: Path, scope: str) -> None:
    """A leftover private create temporary that does NOT share the inode is a warning only."""

    prefix = f".{path.name}.create-"
    try:
        entries = [entry.name for entry in os.scandir(path.parent) if entry.name.startswith(prefix)]
    except OSError:
        return
    for name in sorted(entries):
        report.add(
            "WARN",
            scope,
            f"a private temporary from an interrupted create is present and not linked to the "
            f"file: {path.parent / name}; Chronos never removes it",
        )


def _mode_lines(
    report: Report,
    namespace: tuple[Path, ...],
    scope: str,
    repair_sentence: str,
    unopenable: Callable[[Path], str],
) -> bool:
    """Predict the first construction's repair by ``lstat``: a file it can open read-only it
    chmods to 0600 (WARN); a file without the owner read bit it cannot even open (REFUSE)."""

    ok = True
    for path in namespace:
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            continue
        mode = stat.S_IMODE(metadata.st_mode)
        if mode == 0o600:
            continue
        if not mode & 0o400:
            report.add("REFUSE", scope, unopenable(path))
            ok = False
        else:
            report.add("WARN", scope, f"mode {mode:04o}: {repair_sentence} ({path})")
    return ok


def _read_only_uri(path: Path) -> str:
    return f"{path.as_uri()}?mode=ro"


def _read_only_engine(path: Path) -> Engine:
    uri = _read_only_uri(path)

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(uri, uri=True)

    return create_engine("sqlite://", creator=connect)


def _database_error(error: BaseException) -> str:
    """The effective exception class, qualified; never the error's text.

    Driver text can carry a DSN with a password; a class name cannot.
    """

    original = getattr(error, "orig", None)
    kind = type(original) if original is not None else type(error)
    return f"{kind.__module__}.{kind.__name__}"


_FILE_URI_PROBE = "sqlite:///file:probe"


def _file_uri_refusal() -> str:
    """The runtime's own static sentence for a ``file:`` URI, taken from the runtime at call
    time so this module never carries a copy; empty if the runtime no longer raises it."""

    try:
        _database._sqlite_database_path(_FILE_URI_PROBE)
    except ValueError as error:
        return str(error)
    return ""


def _schema_lines(report: Report, engine: Engine) -> None:
    """``Database.initialize()``'s decisions, read-only, composed to its exact messages."""

    guidance = _database._DATABASE_RECOVERY_GUIDANCE
    expected = _database.SCHEMA_VERSION
    with engine.connect() as connection:
        journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar()
        report.add("INFO", "schema", f"journal_mode={journal_mode}")
        inspector = inspect(connection)
        table_names = set(inspector.get_table_names())
        if "schema_version" not in table_names:
            if table_names:
                report.add(
                    "REFUSE",
                    "schema",
                    "Refusing to initialize an unversioned database containing existing tables: "
                    + ", ".join(sorted(table_names))
                    + ". "
                    + guidance,
                )
            else:
                report.add("CLEAR", "schema", "unversioned, empty: initialize() creates the schema")
            return
        version_columns = {
            str(column["name"]) for column in inspector.get_columns("schema_version")
        }
        if not {"id", "version"} <= version_columns:
            report.add(
                "REFUSE",
                "schema",
                "Chronos schema_version does not contain readable id and version columns. "
                + guidance,
            )
            return
        current = connection.execute(
            text("SELECT version FROM schema_version ORDER BY id DESC LIMIT 1")
        ).scalar()
    if current is None:
        report.add(
            "REFUSE",
            "schema",
            "Chronos schema_version exists without a version record. " + guidance,
        )
        return
    if current != expected:
        report.add(
            "REFUSE",
            "schema",
            f"Unsupported Chronos schema version {current}; expected {expected}. " + guidance,
        )
        return
    drift = _database._schema_drift(engine)
    if drift:
        report.add(
            "REFUSE",
            "schema",
            f"Chronos schema version {expected} does not match the required schema: "
            + "; ".join(drift)
            + ". "
            + guidance,
        )
        return
    report.add("CLEAR", "schema", f"version {expected}, no drift")


def _evidence_lines(report: Report, engine: Engine, settings: Any) -> None:
    """A lower bound on the FU2 verification window; never a refusal."""

    if not settings.autonomy_evidence_bundles:
        report.add("INFO", "evidence", "AUTONOMY_EVIDENCE_BUNDLES is off: the pass does not run")
        return
    rows_per_tick = int(settings.autonomy_evidence_pass_rows_per_tick)
    bytes_per_tick = int(settings.autonomy_evidence_pass_bytes_per_tick)
    with engine.connect() as connection:
        if "hash_chain_records" not in set(inspect(connection).get_table_names()):
            report.add("INFO", "evidence", "no hash_chain_records table: nothing to verify yet")
            return
        rows = connection.execute(
            text(
                "SELECT stream, COUNT(*), COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))), 0) "
                "FROM hash_chain_records WHERE stream LIKE 'autonomy.evidence:%' "
                "GROUP BY stream ORDER BY stream"
            )
        ).all()
    if not rows:
        report.add("INFO", "evidence", "no autonomy.evidence stream recorded yet")
        return
    for stream, count, total in rows:
        ticks = max(math.ceil(int(count) / rows_per_tick), math.ceil(int(total) / bytes_per_tick))
        report.add(
            "INFO",
            "evidence",
            f"{stream}: {count} rows, {total} bytes; the first pass needs at least {ticks} "
            f"ticks (rows/tick {rows_per_tick}, bytes/tick {bytes_per_tick}) — an estimate, "
            "never exact: ordered whole-row packing can need more; evidence-bound proposals "
            "refuse until then; a restart re-enters this window",
        )


def _database_lines(report: Report, database_url: str, *, evidence: bool, settings: Any) -> None:
    try:
        configured = _database._sqlite_database_path(database_url)
    except ValueError as error:
        # Database() itself refuses this value at construction. The runtime's static sentence
        # for a `file:` URI is passed through verbatim only when it IS that sentence; any
        # other ValueError comes from the URL parser, whose text can echo part of the value.
        refusal = str(error)
        if refusal != _file_uri_refusal():
            refusal = (
                "configured DATABASE_URL is not accepted by the runtime's URL parser "
                f"({type(error).__name__})"
            )
        report.add("REFUSE", "db", refusal)
        return
    if configured is None:
        report.add(
            "UNDECIDED",
            "db",
            "configured DATABASE_URL is not a file-backed SQLite database; file "
            "identity, mode and schema are not checked",
        )
        return
    path = Path(os.path.abspath(configured))
    namespace = (path, *(Path(f"{path}{suffix}") for suffix in _database._SQLITE_SIDECAR_SUFFIXES))
    exists = os.path.lexists(path)
    if not _parent_lines(report, path, "db", exists=exists, tail=""):
        return
    if not _identity_lines(report, namespace, "db", _database._check_sqlite_file_identity, ""):
        return
    report.checked.append("identity")
    if not _mode_lines(
        report,
        namespace,
        "db",
        _DB_REPAIR,
        lambda name: f"Unable to secure SQLite path without following links: {name}",
    ):
        return
    report.checked.append("mode")
    if not exists:
        report.add("CLEAR", "schema", "absent: created at first start")
        report.checked.append("schema")
        if evidence:
            report.add("INFO", "evidence", "no database yet: nothing to verify")
            report.checked.append("evidence")
        return
    wal_size = _sidecar_size(path.with_name(path.name + "-wal"))
    if wal_size:
        report.add(
            "INFO",
            "schema",
            f"un-checkpointed WAL: {wal_size} bytes; the backend recovers it at its first connect",
        )
    report.add("INFO", "schema", _SIDECAR_NOTE)
    engine = _read_only_engine(path)
    try:
        try:
            _schema_lines(report, engine)
        except (SQLAlchemyError, sqlite3.Error) as error:
            report.add(
                "UNDECIDED",
                "schema",
                f"cannot read the database read-only ({_database_error(error)})",
            )
            return
        report.checked.append("schema")
        if evidence:
            _evidence_lines(report, engine, settings)
            report.checked.append("evidence")
    finally:
        engine.dispose()


def _sidecar_size(path: Path) -> int:
    try:
        return os.lstat(path).st_size
    except FileNotFoundError:
        return 0


def _ledger_lines(report: Report, raw: str) -> None:
    """The ledger's own admission and schema rule, opt-in; its refusals are the platform's."""

    path = Path(os.path.abspath(raw))
    namespace = _ledger._ledger_namespace(path)
    exists = os.path.lexists(path)
    tail = f" — {_PLATFORM_SENTENCE}"
    report.checked.append("ledger (--ledger)")
    if not _parent_lines(report, path, "ledger", exists=exists, tail=tail):
        return
    if not _identity_lines(report, namespace, "ledger", _ledger._check_ledger_file_identity, tail):
        return
    denied = os.strerror(errno.EACCES)
    if not _mode_lines(
        report,
        namespace,
        "ledger",
        _LEDGER_REPAIR,
        lambda name: (
            f"Refusing ledger path {name}: repair failed ({denied}); chmod 600 it and restart{tail}"
        ),
    ):
        return
    if not exists:
        report.add(
            "CLEAR",
            "ledger",
            f"absent: created at the platform process's first construction ({path}); "
            f"{_BACKEND_DOES_NOT_OPEN}",
        )
        return
    report.add("INFO", "ledger", _SIDECAR_NOTE)
    try:
        connection = sqlite3.connect(_read_only_uri(path), uri=True)
    except sqlite3.Error as error:
        report.add(
            "UNDECIDED",
            "ledger",
            f"cannot read the ledger read-only ({_database_error(error)}){tail}",
        )
        return
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        row = (
            connection.execute("SELECT version FROM schema_info").fetchone()
            if "schema_info" in tables
            else None
        )
    except sqlite3.Error as error:
        report.add(
            "UNDECIDED",
            "ledger",
            f"cannot read the ledger read-only ({_database_error(error)}){tail}",
        )
        return
    finally:
        connection.close()
    expected = _ledger._SCHEMA_VERSION
    if row is None:
        report.add(
            "CLEAR",
            "ledger",
            f"no schema_info version yet: written at the platform process's first construction "
            f"({path}); {_BACKEND_DOES_NOT_OPEN}",
        )
    elif row[0] != expected:
        report.add(
            "REFUSE",
            "ledger",
            f"ledger schema version {row[0]} unsupported (expected {expected}); refusing to run "
            f"against an unknown schema{tail}",
        )
    else:
        report.add(
            "CLEAR", "ledger", f"schema version {expected} ({path}); {_BACKEND_DOES_NOT_OPEN}"
        )


def run_preflight(
    report: Report,
    *,
    database_url: str,
    ledger: str | None,
    evidence: bool,
    settings: Any,
) -> None:
    _database_lines(report, database_url, evidence=evidence, settings=settings)
    if ledger is not None:
        _ledger_lines(report, ledger)


def cmd_db_preflight(args: argparse.Namespace) -> int:
    """Observe; never repair. Every exception is one UNDECIDED line and exit 1.

    The unit runs this command on its private DATABASE_URL and the journal keeps the output,
    so the configured URL is never printed and every caught error — here and in the inner
    catches — is reported by its class alone: exception text can carry credentials (a
    query-parameter password, a DSN, a fragment the URL parser echoes) and no pattern can
    promise to remove them. The one exception text passed through is the runtime's own
    static sentence for a ``file:`` URI, compared verbatim.
    """

    report = Report()
    try:
        from chronos.config.settings import get_settings

        settings = get_settings()
        database_url = args.database_url or settings.database_url
        run_preflight(
            report,
            database_url=database_url,
            ledger=args.ledger,
            evidence=args.evidence,
            settings=settings,
        )
        verdict, code = report.verdict()
    except Exception as error:  # one line, exit 1, never a traceback
        verdict = f"DB-PREFLIGHT UNDECIDED (1) — {type(error).__name__}"
        code = EXIT_UNDECIDED
    if args.json:
        print(
            json.dumps(
                {
                    "lines": [
                        {"level": line.level, "scope": line.scope, "text": line.text}
                        for line in report.lines
                    ],
                    "checked": report.checked,
                    "verdict": verdict,
                    "exit": code,
                },
                indent=2,
            )
        )
        return code
    print("Chronos db-preflight (read-only: observes what startup would do, never repairs)")
    for line in report.lines:
        print(line)
    print(verdict)
    return code


def add_db_preflight_command(sub: Any) -> None:
    """Register ``db-preflight`` on the operator CLI."""

    parser = sub.add_parser(
        "db-preflight",
        help=(
            "report what the backend's database startup checks would do (read-only; "
            "exit 0 CLEAR, 78 REFUSE, 1 UNDECIDED)"
        ),
    )
    parser.add_argument(
        "--database-url", default=None, help="defaults to DATABASE_URL from settings"
    )
    parser.add_argument(
        "--ledger",
        default=None,
        metavar="PATH",
        help="also inspect this platform ledger (opt-in; the backend does not open it)",
    )
    parser.add_argument(
        "--evidence",
        action="store_true",
        help="estimate the evidence-stream verification window (informational)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.set_defaults(func=cmd_db_preflight)
