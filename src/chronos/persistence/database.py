"""SQLite engine creation and explicit schema initialization."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import math
import os
import stat
import sys
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, UniqueConstraint, create_engine, event, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from chronos.persistence.schema import Base, DatabaseScopeRow, SchemaVersionRow
from chronos.utils.identifiers import account_fingerprint

SCHEMA_VERSION = 17

_DATABASE_RECOVERY_GUIDANCE = (
    "Preserve and back up this database first. A prior-version schema can be upgraded with "
    "`alembic upgrade head` (see alembic.ini); a drifted or unversioned schema cannot — "
    "configure a fresh DATABASE_URL instead. Chronos never modifies such a database itself."
)
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

#: How long a connection waits for a competing writer before reporting
#: "database is locked". The single-writer lease means sustained contention is
#: already a bug, so this covers the brief overlap during a writer handover
#: rather than papering over two writers running at once.
_SQLITE_BUSY_TIMEOUT_MS = 5_000

_ACCOUNT_SCOPED_TABLES = (
    "wheel_cycles",
    "strategy_state",
    "candidate_evaluations",
    "rejected_candidate_reasons",
    "order_drafts",
    "order_previews",
    "submitted_orders",
    "fills",
    "commissions",
    "strategy_basis_entries",
    "reconciliation_runs",
    "application_events",
    "guardrail_decisions",
    # Live-wheel order pipeline (schema v3): all carry account-specific data.
    "order_intents",
    "order_confirmations",
    "live_arm_events",
    "kill_switch_events",
    "cash_reservations",
    "share_reservations",
    # Order-management lifecycle (schema v4): account-specific order/risk data.
    "order_events",
    "risk_decisions",
    "risk_check_results",
    # Autonomy supervisor durable state (schema v5): all account-specific.
    # `hash_chain_records` is deliberately NOT here: it is a generic append-only
    # store whose streams are named per account, and its payloads are already
    # scoped by the writer. Listing it would make an unrelated stream block a
    # rebind.
    "autonomy_mandate_activations",
    "autonomy_session_counters",
    "autonomy_decision_attempts",
    "autonomy_owner_alerts",
    "autonomy_proposal_queue",
    # QQQ PAPER position admission (schema v11): pseudonymous broker/order identity.
    "managed_position_bindings",
)


class Database:
    """Own the SQLAlchemy engine and enforce the supported schema version."""

    def __init__(self, url: str) -> None:
        self.url = url
        configured_url = make_url(url)
        is_sqlite = configured_url.get_backend_name() == "sqlite"
        self._sqlite_path = _sqlite_database_path(url)
        self._prepare_sqlite_parent(self._sqlite_path)
        self._prepare_private_sqlite_file()
        # R-21, and the ordering is load-bearing: reject a symlinked database or
        # sidecar BEFORE any connection is opened. This used to run only after
        # the first connect, which happened to work while the journal mode was
        # `delete`. Enabling WAL exposed the latent bug — switching journal modes
        # makes SQLite unlink a stale `-wal`/`-journal` file, so by the time the
        # check ran the symlink it was meant to reject had already been removed
        # and the check passed on the real file SQLite had just created. Nothing
        # was written through the link, but a guard that silently stops firing is
        # a guard that is no longer there. Checking first also means SQLite never
        # gets the chance to follow a link we would have refused.
        self._restrict_sqlite_file_mode()
        engine_kwargs: dict[str, object] = {}
        if is_sqlite:
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        if is_sqlite and configured_url.database in {None, "", ":memory:"}:
            engine_kwargs["poolclass"] = StaticPool
        self.engine: Engine = create_engine(url, **engine_kwargs)
        if is_sqlite:
            event.listen(
                self.engine,
                "connect",
                partial(_configure_sqlite_connection, file_backed=self._sqlite_path is not None),
            )
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False, class_=Session)
        if self._sqlite_path is not None:
            with self.engine.connect():
                pass
            self._restrict_sqlite_file_mode()

    def initialize(self) -> None:
        schema_inspector = inspect(self.engine)
        table_names = set(schema_inspector.get_table_names())
        if "schema_version" not in table_names:
            if table_names:
                raise RuntimeError(
                    "Refusing to initialize an unversioned database containing existing tables: "
                    + ", ".join(sorted(table_names))
                    + ". "
                    + _DATABASE_RECOVERY_GUIDANCE
                )
            Base.metadata.create_all(self.engine)
            with self.sessions.begin() as session:
                session.add(SchemaVersionRow(version=SCHEMA_VERSION))
            self._restrict_sqlite_file_mode()
            return

        version_columns = {
            str(column["name"]) for column in schema_inspector.get_columns("schema_version")
        }
        if not {"id", "version"} <= version_columns:
            raise RuntimeError(
                "Chronos schema_version does not contain readable id and version columns. "
                + _DATABASE_RECOVERY_GUIDANCE
            )
        with self.sessions.begin() as session:
            current_version = session.scalar(
                select(SchemaVersionRow.version).order_by(SchemaVersionRow.id.desc())
            )
            if current_version is None:
                raise RuntimeError(
                    "Chronos schema_version exists without a version record. "
                    + _DATABASE_RECOVERY_GUIDANCE
                )
        if current_version != SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported Chronos schema version {current_version}; expected {SCHEMA_VERSION}. "
                + _DATABASE_RECOVERY_GUIDANCE
            )
        drift = _schema_drift(self.engine)
        if drift:
            raise RuntimeError(
                f"Chronos schema version {SCHEMA_VERSION} does not match the required schema: "
                + "; ".join(drift)
                + ". "
                + _DATABASE_RECOVERY_GUIDANCE
            )
        self._restrict_sqlite_file_mode()

    def bind_scope(
        self,
        *,
        broker_mode: str,
        environment: str,
        account_id: str,
    ) -> None:
        """Bind this database to one account without persisting the account identifier."""

        normalized_broker_mode = broker_mode.strip()
        normalized_environment = environment.strip()
        if not normalized_broker_mode or not normalized_environment:
            raise ValueError("broker_mode and environment must not be blank")
        fingerprint = account_fingerprint(account_id)
        with self.sessions.begin() as session:
            current = session.get(DatabaseScopeRow, 1)
            if current is None:
                populated_tables = _populated_account_tables(session)
                if populated_tables:
                    raise RuntimeError(
                        "Refusing to bind an unscoped Chronos database that already contains "
                        "account-specific data in: " + ", ".join(populated_tables)
                    )
                session.add(
                    DatabaseScopeRow(
                        id=1,
                        broker_mode=normalized_broker_mode,
                        environment=normalized_environment,
                        account_fingerprint=fingerprint,
                    )
                )
                return
            if (
                current.broker_mode != normalized_broker_mode
                or current.environment != normalized_environment
                or current.account_fingerprint != fingerprint
            ):
                raise RuntimeError(
                    "This Chronos database is already bound to a different broker scope; "
                    "configure a separate DATABASE_URL."
                )

    def dispose(self) -> None:
        self.engine.dispose()

    def readable(self) -> bool:
        """Run one bounded application-store read without leaking failure details."""

        try:
            with self.engine.connect() as connection:
                version = connection.scalar(
                    select(SchemaVersionRow.version).order_by(SchemaVersionRow.id.desc()).limit(1)
                )
                return bool(version == SCHEMA_VERSION)
        except (OSError, RuntimeError, SQLAlchemyError):
            return False

    def _restrict_sqlite_file_mode(self) -> None:
        if self._sqlite_path is None:
            return
        for path in (
            self._sqlite_path,
            *(Path(f"{self._sqlite_path}{suffix}") for suffix in _SQLITE_SIDECAR_SUFFIXES),
        ):
            _secure_sqlite_file(path)

    def _prepare_private_sqlite_file(self) -> None:
        if self._sqlite_path is None:
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            file_descriptor = os.open(self._sqlite_path, flags, 0o600)
        except FileExistsError:
            _secure_sqlite_file(self._sqlite_path)
            return
        try:
            os.fchmod(file_descriptor, 0o600)
        finally:
            os.close(file_descriptor)

    @staticmethod
    def _prepare_sqlite_parent(path: Path | None) -> None:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)


def _sqlite_database_path(url: str) -> Path | None:
    configured_url = make_url(url)
    if configured_url.get_backend_name() != "sqlite":
        return None
    database = configured_url.database
    if not database or database == ":memory:":
        return None
    if database.startswith("file:"):
        raise ValueError(
            "SQLite file: URI DATABASE_URL targets are not supported because Chronos cannot "
            "safely enforce database and sidecar file permissions."
        )
    return Path(database).expanduser()


def _secure_sqlite_file(path: Path) -> None:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        file_descriptor = os.open(path, flags)
    except FileNotFoundError:
        return
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise RuntimeError(f"Refusing symbolic-link SQLite path: {path}") from error
        raise RuntimeError(
            f"Unable to secure SQLite path without following links: {path}"
        ) from error

    try:
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"Refusing non-regular SQLite path: {path}")
        if metadata.st_uid != os.geteuid():
            raise RuntimeError(f"Refusing SQLite path not owned by this user: {path}")
        os.fchmod(file_descriptor, 0o600)
    finally:
        os.close(file_descriptor)


def _schema_drift(engine: Engine) -> tuple[str, ...]:
    schema_inspector = inspect(engine)
    actual_tables = set(schema_inspector.get_table_names())
    # Alembic's own bookkeeping table is expected on migrated databases and is
    # deliberately outside the Chronos metadata.
    actual_tables.discard("alembic_version")
    expected_tables = set(Base.metadata.tables)
    drift: list[str] = []

    missing_tables = expected_tables - actual_tables
    if missing_tables:
        drift.append("missing tables: " + ", ".join(sorted(missing_tables)))
    unexpected_tables = actual_tables - expected_tables
    if unexpected_tables:
        drift.append("unexpected tables: " + ", ".join(sorted(unexpected_tables)))

    for table_name in sorted(expected_tables & actual_tables):
        expected_table = Base.metadata.tables[table_name]
        actual_columns = {
            str(column["name"]): column for column in schema_inspector.get_columns(table_name)
        }
        expected_columns = {column.name: column for column in expected_table.columns}

        missing_columns = set(expected_columns) - set(actual_columns)
        if missing_columns:
            drift.append(f"{table_name} missing columns: " + ", ".join(sorted(missing_columns)))
        unexpected_columns = set(actual_columns) - set(expected_columns)
        if unexpected_columns:
            drift.append(
                f"{table_name} unexpected columns: " + ", ".join(sorted(unexpected_columns))
            )

        for column_name in sorted(set(expected_columns) & set(actual_columns)):
            expected_column = expected_columns[column_name]
            actual_column = actual_columns[column_name]
            expected_type = expected_column.type.compile(dialect=engine.dialect).upper()
            actual_type = actual_column["type"].compile(dialect=engine.dialect).upper()
            if actual_type != expected_type:
                drift.append(
                    f"{table_name}.{column_name} type is {actual_type}, expected {expected_type}"
                )
            if bool(actual_column["nullable"]) != bool(expected_column.nullable):
                expected_nullability = "nullable" if expected_column.nullable else "NOT NULL"
                drift.append(
                    f"{table_name}.{column_name} nullability does not enforce "
                    f"{expected_nullability}"
                )

        expected_primary_key = tuple(column.name for column in expected_table.primary_key.columns)
        actual_primary_key = tuple(
            schema_inspector.get_pk_constraint(table_name).get("constrained_columns") or ()
        )
        if actual_primary_key != expected_primary_key:
            drift.append(
                f"{table_name} primary key is {actual_primary_key}, expected {expected_primary_key}"
            )

        expected_unique_constraints = {
            tuple(column.name for column in constraint.columns)
            for constraint in expected_table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        actual_unique_constraints = {
            tuple(constraint.get("column_names") or ())
            for constraint in schema_inspector.get_unique_constraints(table_name)
        }
        if actual_unique_constraints != expected_unique_constraints:
            drift.append(f"{table_name} unique constraints do not match")

        expected_foreign_keys = {
            (
                tuple(element.parent.name for element in constraint.elements),
                constraint.referred_table.name,
                tuple(element.column.name for element in constraint.elements),
            )
            for constraint in expected_table.foreign_key_constraints
        }
        actual_foreign_keys = {
            (
                tuple(foreign_key.get("constrained_columns") or ()),
                str(foreign_key.get("referred_table")),
                tuple(foreign_key.get("referred_columns") or ()),
            )
            for foreign_key in schema_inspector.get_foreign_keys(table_name)
        }
        if actual_foreign_keys != expected_foreign_keys:
            drift.append(f"{table_name} foreign keys do not match")

        expected_indexes = {
            (tuple(column.name for column in index.columns), bool(index.unique))
            for index in expected_table.indexes
        }
        actual_indexes = {
            (tuple(index.get("column_names") or ()), bool(index.get("unique")))
            for index in schema_inspector.get_indexes(table_name)
        }
        if actual_indexes != expected_indexes:
            drift.append(f"{table_name} indexes do not match")

    return tuple(drift)


def _populated_account_tables(session: Session) -> tuple[str, ...]:
    connection = session.connection()
    existing_tables = set(inspect(connection).get_table_names())
    return tuple(
        table_name
        for table_name in _ACCOUNT_SCOPED_TABLES
        if table_name in existing_tables and _table_has_rows(connection, table_name)
    )


def _table_has_rows(connection: Any, table_name: str) -> bool:
    return connection.exec_driver_sql(f"SELECT 1 FROM {table_name} LIMIT 1").first() is not None


def _configure_sqlite_connection(
    dbapi_connection: Any,
    connection_record: Any,
    *,
    file_backed: bool,
) -> None:
    """Apply, and then *verify*, the durability pragmas on every connection.

    Only ``foreign_keys`` was set here. The order ledger's own SQLite store
    (:mod:`chronos.execution.sqlite_ledger`) has used WAL and
    ``synchronous=FULL`` since Phase 9, but the main Chronos database — which
    holds order intents, confirmations, risk decisions, kill-switch events and,
    from M3, the supervisor's durable state — ran on SQLite's defaults:
    ``journal_mode=delete`` and ``synchronous=NORMAL``.

    That mattered for three separate reasons, and M3 needs all three fixed
    before it can put per-session loss and activity counters in this database:

    - **``synchronous=NORMAL`` can lose committed transactions** on OS crash or
      power loss. A risk counter that silently rolls back is worse than one that
      does not exist, because the system would trust it.
    - **``journal_mode=delete`` blocks readers against a writer**, so the
      dashboard could stall the writer it is reading behind.
    - **No ``busy_timeout`` means an immediate ``database is locked``** rather
      than a short wait, which under the single-writer lease shows up as a
      spurious failure exactly when two processes overlap during a handover.

    The pragmas are **verified, not merely issued**. A pragma that silently
    failed to apply would leave the system believing it has a durability
    guarantee it does not have, which is the same class of defect as a mandate
    ceiling that is set but never read. On a file-backed database a failure
    raises; ``:memory:`` databases legitimately report ``journal_mode=memory``
    and are exempt from the WAL check only.
    """

    del connection_record
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
        journal_mode = _pragma_value(cursor)
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA synchronous")
        synchronous = _pragma_value(cursor)

        if file_backed and str(journal_mode).lower() != "wal":
            raise RuntimeError(
                f"SQLite refused write-ahead logging (journal_mode={journal_mode!r}). "
                "Chronos will not run an order database without it: a reader would block "
                "the writer, and crash recovery is weaker. This usually means the database "
                "is on a filesystem that does not support WAL, such as some network mounts."
            )
        # 2 = FULL, 3 = EXTRA. Anything lower can lose a committed transaction.
        if int(synchronous) < 2:
            raise RuntimeError(
                f"SQLite refused synchronous=FULL (synchronous={synchronous!r}). Chronos "
                "will not run an order database that can lose a committed transaction on "
                "power loss: a risk counter that silently rolls back is worse than none."
            )
    finally:
        cursor.close()


def _pragma_value(cursor: Any) -> Any:
    row = cursor.fetchone()
    return row[0] if row else None


# >>> anchor guard (FU1-GUARD-1)
# The dedicated per-stream anchor guard of spec FU1g5.1 sections 1-5, IN ISOLATION.
#
# NOT WIRED: nothing in Chronos calls this yet. It is not used by the issuance route or the
# drain, and must not be until the drain re-presentation blocker (P1-NEW-22) is closed. It
# performs no anchor-file I/O and derives no path: the caller INJECTS the lock path, and
# the wait deadline is a required argument (the default/maximum, R11, is undecided).
#
# It lives in this module only because the approved production set is three files; the
# cost (a larger module that mixes filesystem locking with engine configuration) is
# disclosed in the FU1-GUARD-1 handoff. Linux only (advisory flock on a local filesystem).

#: Release steps, in the documented release order (the reverse of acquisition).
_ANCHOR_RELEASE_STEPS = (
    "unlock",
    "close_lock_fd",
    "discard_held_key",
    "release_thread_lock",
    "close_dir_fd",
)
_ANCHOR_NAME_MAX = 255
_ANCHOR_FLOCK_POLL_S = 0.01
_ANCHOR_DIR_MODE = 0o700
_ANCHOR_LOCK_MODE = 0o600

_GUARD_REGISTRY_LOCK = threading.Lock()
#: One identity-keyed thread lock per (directory st_dev, st_ino, lock name).
_GUARD_THREAD_LOCKS: dict[tuple[int, int, str], threading.Lock] = {}
_GUARD_HELD = threading.local()
#: Guards whose release could not be established as safe. Strongly referenced, never
#: removed in-process: every later in-process acquisition of the key refuses until exit.
_STRANDED: dict[tuple[int, int, str], AnchorGuard] = {}
_STRANDED_UNKEYED: list[AnchorGuard] = []
_LIVE_GUARDS: weakref.WeakSet[AnchorGuard] = weakref.WeakSet()
#: Test-only seam: called as hook(step, "pre"|"post") immediately before and after each
#: release step's system or library call. A no-op while unset.
_ANCHOR_GUARD_STEP_HOOK: Callable[[str, str], None] | None = None


class AnchorGuardError(RuntimeError):
    """Base class for every typed anchor-guard failure."""


class AnchorGuardRefused(AnchorGuardError):
    """The guard refuses as found: platform, path, ownership, mode, identity or lock support."""


class AnchorGuardTimeout(AnchorGuardError):
    """The bounded wait expired; nothing is held and nothing was mutated."""


class AnchorGuardReentrant(AnchorGuardError):
    """The same thread already holds this guard; it refuses rather than deadlocking."""


class AnchorGuardStranded(AnchorGuardError):
    """A previous release of this identity could not be established as safe (no retry)."""


class AnchorGuardCleanupFailed(AnchorGuardError):
    """An ordinary cleanup failure; primary per R12, the raw error is ``__cause__``."""


@dataclass(frozen=True, slots=True)
class AnchorCleanupFailure:
    step: int
    name: str
    error: BaseException
    asynchronous: bool


@dataclass(frozen=True, slots=True)
class AnchorCleanupRecord:
    """Everything a release collected; attached to the primary exception it raises."""

    lock_path: str
    failures: tuple[AnchorCleanupFailure, ...]
    original: BaseException | None
    still_held: tuple[str, ...]
    state: str


def _anchor_held_keys() -> set[tuple[int, int, str]]:
    held = getattr(_GUARD_HELD, "keys", None)
    if held is None:
        held = set()
        _GUARD_HELD.keys = held
    return held


def _anchor_step_hook(step: str, phase: str) -> None:
    hook = _ANCHOR_GUARD_STEP_HOOK
    if hook is not None:
        hook(step, phase)


class AnchorGuard:
    """Exclusive, bounded, per-stream guard on a stable dedicated lock file.

    Acquisition order (spec section 2.2 as it applies to the guard alone): 0 platform
    predicate, 2 directory descriptor plus validation, 3 key plus the stranded lookup,
    4 same-thread re-entrancy, 5 bounded thread lock, 6 held-key, 7 lock descriptor,
    8 bounded non-blocking flock, 9 identity re-check. Release is the exact reverse and
    runs every step (section 3.1); the state afterwards is derived from what is still
    held. A guard is single-use.
    """

    def __init__(self, lock_path: str | os.PathLike[str], *, wait_s: float) -> None:
        if isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)):
            raise AnchorGuardRefused(
                "wait_s must be a finite, non-negative number of seconds within the "
                "platform lock-timeout bound; "
                "the guard has no default deadline and never clamps one"
            )
        try:
            normalized_wait_s = float(wait_s)
        except (OverflowError, ValueError):
            raise AnchorGuardRefused(
                "wait_s must be a finite, non-negative number of seconds within the "
                "platform lock-timeout bound; the guard has no default deadline and "
                "never clamps one"
            ) from None
        if (
            not math.isfinite(normalized_wait_s)
            or normalized_wait_s < 0
            or normalized_wait_s > threading.TIMEOUT_MAX
        ):
            raise AnchorGuardRefused(
                "wait_s must be a finite, non-negative number of seconds within the "
                "platform lock-timeout bound; the guard has no default deadline and "
                "never clamps one"
            )
        raw = os.fspath(lock_path)
        directory, name = os.path.split(raw)
        if (
            not name
            or name in (".", "..")
            or "\x00" in raw
            or len(os.fsencode(name)) > _ANCHOR_NAME_MAX
        ):
            raise AnchorGuardRefused(f"invalid lock name in {raw!r}")
        self._lock_path = raw
        self._dir_path = directory or "."
        self._lock_name = name
        self._wait_s = normalized_wait_s
        self.state = "NEW"
        self.cleanup_record: AnchorCleanupRecord | None = None
        self._key: tuple[int, int, str] | None = None
        self._dir_fd: int | None = None
        self._lock_fd: int | None = None
        self._flocked = False
        self._thread_lock: threading.Lock | None = None
        self._thread_lock_held = False
        self._held_set: set[tuple[int, int, str]] | None = None
        self._held_key = False
        _LIVE_GUARDS.add(self)

    def __enter__(self) -> AnchorGuard:
        return self.acquire()

    def __exit__(self, *_exc_info: object) -> None:
        self.release()

    def held_resources(self) -> tuple[str, ...]:
        held: list[str] = []
        if self._lock_fd is not None:
            held.append("lock_fd (flock HELD)" if self._flocked else "lock_fd")
        if self._held_key:
            held.append("held-key")
        if self._thread_lock_held:
            held.append("thread lock")
        if self._dir_fd is not None:
            held.append("dir_fd")
        return tuple(held)

    # ------------------------------------------------------------------ acquisition

    def acquire(self) -> AnchorGuard:
        if self.state != "NEW":
            raise AnchorGuardRefused(f"an AnchorGuard is single-use; this one is {self.state}")
        self.state = "ACQUIRING"
        try:
            self._acquire_steps()
        except BaseException:
            self.release()
            raise
        self.state = "HELD"
        return self

    def _acquire_steps(self) -> None:
        if sys.platform != "linux":
            raise AnchorGuardRefused(
                f"the anchor guard is supported on Linux only (sys.platform={sys.platform})"
            )
        if not os.path.isabs(self._lock_path):
            raise AnchorGuardRefused(
                f"the lock path must be absolute, got {self._lock_path!r}: a relative path "
                "resolves per working directory"
            )
        deadline = time.monotonic() + self._wait_s
        self._dir_fd = self._open_directory()
        directory = os.fstat(self._dir_fd)
        key = (directory.st_dev, directory.st_ino, self._lock_name)
        self._key = key
        with _GUARD_REGISTRY_LOCK:
            stranded = _STRANDED.get(key)
            thread_lock = _GUARD_THREAD_LOCKS.setdefault(key, threading.Lock())
        if stranded is not None:
            raise AnchorGuardStranded(
                f"the anchor guard for {self._lock_path} is stranded holding "
                f"{', '.join(stranded.held_resources())}; every in-process acquisition "
                "refuses until the process exits (no retry exists)"
            )
        held = _anchor_held_keys()
        if key in held:
            raise AnchorGuardReentrant(
                f"this thread already holds the anchor guard for {self._lock_path}; "
                "it is not reentrant, so this refuses rather than deadlocking"
            )
        self._thread_lock = thread_lock
        self._thread_lock_held = thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if not self._thread_lock_held:
            raise AnchorGuardTimeout(
                f"another thread in this process holds the anchor guard for "
                f"{self._lock_path}; waited {self._wait_s}s"
            )
        self._held_set = held
        held.add(key)
        self._held_key = True
        self._lock_fd = self._open_lock_file(self._dir_fd)
        self._take_flock(deadline)
        self._recheck_identity()

    def _open_directory(self) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(self._dir_path, flags)
        except OSError as error:
            raise AnchorGuardRefused(
                f"cannot open the lock directory {self._dir_path} without following links: {error}"
            ) from error
        try:
            found = os.fstat(fd)
            mode = stat.S_IMODE(found.st_mode)
            if not stat.S_ISDIR(found.st_mode):
                raise AnchorGuardRefused(f"{self._dir_path} is not a directory")
            if found.st_uid != os.geteuid():
                raise AnchorGuardRefused(
                    f"the lock directory {self._dir_path} is owned by uid {found.st_uid}, "
                    f"not this process's effective user {os.geteuid()}; refused as found"
                )
            if mode != _ANCHOR_DIR_MODE:
                raise AnchorGuardRefused(
                    f"the lock directory {self._dir_path} has mode {mode:04o}; it must be "
                    "exactly 0700 (refused as found, never repaired)"
                )
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _open_lock_file(self, dir_fd: int) -> int:
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        created = False
        try:
            fd = os.open(
                self._lock_name, flags | os.O_CREAT | os.O_EXCL, _ANCHOR_LOCK_MODE, dir_fd=dir_fd
            )
            created = True
        except FileExistsError:
            try:
                fd = os.open(self._lock_name, flags, dir_fd=dir_fd)
            except OSError as error:
                raise AnchorGuardRefused(
                    f"cannot open the lock file {self._lock_path} without following links: {error}"
                ) from error
        except OSError as error:
            raise AnchorGuardRefused(
                f"cannot create the lock file {self._lock_path}: {error}"
            ) from error
        try:
            if created:
                os.fchmod(fd, _ANCHOR_LOCK_MODE)
            found = os.fstat(fd)
            mode = stat.S_IMODE(found.st_mode)
            if not stat.S_ISREG(found.st_mode):
                raise AnchorGuardRefused(f"the lock file {self._lock_path} is not a regular file")
            if found.st_uid != os.geteuid():
                raise AnchorGuardRefused(
                    f"the lock file {self._lock_path} is owned by uid {found.st_uid}, not "
                    f"this process's effective user {os.geteuid()}; refused as found"
                )
            if mode != _ANCHOR_LOCK_MODE:
                raise AnchorGuardRefused(
                    f"the lock file {self._lock_path} has mode {mode:04o}; it must be exactly "
                    "0600 (refused as found, never repaired)"
                )
            if found.st_nlink != 1:
                raise AnchorGuardRefused(
                    f"the lock file {self._lock_path} has {found.st_nlink} links; it must "
                    "have exactly one link"
                )
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _take_flock(self, deadline: float) -> None:
        assert self._lock_fd is not None
        while True:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AnchorGuardTimeout(
                        f"another process holds the anchor guard for {self._lock_path}; "
                        f"waited {self._wait_s}s"
                    ) from None
                time.sleep(min(_ANCHOR_FLOCK_POLL_S, remaining))
                continue
            except OSError as error:
                raise AnchorGuardRefused(
                    f"cannot take an advisory lock on {self._lock_path} ({error}); exclusion "
                    "cannot be verified on this filesystem"
                ) from error
            self._flocked = True
            return

    def _recheck_identity(self) -> None:
        assert self._lock_fd is not None and self._dir_fd is not None
        opened = os.fstat(self._lock_fd)
        try:
            named = os.stat(self._lock_name, dir_fd=self._dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            named = None
        if named is None or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise AnchorGuardRefused(
                f"the lock file {self._lock_path} was replaced after it was opened; "
                "refused by identity"
            )
        directory = os.fstat(self._dir_fd)
        try:
            named_dir = os.stat(self._dir_path, follow_symlinks=False)
        except OSError:
            named_dir = None
        if named_dir is None or (named_dir.st_dev, named_dir.st_ino) != (
            directory.st_dev,
            directory.st_ino,
        ):
            raise AnchorGuardRefused(
                f"the lock directory {self._dir_path} was replaced after it was opened; "
                "refused by identity"
            )

    # ------------------------------------------------------------------ release

    def release(self) -> None:
        """Run every release step; derive the state from what is still held.

        Precedence (R12, R13): the most recently delivered asynchronous exception is
        primary; else an unwinding asynchronous exception stays primary; else the first
        ordinary failure in release order is raised as ``AnchorGuardCleanupFailed``; else
        the caller's original failure, if any, propagates unchanged.
        """

        if self.state in ("RELEASED", "INVALID_AFTER_FORK"):
            return
        if self.state == "NEW":
            self.state = "RELEASED"
            return
        if self.state == "RELEASE_INCOMPLETE":
            raise AnchorGuardStranded(
                f"the anchor guard for {self._lock_path} is stranded holding "
                f"{', '.join(self.held_resources())}; release is not retried"
            )
        if self.state == "RELEASING":
            raise AnchorGuardRefused("release() re-entered during its own release")
        self.state = "RELEASING"
        original = sys.exception()
        failures: list[AnchorCleanupFailure] = []

        def failed(name: str, error: BaseException) -> None:
            failures.append(
                AnchorCleanupFailure(
                    _ANCHOR_RELEASE_STEPS.index(name) + 1,
                    name,
                    error,
                    not isinstance(error, Exception),
                )
            )

        if self._flocked and self._lock_fd is not None:
            try:
                _anchor_step_hook("unlock", "pre")
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._flocked = False
                _anchor_step_hook("unlock", "post")
            except BaseException as error:
                failed("unlock", error)
        if self._lock_fd is not None:
            try:
                _anchor_step_hook("close_lock_fd", "pre")
                fd, self._lock_fd = self._lock_fd, None
                try:
                    os.close(fd)
                except OSError:
                    # Linux frees the descriptor even when close reports an error, so the
                    # field stays cleared and the close is never retried.
                    self._flocked = False
                    raise
                except BaseException:
                    self._lock_fd = fd
                    raise
                self._flocked = False
                _anchor_step_hook("close_lock_fd", "post")
            except BaseException as error:
                failed("close_lock_fd", error)
        if self._held_key and self._held_set is not None:
            try:
                _anchor_step_hook("discard_held_key", "pre")
                self._held_set.discard(self._key)
                self._held_key = False
                _anchor_step_hook("discard_held_key", "post")
            except BaseException as error:
                failed("discard_held_key", error)
        if self._thread_lock_held and self._thread_lock is not None:
            try:
                _anchor_step_hook("release_thread_lock", "pre")
                self._thread_lock.release()
                self._thread_lock_held = False
                _anchor_step_hook("release_thread_lock", "post")
            except BaseException as error:
                failed("release_thread_lock", error)
        if self._dir_fd is not None:
            try:
                _anchor_step_hook("close_dir_fd", "pre")
                fd, self._dir_fd = self._dir_fd, None
                try:
                    os.close(fd)
                except OSError:
                    raise
                except BaseException:
                    self._dir_fd = fd
                    raise
                _anchor_step_hook("close_dir_fd", "post")
            except BaseException as error:
                failed("close_dir_fd", error)

        still_held = self.held_resources()
        if still_held:
            self.state = "RELEASE_INCOMPLETE"
            with _GUARD_REGISTRY_LOCK:
                if self._key is not None:
                    _STRANDED[self._key] = self
                else:
                    _STRANDED_UNKEYED.append(self)
        else:
            self.state = "RELEASED"
        record = AnchorCleanupRecord(
            self._lock_path, tuple(failures), original, still_held, self.state
        )
        self.cleanup_record = record
        asynchronous = [failure.error for failure in failures if failure.asynchronous]
        if asynchronous:
            primary = asynchronous[-1]
            _attach_cleanup_record(primary, record)
            raise primary
        if original is not None and not isinstance(original, Exception):
            _attach_cleanup_record(original, record)
            return
        ordinary = [failure.error for failure in failures if not failure.asynchronous]
        if ordinary:
            cleanup_failed = AnchorGuardCleanupFailed(
                f"releasing the anchor guard for {self._lock_path} failed at "
                f"{', '.join(f.name for f in failures)}; cleanup status {self.state}"
                + (f" (still held: {', '.join(still_held)})" if still_held else "")
            )
            _attach_cleanup_record(cleanup_failed, record)
            raise cleanup_failed from ordinary[0]

    def _invalidate_after_fork(self) -> None:
        for fd in (self._lock_fd, self._dir_fd):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self._lock_fd = None
        self._dir_fd = None
        self._flocked = False
        self._thread_lock = None
        self._thread_lock_held = False
        self._held_set = None
        self._held_key = False
        self.state = "INVALID_AFTER_FORK"


def _attach_cleanup_record(error: BaseException, record: AnchorCleanupRecord) -> None:
    # Diagnostic recording must never prevent propagation (R13). Only an ordinary failure
    # of the attribute write is swallowed; a new asynchronous interruption propagates.
    with contextlib.suppress(Exception):
        error.anchor_cleanup_record = record  # type: ignore[attr-defined]


def _reset_anchor_guards_in_forked_child() -> None:
    """A forked child inherits no usable guard: close its descriptor copies, clear state.

    Closing the child's copy of a lock descriptor does not release the parent's flock,
    which the parent's own descriptor still holds.
    """

    global _GUARD_REGISTRY_LOCK, _GUARD_HELD
    for guard in list(_LIVE_GUARDS):
        guard._invalidate_after_fork()
    _GUARD_REGISTRY_LOCK = threading.Lock()
    _GUARD_HELD = threading.local()
    _GUARD_THREAD_LOCKS.clear()
    _STRANDED.clear()
    _STRANDED_UNKEYED.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_anchor_guards_in_forked_child)
# <<< anchor guard (FU1-GUARD-1)
