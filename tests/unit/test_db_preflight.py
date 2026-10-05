"""OPS-2: the backend's startup preflight, ``python -m chronos.cli db-preflight``.

Design: ``run-20260913-pm/OPS-2r2-design-note.md`` (reviewed by Daybreak and kimi). The
command is read-only and refuse-only. Its CLEAR means exactly "none of the checks it executed
would refuse"; the verdict line enumerates what it did not check. Each test below names what
it proves, and every refusal text is taken from the runtime's own functions or from a fresh
process constructing the real ``Database``/``SqliteLedger`` on the same fixture (the oracle),
never from a copy of the message.
"""

from __future__ import annotations

import configparser
import importlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from chronos.cli.main import main
from chronos.config.settings import get_settings
from chronos.execution import sqlite_ledger as ledger_module
from chronos.execution.sqlite_ledger import SqliteLedger
from chronos.persistence import database as database_module
from chronos.persistence.database import SCHEMA_VERSION, Database

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "docs" / "ops" / "chronos-backend.service"
EXIT_REFUSED = 78
EXIT_UNDECIDED = 1
BACKEND_VERDICT = "the backend would refuse to start"
PLATFORM_SENTENCE = (
    "the platform process that opens this ledger would refuse; the backend does not open it"
)
LEDGER_VERDICT = (
    "DB-PREFLIGHT REFUSED (ledger) — the platform process would refuse; "
    "the backend does not open this ledger"
)
_SAFE_ENV = {
    "BROKER_MODE": "demo",
    "ALLOW_ORDER_TRANSMIT": "false",
    "ALLOW_LIVE_TRADING": "false",
    "PYTHONDONTWRITEBYTECODE": "1",
}


@pytest.fixture(autouse=True)
def _fresh_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _url(path: Path) -> str:
    return f"sqlite:///{path}"


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _valid_database(path: Path) -> Path:
    """A real, current-schema, 0600 database with no sidecars left behind."""

    path.parent.mkdir(parents=True, exist_ok=True)
    database = Database(_url(path))
    database.initialize()
    database.dispose()
    assert _mode(path) == 0o600
    return path


def _sql(path: Path, statement: str, rows: list[tuple[Any, ...]] | None = None) -> None:
    connection = sqlite3.connect(path)
    try:
        if rows is None:
            connection.execute(statement)
        else:
            connection.executemany(statement, rows)
        connection.commit()
    finally:
        connection.close()


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    try:
        code = main(["db-preflight", *argv])
    except SystemExit as exit_:  # argparse: an unknown command exits 2 — that is the red
        code = int(exit_.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _child(code: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **_SAFE_ENV, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _oracle_text(completed: subprocess.CompletedProcess[str]) -> str:
    for line in completed.stdout.splitlines():
        if line.startswith("ORACLE:"):
            return line.removeprefix("ORACLE:")
    raise AssertionError(
        f"the oracle printed no verdict: {completed.stdout!r} {completed.stderr!r}"
    )


def _boot_oracle(path: Path) -> subprocess.CompletedProcess[str]:
    """What the backend's own construction does with this file, in a fresh process."""

    return _child(
        f"""
        from chronos.persistence.database import Database
        try:
            Database({_url(path)!r}).initialize()
        except RuntimeError as error:
            print("ORACLE:" + str(error))
            raise SystemExit(3)
        print("ORACLE:OK")
        """
    )


def _ledger_oracle(path: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return _child(
        f"""
        from pathlib import Path
        from chronos.execution.sqlite_ledger import SqliteLedger
        try:
            SqliteLedger(Path({path!r})).close()
        except RuntimeError as error:
            print("ORACLE:" + str(error))
            raise SystemExit(3)
        print("ORACLE:OK")
        """,
        cwd=cwd,
    )


def _template_directive(section: str, name: str) -> str:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[method-assign]
    parser.read_string(TEMPLATE.read_text(encoding="utf-8"))
    return parser[section].get(name, "").strip()


# --- T1 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("damage", ["version", "drift"])
def test_the_default_run_refuses_an_identity_clean_0600_database_whose_schema_initialize_would_refuse(  # noqa: E501
    tmp_path: Path, capsys: pytest.CaptureFixture[str], damage: str
) -> None:
    """A file no lstat check can fault still refuses when initialize() would, with its text."""

    db = _valid_database(tmp_path / "state" / "chronos.db")
    if damage == "version":
        _sql(db, "UPDATE schema_version SET version = version + 1")
    else:
        _sql(db, "ALTER TABLE hash_chain_records DROP COLUMN kind")
    os.chmod(db, 0o600)
    assert os.lstat(db).st_nlink == 1

    code, out, _ = _run(["--database-url", _url(db)], capsys)
    oracle = _oracle_text(_boot_oracle(db))

    assert oracle != "OK"
    assert code == EXIT_REFUSED
    assert f"REFUSE [schema] {oracle}" in out
    assert f"DB-PREFLIGHT REFUSED (1) — {BACKEND_VERDICT}" in out


# --- T2 ---------------------------------------------------------------------------------------


def test_schema_that_exists_only_in_a_committed_crash_wal_is_observed_or_undecided_never_clear(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The schema committed into a WAL by a crashed writer is read; unreadable WAL truth is
    UNDECIDED, never CLEAR from the main file alone."""

    for bump, expected_code in ((False, 0), (True, EXIT_REFUSED)):
        state = tmp_path / ("bumped" if bump else "current")
        state.mkdir()
        db = state / "chronos.db"
        crashed = _child(
            f"""
            import os
            from sqlalchemy import text
            from chronos.persistence.database import Database
            database = Database({_url(db)!r})
            database.initialize()
            if {bump!r}:
                with database.engine.begin() as connection:
                    connection.execute(text("UPDATE schema_version SET version = version + 1"))
            os._exit(0)
            """
        )
        assert crashed.returncode == 0, crashed.stderr
        wal = db.with_name("chronos.db-wal")
        assert wal.stat().st_size > 0, "the fixture's schema must live in the WAL"
        immutable = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
        try:
            # Positive control: the main file alone shows no schema at all.
            assert immutable.execute("SELECT name FROM sqlite_master").fetchall() == []
        finally:
            immutable.close()

        code, out, _ = _run(["--database-url", _url(db)], capsys)
        assert code == expected_code, out
        copy = tmp_path / f"copy-{state.name}"
        shutil.copytree(state, copy)
        oracle = _oracle_text(_boot_oracle(copy / "chronos.db"))
        if bump:
            unsupported = f"Unsupported Chronos schema version {SCHEMA_VERSION + 1}; "
            assert oracle.startswith(unsupported + f"expected {SCHEMA_VERSION}")
            assert f"REFUSE [schema] {oracle}" in out
        else:
            assert oracle == "OK"
            assert f"CLEAR [schema] version {SCHEMA_VERSION}, no drift" in out

    current = tmp_path / "current"
    (current / "chronos.db-shm").unlink()
    os.chmod(current, 0o500)
    try:
        code, out, _ = _run(["--database-url", _url(current / "chronos.db")], capsys)
    finally:
        os.chmod(current, 0o700)
    assert code == EXIT_UNDECIDED, out
    assert "UNDECIDED [schema] cannot read the database read-only (sqlite3.OperationalError)" in out
    assert "unable to open" not in out, "the driver's text is not emitted"
    assert "CLEAR [schema]" not in out
    assert "DB-PREFLIGHT UNDECIDED (1)" in out


# --- T3 ---------------------------------------------------------------------------------------


def test_a_preflight_beside_a_live_writer_loses_no_committed_row_and_leaves_the_wal_inode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The DBLOCK class: a read-only preflight's close must not unlink the writer's WAL."""

    db = _valid_database(tmp_path / "chronos.db")
    ack = tmp_path / "acks"
    writer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import os, sqlite3, time
                connection = sqlite3.connect({str(db)!r})
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("CREATE TABLE IF NOT EXISTS probe_rows (x INTEGER)")
                connection.commit()
                fd = os.open({str(ack)!r}, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                for i in range(3000):
                    connection.execute("INSERT INTO probe_rows VALUES (?)", (i,))
                    connection.commit()
                    os.write(fd, b"1")
                    time.sleep(0.002)
                time.sleep(60)
                """
            ),
        ],
        env={**os.environ, **_SAFE_ENV},
    )
    wal = db.with_name("chronos.db-wal")
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (ack.exists() and ack.stat().st_size >= 50):
            time.sleep(0.05)
        assert ack.stat().st_size >= 50, "the writer did not get going"
        before = wal.stat()
        code, out, _ = _run(["--database-url", _url(db)], capsys)
        # The writer's probe table is schema drift, so the verdict is a refusal; the pin is
        # what the preflight's connection close did to the WAL under a live writer.
        assert code in (0, EXIT_REFUSED), out
        assert writer.poll() is None
        after = wal.stat()
        assert after.st_ino == before.st_ino
        assert after.st_size >= before.st_size
        time.sleep(0.3)
    finally:
        writer.kill()
        writer.wait()
    acked = ack.stat().st_size
    connection = sqlite3.connect(db)
    try:
        rows = connection.execute("SELECT count(*) FROM probe_rows").fetchone()[0]
    finally:
        connection.close()
    assert acked > 50
    assert rows >= acked, f"acknowledged {acked}, recovered {rows}"


# --- T4 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    ["symlink-db", "symlink-wal", "fifo", "foreign-owner", "hardlink-db", "hardlink-wal", "stray"],
)
def test_identity_classes_refuse_with_the_runtime_text_and_exit_78(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Every identity refusal of the runtime is reported with the runtime's own text, and the
    database is never opened afterwards (a FIFO would block an opener)."""

    state = tmp_path / "state"
    state.mkdir()
    db = state / "chronos.db"
    offending = db
    if case == "symlink-db":
        db.symlink_to(_valid_database(tmp_path / "real.db"))
    elif case == "symlink-wal":
        _valid_database(db)
        elsewhere = tmp_path / "elsewhere-wal"
        elsewhere.touch(mode=0o600)
        offending = state / "chronos.db-wal"
        offending.symlink_to(elsewhere)
    elif case == "fifo":
        os.mkfifo(db, 0o600)
    elif case == "foreign-owner":
        _valid_database(db)
        monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    elif case == "hardlink-db":
        _valid_database(db)
        os.link(db, tmp_path / "twin.db")
    elif case == "hardlink-wal":
        _valid_database(db)
        offending = state / "chronos.db-wal"
        offending.touch(mode=0o600)
        os.link(offending, tmp_path / "twin-wal")
    else:
        _valid_database(db)
        os.link(db, state / ".chronos.db.create-0123456789abcdef")

    with pytest.raises(RuntimeError) as expected:
        database_module._check_sqlite_file_identity(offending)

    code, out, _ = _run(["--database-url", _url(db)], capsys)
    assert code == EXIT_REFUSED, out
    assert f"REFUSE [db] {expected.value}" in out
    assert BACKEND_VERDICT in out
    assert "[schema]" not in out


# --- T5 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "outcome"),
    [(0o644, "warn"), (0o666, "warn"), (0o400, "warn"), (0o200, "refuse"), (0o000, "refuse")],
)
def test_mode_drift_agrees_with_boot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: int, outcome: str
) -> None:
    """What the first construction repairs is a WARN; what it cannot even open is a REFUSE with
    the boot's own text. The oracle is a fresh process constructing the real Database."""

    db = _valid_database(tmp_path / "chronos.db")
    os.chmod(db, mode)

    code, out, _ = _run(["--database-url", _url(db)], capsys)
    oracle = _boot_oracle(db)
    if outcome == "warn":
        assert code == 0, out
        assert (
            f"WARN [db] mode {mode:04o}: Chronos will chmod 0600 at its first construction" in out
        )
        assert _oracle_text(oracle) == "OK"
        assert _mode(db) == 0o600
    else:
        assert code == EXIT_REFUSED, out
        refusal = f"Unable to secure SQLite path without following links: {db}"
        assert f"REFUSE [db] {refusal}" in out
        assert oracle.returncode == 3
        assert _oracle_text(oracle) == refusal
        assert "[schema]" not in out, "the database is never opened after a mode refusal"


# --- T6 ---------------------------------------------------------------------------------------


def test_the_preflight_changes_no_mode_unlinks_nothing_and_creates_only_sqlite_sidecars(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The only mutation is SQLite's own sidecar creation for a read-only WAL open."""

    db = _valid_database(tmp_path / "chronos.db")
    os.chmod(db, 0o644)
    calls: list[str] = []
    for name in ("chmod", "fchmod", "unlink", "remove", "rename", "replace", "link", "rmdir"):
        monkeypatch.setattr(os, name, lambda *a, _n=name, **k: calls.append(_n))
    for name in ("chmod", "unlink", "rename", "replace", "touch", "write_text", "write_bytes"):
        monkeypatch.setattr(Path, name, lambda self, *a, _n=name, **k: calls.append(_n))
    connects: list[str] = []
    real_connect = sqlite3.connect

    def recording_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        connects.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    before = {entry.name for entry in tmp_path.iterdir()}

    code, out, _ = _run(["--database-url", _url(db)], capsys)

    assert code == 0, out
    assert calls == []
    created = {entry.name for entry in tmp_path.iterdir()} - before
    assert created <= {"chronos.db-wal", "chronos.db-shm"}
    for name in created:
        assert _mode(tmp_path / name) == _mode(db)
    assert _mode(db) == 0o644
    assert connects, "the schema check opens a read-only connection"
    assert all("mode=ro" in text and "immutable" not in text for text in connects), connects
    assert "WARN [db] mode 0644" in out
    assert "SQLite may create" in out


# --- T7 ---------------------------------------------------------------------------------------


def test_evidence_estimate_names_both_bounds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """2 500 rows x 1 000 bytes: with 1000 rows/tick and 800 000 bytes/tick the byte bound
    binds (4 ticks); with the default byte bound the row bound does (3). Always "at least"."""

    db = _valid_database(tmp_path / "chronos.db")
    payload = "x" * 1000
    _sql(
        db,
        "INSERT INTO hash_chain_records (stream, sequence, kind, payload_json, recorded_at, "
        "previous_hash, record_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "autonomy.evidence:acct-1",
                i,
                "evidence",
                payload,
                "2026-10-05 00:00:00",
                "0" * 64,
                f"{i:064x}",
            )
            for i in range(2500)
        ],
    )
    monkeypatch.setenv("AUTONOMY_EVIDENCE_BUNDLES", "1")
    monkeypatch.setenv("AUTONOMY_EVIDENCE_PASS_ROWS_PER_TICK", "1000")
    monkeypatch.setenv("AUTONOMY_EVIDENCE_PASS_BYTES_PER_TICK", "800000")

    code, out, _ = _run(["--database-url", _url(db), "--evidence"], capsys)
    assert code == 0, out
    assert (
        "INFO [evidence] autonomy.evidence:acct-1: 2500 rows, 2500000 bytes; "
        "the first pass needs at least 4 ticks (rows/tick 1000, bytes/tick 800000)"
    ) in out
    assert "an estimate, never exact" in out
    assert "checked: identity, mode, schema, evidence" in out

    get_settings.cache_clear()
    monkeypatch.setenv("AUTONOMY_EVIDENCE_PASS_BYTES_PER_TICK", "1048576")
    code, out, _ = _run(["--database-url", _url(db), "--evidence"], capsys)
    assert code == 0
    assert "the first pass needs at least 3 ticks (rows/tick 1000, bytes/tick 1048576)" in out

    get_settings.cache_clear()
    monkeypatch.setenv("AUTONOMY_EVIDENCE_BUNDLES", "0")
    code, out, _ = _run(["--database-url", _url(db), "--evidence"], capsys)
    assert code == 0
    assert "INFO [evidence] AUTONOMY_EVIDENCE_BUNDLES is off: the pass does not run" in out


# --- T8 ---------------------------------------------------------------------------------------


def test_exit_codes_are_0_78_1_and_every_exception_maps_to_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _valid_database(tmp_path / "chronos.db")
    codes: list[int] = []

    code, out, err = _run(["--database-url", _url(db)], capsys)
    codes.append(code)
    assert code == 0, out
    assert "DB-PREFLIGHT CLEAR — checked: identity, mode, schema; NOT checked:" in out
    assert "connect-time pragmas" in out and "path stability" in out
    assert err == ""

    (tmp_path / "link.db").symlink_to(db)
    code, out, err = _run(["--database-url", _url(tmp_path / "link.db")], capsys)
    codes.append(code)
    assert code == EXIT_REFUSED and err == ""

    code, out, err = _run(["--database-url", "not a url"], capsys)
    codes.append(code)
    assert code == EXIT_UNDECIDED, out
    assert "DB-PREFLIGHT UNDECIDED (1)" in out and err == ""

    code, out, err = _run(["--database-url", "sqlite://"], capsys)
    codes.append(code)
    assert code == EXIT_UNDECIDED, out
    assert "not a file-backed SQLite database" in out
    assert "checked: identity" not in out and err == ""

    module = importlib.import_module("chronos.cli.db_preflight")

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "_identity_lines", explode)
    code, out, err = _run(["--database-url", _url(db)], capsys)
    codes.append(code)
    assert code == EXIT_UNDECIDED, out
    assert "DB-PREFLIGHT UNDECIDED (1) — RuntimeError" in out
    assert "boom" not in out, "the exception class is reported, never its text"
    assert "Traceback" not in err and err == ""
    assert set(codes) <= {0, EXIT_UNDECIDED, EXIT_REFUSED}


# --- T10 --------------------------------------------------------------------------------------


def test_the_preflight_uses_the_runtimes_own_identity_checks_rather_than_copies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity findings come from the runtime's functions, called on the runtime's namespace;
    the preflight carries none of their texts."""

    db = _valid_database(tmp_path / "chronos.db")
    ledger = tmp_path / "platform_ledger.db"
    SqliteLedger(ledger).close()
    seen_db: list[Path] = []
    seen_ledger: list[Path] = []
    real_db = database_module._check_sqlite_file_identity
    real_ledger = ledger_module._check_ledger_file_identity

    def spy_db(path: Path) -> None:
        seen_db.append(Path(path))
        real_db(path)

    def spy_ledger(path: Path) -> None:
        seen_ledger.append(Path(path))
        real_ledger(path)

    monkeypatch.setattr(database_module, "_check_sqlite_file_identity", spy_db)
    monkeypatch.setattr(ledger_module, "_check_ledger_file_identity", spy_ledger)

    code, out, _ = _run(["--database-url", _url(db), "--ledger", str(ledger)], capsys)
    assert code == 0, out
    assert set(seen_db) == {db, *(db.with_name(db.name + s) for s in ("-wal", "-shm", "-journal"))}
    assert set(seen_ledger) == {
        ledger,
        *(ledger.with_name(ledger.name + s) for s in ("-wal", "-shm", "-journal")),
    }
    source = (ROOT / "src" / "chronos" / "cli" / "db_preflight.py").read_text(encoding="utf-8")
    for copied in (
        "Refusing symbolic-link",
        "Refusing non-regular",
        "not owned by this user",
        "hard links",
    ):
        assert copied not in source


# --- T11 --------------------------------------------------------------------------------------


def test_db_preflight_is_a_registered_command_that_parses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_:
        main(["db-preflight", "--help"])
    assert exit_.value.code == 0
    out = capsys.readouterr().out
    assert "--ledger" in out and "--evidence" in out and "--json" in out


# --- T12 / T13 --------------------------------------------------------------------------------


def _ledger_fixture(work: Path, case: str) -> Path:
    data = work / "data"
    data.mkdir(exist_ok=True)
    path = data / "platform_ledger.db"
    if case == "symlink":
        real = work / "real_platform.db"
        SqliteLedger(real).close()
        path.symlink_to(real)
        return path
    SqliteLedger(path).close()
    if case == "mode-0644":
        os.chmod(path, 0o644)
    elif case == "mode-0200":
        os.chmod(path, 0o200)
    else:
        _sql(path, "UPDATE schema_info SET version = version + 1")
        os.chmod(path, 0o600)
    return path


@pytest.mark.parametrize("case", ["symlink", "mode-0644", "mode-0200", "version-bumped"])
def test_the_default_run_and_the_unit_condition_never_mention_or_refuse_on_a_ledger(
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Daybreak P1-OPS-2r1-1, side one: the backend does not open the platform ledger, so
    nothing about an unsafe or unsupported ledger at the default path reaches the unit."""

    work = tmp_path_factory.mktemp("work")
    assert "ledger" not in str(work).lower()
    db = _valid_database(work / "state" / "chronos.db")
    _ledger_fixture(work, case)
    monkeypatch.chdir(work)

    code, out, _ = _run(["--database-url", _url(db)], capsys)
    assert code == 0, out
    assert "ledger" not in out.lower()
    condition = _template_directive("Service", "ExecCondition")
    assert "db-preflight" in condition
    assert "--ledger" not in condition


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("symlink", EXIT_REFUSED),
        ("mode-0200", EXIT_REFUSED),
        ("version-bumped", EXIT_REFUSED),
        ("mode-0644", 0),
    ],
)
def test_the_explicit_ledger_diagnostic_refuses_the_same_fixture_with_the_platform_wording(
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_code: int,
) -> None:
    """Daybreak P1-OPS-2r1-1, side two: through --ledger the same fixture refuses with the
    ledger's own text and the platform wording, never "the backend would refuse". A 0644 ledger
    is repaired by the ledger's first construction (sqlite_ledger.py), so it is a WARN."""

    work = tmp_path_factory.mktemp("work")
    db = _valid_database(work / "state" / "chronos.db")
    _ledger_fixture(work, case)
    monkeypatch.chdir(work)
    resolved = Path(os.path.abspath("data/platform_ledger.db"))

    code, out, _ = _run(["--database-url", _url(db), "--ledger", "data/platform_ledger.db"], capsys)
    oracle = _ledger_oracle("data/platform_ledger.db", work)
    assert code == expected_code, out
    if case == "symlink":
        with pytest.raises(RuntimeError) as raised:
            ledger_module._check_ledger_file_identity(resolved)
        expected = str(raised.value)
    elif case == "mode-0200":
        expected = (
            f"Refusing ledger path {resolved}: repair failed (Permission denied); "
            "chmod 600 it and restart"
        )
    elif case == "version-bumped":
        expected = (
            "ledger schema version 2 unsupported (expected 1); "
            "refusing to run against an unknown schema"
        )
    else:
        expected = "OK"
    if expected_code == EXIT_REFUSED:
        assert f"REFUSE [ledger] {expected} — {PLATFORM_SENTENCE}" in out
        assert LEDGER_VERDICT in out
        assert BACKEND_VERDICT not in out
        assert oracle.returncode == 3
        assert _oracle_text(oracle) == expected
    else:
        warned = "WARN [ledger] mode 0644: the platform process will chmod 0600 at its first "
        assert warned + "construction" in out
        assert "DB-PREFLIGHT CLEAR" in out
        assert _oracle_text(oracle) == "OK"
        assert _mode(resolved) == 0o600


# --- r1: credentials never reach output (Daybreak P1-1 at 5b8a7c9) ---------------------------

#: A synthetic password, never a real credential, unique enough to grep for.
_SENTINEL = "pw-sentinel-7f3a2c"
#: The first two characters of a malformed password (the reader's P1-R2-1): an unencoded "@"
#: and ":" inside the password push the sentinel into the URL parser's port slot, whose
#: ValueError text echoes it.
_FRAGMENT = "pa"
#: Credential-bearing URL shapes: user information; a query parameter that SQLAlchemy's
#: PostgreSQL dialect maps into the connection's password (Daybreak r1 re-pin); and the
#: malformed password the runtime's URL parser rejects with its own text.
_CREDENTIAL_URLS = {
    "userinfo": f"postgresql://synthetic-user:{_SENTINEL}@example.invalid/chronos",
    "query": f"postgresql://example.invalid/chronos?password={_SENTINEL}",
    "malformed": f"postgresql://synthetic-user:{_FRAGMENT}@ss:{_SENTINEL}@example.invalid/chronos",
}
#: A driver-style DSN a caught database error could carry (Daybreak r2 re-pin).
_DSN = f"DRIVER={{PostgreSQL Unicode}};SERVER=example.invalid;UID=synthetic-user;PWD={_SENTINEL}"


def _emitted(mode: str, out: str) -> tuple[str, str]:
    """(every line's text, the verdict) for either output mode."""

    if mode == "json":
        document = json.loads(out)
        return "\n".join(line["text"] for line in document["lines"]), str(document["verdict"])
    return out, out


@pytest.mark.parametrize("mode", ["text", "json"])
@pytest.mark.parametrize("path", ["environment", "flag", "exception"])
@pytest.mark.parametrize("shape", ["userinfo", "query", "malformed"])
def test_a_database_url_with_credentials_never_reaches_any_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    path: str,
    shape: str,
) -> None:
    """Daybreak P1-1 at 5b8a7c9 and P1-OPS-2-BUILD-r1-1 at 7842b4f: the unit runs the command
    on its private DATABASE_URL and the journal keeps the output, so the configured URL,
    credentials included in either shape, must not be printed by any path: the non-file-backed
    diagnostic reached from the environment or the flag, or an exception whose text echoes it.
    An unexpected error is reported by its class alone; exception text is never emitted."""

    credential_url = _CREDENTIAL_URLS[shape]
    extra = ["--json"] if mode == "json" else []
    if path == "environment":
        monkeypatch.setenv("DATABASE_URL", credential_url)
        argv = extra
    elif path == "flag":
        argv = ["--database-url", credential_url, *extra]
    else:
        db = _valid_database(tmp_path / "chronos.db")
        module = importlib.import_module("chronos.cli.db_preflight")

        def echo(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(f"cannot read {credential_url}")

        monkeypatch.setattr(module, "_identity_lines", echo)
        argv = ["--database-url", _url(db), *extra]

    code, out, err = _run(argv, capsys)
    texts, verdict = _emitted(mode, out)
    if path == "exception":
        assert code == EXIT_UNDECIDED, out
        assert "DB-PREFLIGHT UNDECIDED (1) — RuntimeError" in verdict
        assert "cannot read" not in out, "exception text must not be emitted"
    elif shape == "malformed":
        # The runtime's own URL parser rejects it, so the backend would refuse to construct
        # its Database: a refusal that names the error class and nothing of the value.
        assert code == EXIT_REFUSED, out
        assert (
            "configured DATABASE_URL is not accepted by the runtime's URL parser (ValueError)"
            in texts
        )
        assert "invalid literal" not in out, "the parser's text must not be emitted"
    else:
        assert code == EXIT_UNDECIDED, out
        assert "configured DATABASE_URL is not a file-backed SQLite database" in texts
    for leaked in (_SENTINEL, credential_url):
        assert leaked not in out
        assert leaked not in err


@pytest.mark.parametrize("mode", ["text", "json"])
@pytest.mark.parametrize("where", ["database-open", "ledger-open", "ledger-read"])
def test_a_caught_driver_error_carrying_a_dsn_is_reported_by_class_alone(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    where: str,
) -> None:
    """Daybreak P1-OPS-2-BUILD-r2-1 at 2f3f5f2: the inner catches (the read-only database
    open, the ledger open, the ledger read) must not serialize the driver's text, which can
    carry a DSN with a password; the line names the effective exception class and nothing
    else of the error."""

    db = _valid_database(tmp_path / "chronos.db")
    ledger = tmp_path / "platform_ledger.db"
    SqliteLedger(ledger).close()
    module = importlib.import_module("chronos.cli.db_preflight")
    real_connect = sqlite3.connect

    def raising(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError(_DSN)

    class _BrokenConnection:
        def execute(self, *args: Any, **kwargs: Any) -> Any:
            raise sqlite3.OperationalError(_DSN)

        def close(self) -> None:
            return None

    def ledger_only(database: Any, *args: Any, **kwargs: Any) -> Any:
        if "platform_ledger" not in str(database):
            return real_connect(database, *args, **kwargs)
        if where == "ledger-open":
            raise sqlite3.OperationalError(_DSN)
        return _BrokenConnection()

    if where == "database-open":
        monkeypatch.setattr(module, "_schema_lines", raising)
        subject = "database"
    else:
        monkeypatch.setattr(sqlite3, "connect", ledger_only)
        subject = "ledger"
    extra = ["--json"] if mode == "json" else []

    code, out, err = _run(["--database-url", _url(db), "--ledger", str(ledger), *extra], capsys)
    texts, _ = _emitted(mode, out)
    assert code == EXIT_UNDECIDED, out
    assert f"cannot read the {subject} read-only (sqlite3.OperationalError)" in texts
    for leaked in (_SENTINEL, _DSN, "PWD=", "DRIVER="):
        assert leaked not in out
        assert leaked not in err


def test_a_stray_private_temporary_not_sharing_the_inode_is_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reader N1: a leftover ``.chronos.db.create-<hex>`` that is NOT linked to the database is
    reported and never refused (the runtime names only one that shares the inode)."""

    db = _valid_database(tmp_path / "chronos.db")
    stray = tmp_path / ".chronos.db.create-fedcba9876543210"
    stray.touch(mode=0o600)
    code, out, _ = _run(["--database-url", _url(db)], capsys)
    assert code == 0, out
    warned = "WARN [db] a private temporary from an interrupted create is present and not linked "
    assert warned + f"to the file: {stray}" in out


# --- r3a: the reader's P3 pins at b3ded48 (test-only; nothing else moves) --------------------


def test_a_malformed_password_whose_fragment_spells_the_file_uri_words_still_gets_the_generic_sentence(  # noqa: E501
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reader P3-1: the one passed-through text is selected by exact equality with the runtime's
    static ``file:`` sentence. A parser echo that merely contains those words (here the sentinel
    sits in the port slot the parser rejects) must still get the generic sentence."""

    sentinel = f"SQLite-file-URI-{_SENTINEL}"
    malformed = f"postgresql://synthetic-user:{_FRAGMENT}@ss:{sentinel}@example.invalid/chronos"
    code, out, err = _run(["--database-url", malformed], capsys)
    assert code == EXIT_REFUSED, out
    assert "configured DATABASE_URL is not accepted by the runtime's URL parser (ValueError)" in out
    for leaked in (sentinel, _SENTINEL, "invalid literal"):
        assert leaked not in out
        assert leaked not in err


def test_a_file_uri_database_url_gets_the_runtimes_exact_static_sentence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reader P3-2: the runtime's static ``file:`` refusal is passed through verbatim — the
    expected text is taken from the runtime here too, never copied."""

    with pytest.raises(ValueError) as raised:
        database_module._sqlite_database_path("sqlite:///file:pinned?mode=ro")
    sentence = str(raised.value)
    assert sentence.startswith("SQLite file: URI DATABASE_URL targets are not supported")
    code, out, _ = _run(["--database-url", "sqlite:///file:pinned?mode=ro"], capsys)
    assert code == EXIT_REFUSED, out
    assert f"REFUSE [db] {sentence}" in out
    assert "not accepted by the runtime's URL parser" not in out
