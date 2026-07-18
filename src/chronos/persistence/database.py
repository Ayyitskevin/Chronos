"""SQLite engine creation and explicit schema initialization."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, UniqueConstraint, create_engine, event, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from chronos.persistence.schema import Base, DatabaseScopeRow, SchemaVersionRow
from chronos.utils.identifiers import account_fingerprint

SCHEMA_VERSION = 4

_DATABASE_RECOVERY_GUIDANCE = (
    "Preserve and back up this database first. A prior-version schema can be upgraded with "
    "`alembic upgrade head` (see alembic.ini); a drifted or unversioned schema cannot — "
    "configure a fresh DATABASE_URL instead. Chronos never modifies such a database itself."
)
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

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
        engine_kwargs: dict[str, object] = {}
        if is_sqlite:
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        if is_sqlite and configured_url.database in {None, "", ":memory:"}:
            engine_kwargs["poolclass"] = StaticPool
        self.engine: Engine = create_engine(url, **engine_kwargs)
        if is_sqlite:
            event.listen(self.engine, "connect", _enable_sqlite_foreign_keys)
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


def _enable_sqlite_foreign_keys(
    dbapi_connection: Any,
    connection_record: Any,
) -> None:
    del connection_record
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()
