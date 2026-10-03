"""SQLite engine creation and explicit schema initialization."""

from __future__ import annotations

import errno
import os
import secrets
import stat
import threading
from collections.abc import Callable
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

# >>> R-21 startup repair window (DBLOCK)
# POSIX record locks belong to the (process, file) pair, and close() of ANY descriptor for a
# file releases every lock the process holds on it. Re-opening a database or sidecar by path
# while this process holds it through SQLite therefore silently drops that connection's WAL
# locks, and another process can then delete the live WAL under it (P1-NEW-23: acknowledged
# commits lost). So files may be opened to be repaired only during the FIRST file-backed
# Database construction in a process: the window closes permanently, under the lock, before
# that construction may create an engine, and is never reopened (not by dispose(), a failed
# constructor or a fork). Afterwards every check is lstat-only and refuses what it would
# have repaired: a restart repairs.
_REPAIR_WINDOW_LOCK = threading.Lock()
_REPAIR_WINDOW_OPEN = True
_RESTART_TO_REPAIR = (
    "restart the process to repair: the startup repair window closed during this process's "
    "first file-backed Database construction, and re-opening a database file it may hold "
    "through SQLite would drop that connection's locks"
)
#: Test-only seam: called as hook(step) at "decided" (the window state was read, the lock is
#: held) and "linked" (a new database file was linked into place). A no-op while unset.
_ADMISSION_STEP_HOOK: Callable[[str], None] | None = None


def _admission_step(step: str) -> None:
    hook = _ADMISSION_STEP_HOOK
    if hook is not None:
        hook(step)


def _reset_repair_window_lock_in_child() -> None:
    """A forked child gets a fresh lock; the window STATE is inherited unchanged.

    A sibling thread holding the lock at fork() does not exist in the child and could never
    release the inherited copy.
    """

    global _REPAIR_WINDOW_LOCK
    _REPAIR_WINDOW_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_repair_window_lock_in_child)
# <<< R-21 startup repair window (DBLOCK)

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
        self._refused = False
        # R-21 is a startup check. During the first Database construction for file-backed
        # SQLite in this process, existing database and sidecar paths are checked and may
        # have their mode tightened. Before that construction is allowed to create an
        # engine, the process-wide repair window closes permanently under the module lock,
        # whether or not engine creation or the first connection later succeeds. From then
        # on every Database in the process only checks these files with lstat and refuses
        # any problem; nothing is repaired until the process restarts. The checks reject a
        # symbolic link, a non-regular file or a file owned by another user BEFORE any
        # connection is opened, so SQLite is not handed a link already present at startup.
        #
        # It gives no protection against concurrent changes to these paths or their
        # directories by any actor. SQLite opens by pathname, so such a change can redirect
        # what it opens. Connections that other components open directly with sqlite3 are
        # outside this check.
        self._admit_sqlite_files()
        engine_kwargs: dict[str, object] = {}
        if is_sqlite:
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        if is_sqlite and configured_url.database in {None, "", ":memory:"}:
            engine_kwargs["poolclass"] = StaticPool
        self.engine: Engine = create_engine(url, **engine_kwargs)
        if is_sqlite:
            event.listen(self.engine, "connect", self._refuse_if_refused)
            event.listen(
                self.engine,
                "connect",
                partial(_configure_sqlite_connection, file_backed=self._sqlite_path is not None),
            )
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False, class_=Session)
        if self._sqlite_path is not None:
            try:
                with self.engine.connect():
                    pass
                self._verify_sqlite_files()
            except BaseException:
                self._refuse()
                raise

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
            self._verify_sqlite_files_or_refuse()
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
        self._verify_sqlite_files_or_refuse()

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

    def _sqlite_namespace(self) -> tuple[Path, ...]:
        assert self._sqlite_path is not None
        return (
            self._sqlite_path,
            *(Path(f"{self._sqlite_path}{suffix}") for suffix in _SQLITE_SIDECAR_SUFFIXES),
        )

    def _admit_sqlite_files(self) -> None:
        """Prepare, check and (only while the window is open) repair, then close the window."""

        global _REPAIR_WINDOW_OPEN
        if self._sqlite_path is None:
            return
        with _REPAIR_WINDOW_LOCK:
            try:
                self._prepare_sqlite_parent(self._sqlite_path)
                _create_private_sqlite_file(self._sqlite_path)
                repair = _REPAIR_WINDOW_OPEN
                _admission_step("decided")
                namespace = self._sqlite_namespace()
                # Identity first, across every path, so a link refusal is never masked by a
                # mode refusal on another path.
                for path in namespace:
                    _check_sqlite_file_identity(path)
                for path in namespace:
                    if repair:
                        _secure_sqlite_file(path)
                    else:
                        _check_sqlite_file_mode(path)
            finally:
                _REPAIR_WINDOW_OPEN = False

    def _verify_sqlite_files(self) -> None:
        """lstat-only re-check after a connection exists: never opens, never repairs."""

        if self._sqlite_path is None:
            return
        namespace = self._sqlite_namespace()
        for path in namespace:
            _check_sqlite_file_identity(path)
        for path in namespace:
            _check_sqlite_file_mode(path)

    def _verify_sqlite_files_or_refuse(self) -> None:
        try:
            self._verify_sqlite_files()
        except BaseException:
            self._refuse()
            raise

    def _refuse(self) -> None:
        """Fail closed: no new connection, and the pooled ones are closed through SQLite."""

        self._refused = True
        self.engine.dispose()

    def _refuse_if_refused(self, dbapi_connection: Any, _connection_record: Any) -> None:
        if self._refused:
            dbapi_connection.close()
            raise RuntimeError(
                "this Database refused its database files; construct a new one after the "
                "problem is fixed"
            )

    @staticmethod
    def _prepare_sqlite_parent(path: Path | None) -> None:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)


def _create_private_sqlite_file(path: Path) -> None:
    """Create a missing database 0600 without ever closing a published name.

    A descriptor closed on the final name could belong to a file another thread's SQLite
    connection has just opened; so the file is created under an unpublished random name,
    closed, and only then linked into place (no-replace) and the temporary name removed.
    """

    try:
        os.lstat(path)
        return
    except FileNotFoundError:
        pass
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        temporary = f".{path.name}.create-{secrets.token_hex(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            _admission_step("linked")
        except FileExistsError:
            pass  # created meanwhile; checked as an existing file
        finally:
            os.unlink(temporary, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _check_sqlite_file_identity(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError(f"Refusing symbolic-link SQLite path: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"Refusing non-regular SQLite path: {path}")
    if metadata.st_uid != os.geteuid():
        raise RuntimeError(f"Refusing SQLite path not owned by this user: {path}")
    if metadata.st_nlink != 1:
        # SQLite keys a database's -wal/-shm by PATHNAME, so a second name for the same file
        # gets a second WAL namespace and acknowledged writes fork (DBLOCK-2 review, P1).
        stray = _interrupted_create_stray(path, metadata)
        if stray is not None:
            raise RuntimeError(
                f"Refusing SQLite path with {metadata.st_nlink} hard links: {path}; an "
                f"interrupted Chronos create left the private temporary {stray} linked to "
                "it. Check that file, remove exactly it, then restart"
            )
        raise RuntimeError(
            f"Refusing SQLite path with {metadata.st_nlink} hard links: {path}; a database "
            "file or sidecar must have exactly one name, because SQLite gives each name its "
            "own WAL files"
        )


def _interrupted_create_stray(path: Path, metadata: os.stat_result) -> Path | None:
    """The `.<name>.create-<hex>` temp of _create_private_sqlite_file sharing this inode.

    Only NAMED in the refusal; never removed automatically (a name pattern is not proof the
    stray is ours).
    """

    prefix = f".{path.name}.create-"
    try:
        entries = list(os.scandir(path.parent))
    except OSError:
        return None
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        try:
            found = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if (found.st_dev, found.st_ino) == (metadata.st_dev, metadata.st_ino):
            return path.parent / entry.name
    return None


def _check_sqlite_file_mode(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError(
            f"Refusing SQLite path with mode {stat.S_IMODE(metadata.st_mode):04o} (group or "
            f"other access): {path}; {_RESTART_TO_REPAIR}"
        )


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
