from datetime import UTC
from pathlib import Path
from stat import S_IMODE
from typing import NamedTuple, cast

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.pool import StaticPool

from chronos.persistence.database import SCHEMA_VERSION, Database
from chronos.persistence.repositories import ApplicationEventRepository
from chronos.persistence.schema import DatabaseScopeRow, SchemaVersionRow
from chronos.utils.identifiers import account_fingerprint


class _SqliteSnapshot(NamedTuple):
    file_bytes: bytes
    schema: tuple[tuple[object, ...], ...]
    data: tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]
    version: int | None


def _create_empty_v1_schema(database: Database) -> None:
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE schema_version ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "version INTEGER NOT NULL, applied_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO schema_version (version, applied_at) VALUES (1, '2026-01-15 15:30:00')"
        )
        connection.exec_driver_sql(
            "CREATE TABLE wheel_cycles ("
            "id VARCHAR(64) PRIMARY KEY, symbol VARCHAR(32) NOT NULL, "
            "status VARCHAR(32) NOT NULL, opened_at DATETIME NOT NULL, "
            "closed_at DATETIME, notes TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE fills ("
            "execution_id VARCHAR(120) PRIMARY KEY, correlation_id VARCHAR(80), "
            "broker_order_id INTEGER NOT NULL, wheel_cycle_id VARCHAR(64) "
            "REFERENCES wheel_cycles(id), symbol VARCHAR(32) NOT NULL, "
            "contract_id INTEGER NOT NULL, side VARCHAR(8) NOT NULL, "
            "quantity NUMERIC(20, 8) NOT NULL, price NUMERIC(20, 8) NOT NULL, "
            "occurred_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE strategy_basis_entries ("
            "id VARCHAR(64) PRIMARY KEY, wheel_cycle_id VARCHAR(64) NOT NULL "
            "REFERENCES wheel_cycles(id), symbol VARCHAR(32) NOT NULL, "
            "entry_type VARCHAR(48) NOT NULL, amount NUMERIC(20, 8) NOT NULL, "
            "source_execution_id VARCHAR(120) REFERENCES fills(execution_id), "
            "source_note TEXT, occurred_at DATETIME NOT NULL, created_at DATETIME NOT NULL)"
        )


def _snapshot_sqlite_database(database: Database, database_path: Path) -> _SqliteSnapshot:
    with database.engine.connect() as connection:
        schema = tuple(
            tuple(row)
            for row in connection.exec_driver_sql(
                "SELECT type, name, tbl_name, rootpage, sql "
                "FROM sqlite_schema ORDER BY type, name, tbl_name"
            )
        )
        table_names = tuple(
            str(row[0])
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name"
            )
        )
        data = tuple(
            (
                table_name,
                tuple(
                    tuple(row)
                    for row in connection.exec_driver_sql(
                        f'SELECT * FROM "{table_name}" ORDER BY rowid'
                    )
                ),
            )
            for table_name in table_names
        )
        version = (
            cast(
                int | None,
                connection.scalar(text("SELECT MAX(version) FROM schema_version")),
            )
            if "schema_version" in table_names
            else None
        )
    return _SqliteSnapshot(database_path.read_bytes(), schema, data, version)


def _assert_initialize_refuses_without_mutation(
    database: Database,
    database_path: Path,
    *,
    expected_version: int | None,
    expected_error: str,
) -> None:
    before = _snapshot_sqlite_database(database, database_path)
    assert before.version == expected_version

    with pytest.raises(RuntimeError) as error:
        database.initialize()

    message = str(error.value)
    assert expected_error in message
    assert "Preserve and back up" in message
    assert "fresh DATABASE_URL" in message
    after = _snapshot_sqlite_database(database, database_path)
    assert after.file_bytes == before.file_bytes
    assert after.schema == before.schema
    assert after.data == before.data
    assert after.version == before.version


def _replace_strategy_state_schema(
    database: Database,
    *,
    include_unique_constraint: bool,
    include_foreign_key: bool,
) -> None:
    unique_constraint = (
        ", CONSTRAINT uq_strategy_state_symbol UNIQUE (symbol)" if include_unique_constraint else ""
    )
    foreign_key = (
        ", FOREIGN KEY (wheel_cycle_id) REFERENCES wheel_cycles (id)" if include_foreign_key else ""
    )
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE strategy_state")
        connection.exec_driver_sql(
            "CREATE TABLE strategy_state ("
            "id INTEGER NOT NULL, symbol VARCHAR(32) NOT NULL, "
            "wheel_cycle_id VARCHAR(64), wheel_stage VARCHAR(32) NOT NULL, "
            "broker_average_cost NUMERIC(20, 8), "
            "strategy_adjusted_basis NUMERIC(20, 8), "
            "reconciliation_status VARCHAR(32) NOT NULL, updated_at DATETIME NOT NULL, "
            "PRIMARY KEY (id)" + unique_constraint + foreign_key + ")"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_strategy_state_symbol ON strategy_state (symbol)"
        )


def test_schema_initialization_creates_required_evidence_tables() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        database.initialize()
        names = set(inspect(database.engine).get_table_names())
    finally:
        database.dispose()

    assert {
        "application_events",
        "candidate_evaluations",
        "commissions",
        "database_scope",
        "fills",
        "guardrail_decisions",
        "order_drafts",
        "order_previews",
        "reconciliation_runs",
        "rejected_candidate_reasons",
        "schema_version",
        "strategy_basis_entries",
        "strategy_state",
        "submitted_orders",
        "wheel_cycles",
        "order_events",
        "risk_decisions",
        "risk_check_results",
        # Autonomy supervisor durable state (v5).
        "hash_chain_records",
        "autonomy_mandate_activations",
        "autonomy_session_counters",
        "autonomy_decision_attempts",
        "autonomy_proposal_queue",
        # The per-job evidence-bundle record (v9, ADR-0028, migration 0008).
        "autonomy_evidence_bundles",
        # A3: durable proposer revocation, the ledger both planes consult (v10).
        "autonomy_proposer_revocations",
    } <= names
    # v8: autonomy_proposal_queue gains proposer_id (ADR-0023, migration 0007).
    # v9: autonomy_evidence_bundles arrives (ADR-0028, migration 0008).
    # v10: autonomy_proposer_revocations (A3, migration 0009).
    assert SCHEMA_VERSION == 10


def test_application_events_are_append_only_and_queryable() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        database.bind_scope(
            broker_mode="demo",
            environment="DEMO",
            account_id="DU1234567",
        )
        repository = ApplicationEventRepository(database.sessions)
        event_id = repository.append(
            event_type="demo_started",
            message="Deterministic demo started",
            event_data={"mode": "demo"},
        )
        events = repository.recent()
    finally:
        database.dispose()

    assert event_id > 0
    assert len(events) == 1
    assert events[0].event_type == "demo_started"
    assert events[0].occurred_at.tzinfo is UTC


def test_sqlite_schema_timestamps_round_trip_as_aware_utc() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        with database.sessions() as session:
            applied_at = session.scalar(select(SchemaVersionRow.applied_at))
    finally:
        database.dispose()

    assert applied_at is not None
    assert applied_at.tzinfo is UTC


def test_empty_v1_database_is_refused_without_mutation(tmp_path: Path) -> None:
    database_path = tmp_path / "empty-v1.db"
    database = Database(f"sqlite+pysqlite:///{database_path}")
    try:
        _create_empty_v1_schema(database)
        _assert_initialize_refuses_without_mutation(
            database,
            database_path,
            expected_version=1,
            expected_error="Unsupported Chronos schema version 1",
        )
    finally:
        database.dispose()


def test_populated_v1_database_is_refused_without_mutation(tmp_path: Path) -> None:
    database_path = tmp_path / "populated-v1.db"
    database = Database(f"sqlite+pysqlite:///{database_path}")
    try:
        _create_empty_v1_schema(database)
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO wheel_cycles "
                "(id, symbol, status, opened_at) "
                "VALUES ('CYCLE-1', 'AAPL', 'OPEN', '2026-01-15 15:30:00')"
            )
            connection.exec_driver_sql(
                "INSERT INTO strategy_basis_entries "
                "(id, wheel_cycle_id, symbol, entry_type, amount, source_note, "
                "occurred_at, created_at) VALUES "
                "('BASIS-LEGACY', 'CYCLE-1', 'AAPL', 'MANUAL_ADJUSTMENT', 1, "
                "'legacy', '2026-01-15 15:30:00', '2026-01-15 15:30:00')"
            )
        _assert_initialize_refuses_without_mutation(
            database,
            database_path,
            expected_version=1,
            expected_error="Unsupported Chronos schema version 1",
        )
    finally:
        database.dispose()


def test_v1_database_with_foreign_key_orphan_is_refused_without_mutation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "orphaned-v1.db"
    database = Database(f"sqlite+pysqlite:///{database_path}")
    try:
        _create_empty_v1_schema(database)
        with database.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.exec_driver_sql(
                "INSERT INTO fills "
                "(execution_id, broker_order_id, wheel_cycle_id, symbol, contract_id, "
                "side, quantity, price, occurred_at) VALUES "
                "('EXEC-ORPHAN', 1, 'MISSING-CYCLE', 'AAPL', 100, "
                "'SELL', 1, 2, '2026-01-15 15:30:00')"
            )
            connection.commit()

        with database.engine.connect() as connection:
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        assert violations
        assert violations[0][0] == "fills"
        _assert_initialize_refuses_without_mutation(
            database,
            database_path,
            expected_version=1,
            expected_error="Unsupported Chronos schema version 1",
        )
    finally:
        database.dispose()


def test_unversioned_populated_database_is_refused_without_mutation(tmp_path: Path) -> None:
    database_path = tmp_path / "foreign.db"
    database = Database(f"sqlite+pysqlite:///{database_path}")
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE foreign_records (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO foreign_records (id, value) VALUES (1, 'preserve me')"
            )

        _assert_initialize_refuses_without_mutation(
            database,
            database_path,
            expected_version=None,
            expected_error="unversioned database containing existing tables: foreign_records",
        )
    finally:
        database.dispose()


def test_current_schema_missing_table_is_refused_without_mutation(tmp_path: Path) -> None:
    database_path = tmp_path / "missing-table.db"
    database = Database(f"sqlite+pysqlite:///{database_path}")
    try:
        database.initialize()
        with database.engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE guardrail_decisions")

        _assert_initialize_refuses_without_mutation(
            database,
            database_path,
            expected_version=SCHEMA_VERSION,
            expected_error="missing tables: guardrail_decisions",
        )
    finally:
        database.dispose()


def test_current_schema_missing_required_column_is_refused_without_mutation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "missing-column.db"
    database = Database(f"sqlite+pysqlite:///{database_path}")
    try:
        database.initialize()
        with database.engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE database_scope")
            connection.exec_driver_sql(
                "CREATE TABLE database_scope ("
                "id INTEGER NOT NULL PRIMARY KEY, broker_mode VARCHAR(16) NOT NULL, "
                "environment VARCHAR(16) NOT NULL, bound_at DATETIME NOT NULL)"
            )

        _assert_initialize_refuses_without_mutation(
            database,
            database_path,
            expected_version=SCHEMA_VERSION,
            expected_error="database_scope missing columns: account_fingerprint",
        )
    finally:
        database.dispose()


def test_current_schema_missing_unique_constraint_is_refused_without_mutation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "missing-constraint.db"
    database = Database(f"sqlite+pysqlite:///{database_path}")
    try:
        database.initialize()
        _replace_strategy_state_schema(
            database,
            include_unique_constraint=False,
            include_foreign_key=True,
        )

        _assert_initialize_refuses_without_mutation(
            database,
            database_path,
            expected_version=SCHEMA_VERSION,
            expected_error="strategy_state unique constraints do not match",
        )
    finally:
        database.dispose()


def test_current_schema_missing_foreign_key_is_refused_without_mutation(tmp_path: Path) -> None:
    database_path = tmp_path / "missing-foreign-key.db"
    database = Database(f"sqlite+pysqlite:///{database_path}")
    try:
        database.initialize()
        _replace_strategy_state_schema(
            database,
            include_unique_constraint=True,
            include_foreign_key=False,
        )

        _assert_initialize_refuses_without_mutation(
            database,
            database_path,
            expected_version=SCHEMA_VERSION,
            expected_error="strategy_state foreign keys do not match",
        )
    finally:
        database.dispose()


@pytest.mark.parametrize(
    ("table_name", "statements"),
    [
        (
            "wheel_cycles",
            (
                "INSERT INTO wheel_cycles (id, symbol, status, opened_at) "
                "VALUES ('CYCLE-1', 'AAPL', 'OPEN', '2026-01-15 15:30:00')",
            ),
        ),
        (
            "strategy_state",
            (
                "INSERT INTO strategy_state "
                "(symbol, wheel_stage, reconciliation_status, updated_at) "
                "VALUES ('AAPL', 'FLAT', 'PENDING', '2026-01-15 15:30:00')",
            ),
        ),
        (
            "candidate_evaluations",
            (
                "INSERT INTO candidate_evaluations "
                "(id, symbol, outcome, input_snapshot, ranking_snapshot, created_at) "
                "VALUES ('EVAL-1', 'AAPL', 'NO_TRADE', '{}', '{}', "
                "'2026-01-15 15:30:00')",
            ),
        ),
        (
            "order_drafts",
            (
                "INSERT INTO order_drafts "
                "(correlation_id, account_id_masked, symbol, contract_id, intent, quantity, "
                "limit_price, lifecycle, created_at, updated_at) VALUES "
                "('CORR-1', '***4567', 'AAPL', 100, 'OPEN_SHORT_PUT', 1, 1, 'DRAFT', "
                "'2026-01-15 15:30:00', '2026-01-15 15:30:00')",
            ),
        ),
        (
            "fills",
            (
                "INSERT INTO fills "
                "(execution_id, broker_order_id, symbol, contract_id, security_type, side, "
                "quantity, price, multiplier, currency, account_fingerprint, occurred_at) "
                f"VALUES ('EXEC-1', 1, 'AAPL', 100, 'OPT', 'SELL', 1, 2, 100, 'USD', "
                f"'{account_fingerprint('DU1234567')}', '2026-01-15 15:30:00')",
            ),
        ),
        (
            "strategy_basis_entries",
            (
                "INSERT INTO wheel_cycles (id, symbol, status, opened_at) "
                "VALUES ('CYCLE-1', 'AAPL', 'OPEN', '2026-01-15 15:30:00')",
                "INSERT INTO strategy_basis_entries "
                "(id, wheel_cycle_id, symbol, entry_type, amount, account_fingerprint, "
                "currency, provisional, reconciliation_status, source_note, occurred_at, "
                f"created_at) VALUES ('BASIS-1', 'CYCLE-1', 'AAPL', 'MANUAL_ADJUSTMENT', 1, "
                f"'{account_fingerprint('DU1234567')}', 'USD', 0, 'RECONCILED', 'manual', "
                "'2026-01-15 15:30:00', '2026-01-15 15:30:00')",
            ),
        ),
        (
            "reconciliation_runs",
            (
                "INSERT INTO reconciliation_runs "
                "(id, trigger, status, broker_snapshot, decisions, started_at) "
                "VALUES ('RUN-1', 'STARTUP', 'PENDING', '{}', '[]', "
                "'2026-01-15 15:30:00')",
            ),
        ),
        (
            "application_events",
            (
                "INSERT INTO application_events "
                "(event_type, severity, message, event_data, occurred_at) "
                "VALUES ('startup', 'INFO', 'started', '{}', '2026-01-15 15:30:00')",
            ),
        ),
        (
            "guardrail_decisions",
            (
                "INSERT INTO guardrail_decisions "
                "(correlation_id, guardrail, passed, explanation, evidence, decided_at) "
                "VALUES ('CORR-1', 'scope', 1, 'ok', '{}', '2026-01-15 15:30:00')",
            ),
        ),
    ],
)
def test_first_scope_binding_refuses_every_class_of_unscoped_account_data(
    table_name: str,
    statements: tuple[str, ...],
) -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        with database.engine.begin() as connection:
            for statement in statements:
                connection.exec_driver_sql(statement)

        with pytest.raises(RuntimeError, match=table_name):
            database.bind_scope(
                broker_mode="demo",
                environment="DEMO",
                account_id="DU1234567",
            )
    finally:
        database.dispose()


@pytest.mark.parametrize("preexisting", [False, True])
def test_sqlite_file_permissions_are_restricted_after_create_open_and_initialize(
    tmp_path: Path,
    preexisting: bool,
) -> None:
    database_path = tmp_path / "chronos.db"
    if preexisting:
        database_path.touch(mode=0o666)
        database_path.chmod(0o666)

    database = Database(f"sqlite+pysqlite:///{database_path}")
    try:
        assert S_IMODE(database_path.stat().st_mode) == 0o600
        database.initialize()
        assert S_IMODE(database_path.stat().st_mode) == 0o600
    finally:
        database.dispose()


def test_sqlite_file_uri_database_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="SQLite file: URI DATABASE_URL targets are not supported"):
        Database("sqlite+pysqlite:///file:chronos.db?uri=true")


def test_preexisting_sqlite_sidecar_permissions_are_restricted(tmp_path: Path) -> None:
    database_path = tmp_path / "sidecars.db"
    initial_database = Database(f"sqlite+pysqlite:///{database_path}")
    try:
        initial_database.initialize()
    finally:
        initial_database.dispose()

    sidecar_paths = tuple(
        Path(f"{database_path}{suffix}") for suffix in ("-wal", "-shm", "-journal")
    )
    for sidecar_path in sidecar_paths:
        sidecar_path.touch(mode=0o666)
        sidecar_path.chmod(0o666)

    reopened_database = Database(f"sqlite+pysqlite:///{database_path}")
    try:
        assert all(S_IMODE(path.stat().st_mode) == 0o600 for path in sidecar_paths)
    finally:
        reopened_database.dispose()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_preexisting_sqlite_main_and_sidecar_symlinks_are_rejected(
    tmp_path: Path,
    suffix: str,
) -> None:
    database_path = tmp_path / "symlink.db"
    if suffix:
        database_path.touch()
    target_path = tmp_path / f"target{suffix or '-main'}"
    target_path.write_bytes(b"must remain untouched")
    unsafe_path = Path(f"{database_path}{suffix}")
    unsafe_path.symlink_to(target_path)

    with pytest.raises(RuntimeError, match="Refusing symbolic-link SQLite path"):
        Database(f"sqlite+pysqlite:///{database_path}")

    assert unsafe_path.is_symlink()
    assert target_path.read_bytes() == b"must remain untouched"


def test_database_scope_is_pseudonymous_and_fails_closed_on_mismatch() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        database.bind_scope(
            broker_mode="demo",
            environment="DEMO",
            account_id="DU1234567",
        )
        database.bind_scope(
            broker_mode="demo",
            environment="DEMO",
            account_id="DU1234567",
        )
        with database.sessions() as session:
            scope = session.get(DatabaseScopeRow, 1)

        with pytest.raises(RuntimeError, match="different broker scope"):
            database.bind_scope(
                broker_mode="ibkr",
                environment="PAPER",
                account_id="DU7654321",
            )
    finally:
        database.dispose()

    assert scope is not None
    assert scope.account_fingerprint != "DU1234567"
    assert len(scope.account_fingerprint) == 64


def test_sqlite_foreign_keys_are_enabled() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        with database.engine.connect() as connection:
            enabled = connection.scalar(text("PRAGMA foreign_keys"))
    finally:
        database.dispose()

    assert enabled == 1


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+pysqlite:///:memory:",
        "sqlite+pysqlite:///:memory:?cache=shared",
        "sqlite+pysqlite://",
    ],
)
def test_all_parsed_sqlite_memory_urls_use_one_shared_pool(url: str) -> None:
    database = Database(url)
    try:
        assert isinstance(database.engine.pool, StaticPool)
        database.initialize()
        with database.engine.connect() as connection:
            assert "schema_version" in set(inspect(connection).get_table_names())
    finally:
        database.dispose()


# --- durability pragmas (M3 prerequisite) ------------------------------------
#
# The order ledger's own SQLite store has used WAL + synchronous=FULL since
# Phase 9; the main Chronos database ran on SQLite's defaults. M3 puts the
# supervisor's durable loss and activity counters in this database, and a
# counter that can silently roll back after a commit is worse than no counter,
# because the system would trust it.


def test_a_file_database_uses_write_ahead_logging(tmp_path: Path) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'wal.db'}")
    try:
        database.initialize()
        with database.engine.connect() as connection:
            mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
        assert str(mode).lower() == "wal"
    finally:
        database.dispose()


def test_every_connection_commits_durably(tmp_path: Path) -> None:
    """synchronous=FULL is per-connection, so it must be set on each one.

    Unlike journal_mode, this pragma is not persisted in the database file. A
    version that set it once at creation would leave every later connection --
    including the one that actually writes a risk counter -- back on NORMAL.
    """

    database = Database(f"sqlite+pysqlite:///{tmp_path / 'sync.db'}")
    try:
        database.initialize()
        for _ in range(3):
            with database.engine.connect() as connection:
                # 2 = FULL, 3 = EXTRA. Anything lower can lose a committed txn.
                assert int(connection.exec_driver_sql("PRAGMA synchronous").scalar() or 0) >= 2
    finally:
        database.dispose()


def test_a_locked_database_waits_rather_than_failing_immediately(tmp_path: Path) -> None:
    """Without busy_timeout, a brief writer overlap is an instant hard error."""

    database = Database(f"sqlite+pysqlite:///{tmp_path / 'busy.db'}")
    try:
        database.initialize()
        with database.engine.connect() as connection:
            assert int(connection.exec_driver_sql("PRAGMA busy_timeout").scalar() or 0) > 0
    finally:
        database.dispose()


def test_an_in_memory_database_is_exempt_from_wal_but_not_from_durability() -> None:
    """`:memory:` legitimately reports journal_mode=memory; it must still open.

    Guard against a fail-closed check that is too eager: every test in this
    suite, and the demo path, uses an in-memory database.
    """

    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() is not None
    finally:
        database.dispose()


def test_the_durability_pragmas_are_verified_not_merely_issued() -> None:
    """A pragma that silently failed to apply must raise, not be assumed.

    This is the same class of defect as a mandate ceiling that is set and never
    read: the system would believe it has a guarantee it does not have.
    """

    from chronos.persistence.database import _configure_sqlite_connection

    class _Cursor:
        """A cursor on a filesystem that quietly refuses WAL and weak sync."""

        def __init__(self, journal_mode: str, synchronous: int) -> None:
            self._journal_mode = journal_mode
            self._synchronous = synchronous
            self._last = ""

        def execute(self, sql: str) -> None:
            self._last = sql

        def fetchone(self) -> tuple[object, ...]:
            if self._last.startswith("PRAGMA journal_mode"):
                return (self._journal_mode,)
            return (self._synchronous,)

        def close(self) -> None:
            return None

    class _Connection:
        def __init__(self, journal_mode: str, synchronous: int) -> None:
            self._journal_mode = journal_mode
            self._synchronous = synchronous

        def cursor(self) -> _Cursor:
            return _Cursor(self._journal_mode, self._synchronous)

    with pytest.raises(RuntimeError, match="refused write-ahead logging"):
        _configure_sqlite_connection(_Connection("delete", 2), None, file_backed=True)

    with pytest.raises(RuntimeError, match="refused synchronous=FULL"):
        _configure_sqlite_connection(_Connection("wal", 1), None, file_backed=True)

    # Guard the guard: the healthy combination must NOT raise, or the check
    # above would pass for the wrong reason.
    _configure_sqlite_connection(_Connection("wal", 2), None, file_backed=True)
