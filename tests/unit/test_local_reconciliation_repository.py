from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event, func, insert, select, update

from chronos.domain.enums import (
    BasisEntryType,
    OrderIntent,
    OrderLifecycle,
    OrderSide,
    ReconciliationStatus,
    SecurityType,
    WheelStage,
)
from chronos.domain.models import BrokerOrderIdentity
from chronos.persistence.database import Database
from chronos.persistence.repositories import LocalReconciliationRepository
from chronos.persistence.schema import (
    ApplicationEventRow,
    BasisEntryRow,
    DatabaseScopeRow,
    FillRow,
    OrderDraftRow,
    StrategyStateRow,
    SubmittedOrderRow,
    WheelCycleRow,
)
from chronos.utils.identifiers import account_fingerprint

NOW = datetime(2026, 1, 15, 15, 30, tzinfo=UTC)
ACCOUNT_ID = "DU1234567"
OTHER_ACCOUNT_ID = "DU7654321"
ACCOUNT_FINGERPRINT = account_fingerprint(ACCOUNT_ID)


def _database(*, url: str = "sqlite+pysqlite:///:memory:") -> Database:
    database = Database(url)
    database.initialize()
    database.bind_scope(broker_mode="demo", environment="DEMO", account_id=ACCOUNT_ID)
    return database


def _seed_complete_active_order_evidence(database: Database) -> None:
    with database.sessions.begin() as session:
        session.add(
            WheelCycleRow(
                id="CYCLE-1",
                symbol="AAPL",
                status="OPEN",
                opened_at=NOW,
            )
        )
        session.flush()
        session.add(
            StrategyStateRow(
                symbol="AAPL",
                wheel_cycle_id="CYCLE-1",
                wheel_stage=WheelStage.SHORT_PUT_PENDING.value,
                reconciliation_status=ReconciliationStatus.PENDING.value,
                updated_at=NOW,
            )
        )
        session.add(
            OrderDraftRow(
                correlation_id="CORR-1",
                wheel_cycle_id="CYCLE-1",
                account_id_masked="****4567",
                symbol="AAPL",
                contract_id=101,
                intent=OrderIntent.OPEN_SHORT_PUT.value,
                quantity=1,
                limit_price=Decimal("2.00"),
                lifecycle=OrderLifecycle.SUBMITTED.value,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        session.add(
            SubmittedOrderRow(
                correlation_id="CORR-1",
                broker_order_id=11,
                permanent_id=9001,
                client_id=17,
                order_ref="CHR-PUT-11",
                lifecycle=OrderLifecycle.SUBMITTED.value,
                submitted_at=NOW,
            )
        )
        session.flush()
        session.add(
            FillRow(
                execution_id="EXEC-1",
                correlation_id="CORR-1",
                broker_order_id=11,
                wheel_cycle_id="CYCLE-1",
                symbol="AAPL",
                contract_id=101,
                security_type=SecurityType.OPTION.value,
                side=OrderSide.SELL.value,
                quantity=Decimal("1"),
                price=Decimal("2.00"),
                multiplier=Decimal("100"),
                currency="USD",
                account_fingerprint=ACCOUNT_FINGERPRINT,
                occurred_at=NOW,
            )
        )
        session.flush()
        session.add(
            BasisEntryRow(
                id="BASIS-1",
                wheel_cycle_id="CYCLE-1",
                symbol="AAPL",
                entry_type=BasisEntryType.OPENING_OPTION_PREMIUM.value,
                amount=Decimal("-200"),
                account_fingerprint=ACCOUNT_FINGERPRINT,
                contract_id=101,
                security_type=SecurityType.OPTION.value,
                source_side=OrderSide.SELL.value,
                quantity=Decimal("1"),
                unit_price=Decimal("2.00"),
                multiplier=Decimal("100"),
                currency="USD",
                provisional=False,
                reconciliation_status=ReconciliationStatus.RECONCILED.value,
                source_execution_id="EXEC-1",
                occurred_at=NOW,
                created_at=NOW,
            )
        )


def test_empty_scoped_database_is_a_complete_local_read() -> None:
    database = _database()
    try:
        evidence = LocalReconciliationRepository(database.sessions).read(ACCOUNT_ID)
    finally:
        database.dispose()

    assert evidence.active_order_identities == frozenset()
    assert evidence.unresolved_symbols == frozenset()
    assert evidence.complete is True
    assert evidence.reasons == ()


def test_valid_local_history_is_complete_but_conservatively_unresolved() -> None:
    database = _database()
    try:
        _seed_complete_active_order_evidence(database)

        evidence = LocalReconciliationRepository(database.sessions).read(ACCOUNT_ID)
    finally:
        database.dispose()

    assert evidence.active_order_identities == frozenset(
        {
            BrokerOrderIdentity(
                account_id=ACCOUNT_ID,
                client_id=17,
                broker_order_id=11,
                permanent_id=9001,
                order_ref="CHR-PUT-11",
                symbol="AAPL",
                contract_id=101,
                side=OrderSide.SELL,
                quantity=Decimal("1"),
            )
        }
    )
    assert evidence.unresolved_symbols == frozenset({"AAPL"})
    assert evidence.complete is True
    assert evidence.reasons == (
        "Local strategy evidence exists but is conservatively unresolved by this reader.",
    )


def test_every_ownership_bearing_table_contributes_its_canonical_symbol() -> None:
    database = _database()
    try:
        with database.sessions.begin() as session:
            session.add_all(
                [
                    WheelCycleRow(
                        id="CYCLE-AAPL",
                        symbol="AAPL",
                        status="OPEN",
                        opened_at=NOW,
                    ),
                    WheelCycleRow(
                        id="CYCLE-AMD",
                        symbol="AMD",
                        status="OPEN",
                        opened_at=NOW,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    StrategyStateRow(
                        symbol="MSFT",
                        wheel_stage=WheelStage.FLAT.value,
                        reconciliation_status=ReconciliationStatus.PENDING.value,
                        updated_at=NOW,
                    ),
                    OrderDraftRow(
                        correlation_id="CORR-DRAFT",
                        account_id_masked="****4567",
                        symbol="TSLA",
                        contract_id=201,
                        intent=OrderIntent.OPEN_SHORT_PUT.value,
                        quantity=1,
                        limit_price=Decimal("1"),
                        lifecycle=OrderLifecycle.DRAFT.value,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                    FillRow(
                        execution_id="EXEC-NVDA",
                        broker_order_id=22,
                        symbol="NVDA",
                        contract_id=202,
                        security_type=SecurityType.STOCK.value,
                        side=OrderSide.BUY.value,
                        quantity=Decimal("1"),
                        price=Decimal("100"),
                        multiplier=Decimal("1"),
                        currency="USD",
                        account_fingerprint=ACCOUNT_FINGERPRINT,
                        occurred_at=NOW,
                    ),
                    BasisEntryRow(
                        id="BASIS-AMD",
                        wheel_cycle_id="CYCLE-AMD",
                        symbol="AMD",
                        entry_type=BasisEntryType.MANUAL_ADJUSTMENT.value,
                        amount=Decimal("1"),
                        account_fingerprint=ACCOUNT_FINGERPRINT,
                        currency="USD",
                        provisional=False,
                        reconciliation_status=ReconciliationStatus.MANUAL_REVIEW.value,
                        source_note="Operator-reviewed local adjustment",
                        occurred_at=NOW,
                        created_at=NOW,
                    ),
                ]
            )

        evidence = LocalReconciliationRepository(database.sessions).read(ACCOUNT_ID)
    finally:
        database.dispose()

    assert evidence.unresolved_symbols == frozenset({"AAPL", "AMD", "MSFT", "NVDA", "TSLA"})
    assert evidence.complete is True


@pytest.mark.parametrize(
    ("model", "values", "expected_reason"),
    [
        (
            WheelCycleRow,
            {"status": "UNKNOWN"},
            "Persisted Wheel-cycle evidence is incomplete or malformed.",
        ),
        (
            StrategyStateRow,
            {"wheel_stage": "UNKNOWN"},
            "Persisted strategy-state evidence is incomplete or malformed.",
        ),
        (
            OrderDraftRow,
            {"account_id_masked": ACCOUNT_ID},
            "Persisted order evidence is incomplete or malformed.",
        ),
        (
            FillRow,
            {"account_fingerprint": account_fingerprint(OTHER_ACCOUNT_ID)},
            "Persisted fill evidence is incomplete or malformed.",
        ),
        (
            BasisEntryRow,
            {"reconciliation_status": "UNKNOWN"},
            "Persisted strategy-basis evidence is incomplete or malformed.",
        ),
    ],
)
def test_malformed_status_lifecycle_or_account_evidence_fails_closed(
    model: type[object],
    values: dict[str, object],
    expected_reason: str,
) -> None:
    database = _database()
    try:
        _seed_complete_active_order_evidence(database)
        with database.sessions.begin() as session:
            session.execute(update(model).values(**values))

        evidence = LocalReconciliationRepository(database.sessions).read(ACCOUNT_ID)
    finally:
        database.dispose()

    assert evidence.complete is False
    assert expected_reason in evidence.reasons
    assert ACCOUNT_ID not in " ".join(evidence.reasons)
    assert OTHER_ACCOUNT_ID not in " ".join(evidence.reasons)


def test_noncanonical_symbol_is_not_silently_normalized() -> None:
    database = _database()
    try:
        with database.sessions.begin() as session:
            session.add(
                StrategyStateRow(
                    symbol="aapl",
                    wheel_stage=WheelStage.FLAT.value,
                    reconciliation_status=ReconciliationStatus.PENDING.value,
                    updated_at=NOW,
                )
            )

        evidence = LocalReconciliationRepository(database.sessions).read(ACCOUNT_ID)
    finally:
        database.dispose()

    assert evidence.unresolved_symbols == frozenset()
    assert evidence.complete is False
    assert evidence.reasons == ("Persisted strategy-state evidence is incomplete or malformed.",)


@pytest.mark.parametrize(
    ("status", "closed_at"),
    [
        ("OPEN", NOW),
        ("CLOSED", None),
        ("CLOSED", NOW - timedelta(seconds=1)),
    ],
)
def test_wheel_cycle_status_requires_consistent_aware_timestamps(
    status: str,
    closed_at: datetime | None,
) -> None:
    database = _database()
    try:
        with database.sessions.begin() as session:
            session.add(
                WheelCycleRow(
                    id="CYCLE-TIME",
                    symbol="AAPL",
                    status=status,
                    opened_at=NOW,
                    closed_at=closed_at,
                )
            )

        evidence = LocalReconciliationRepository(database.sessions).read(ACCOUNT_ID)
    finally:
        database.dispose()

    assert evidence.complete is False
    assert "Persisted Wheel-cycle evidence is incomplete or malformed." in evidence.reasons


def test_closed_wheel_cycle_with_ordered_timestamps_is_structurally_complete() -> None:
    database = _database()
    try:
        with database.sessions.begin() as session:
            session.add(
                WheelCycleRow(
                    id="CYCLE-CLOSED",
                    symbol="AAPL",
                    status="CLOSED",
                    opened_at=NOW,
                    closed_at=NOW + timedelta(seconds=1),
                )
            )

        evidence = LocalReconciliationRepository(database.sessions).read(ACCOUNT_ID)
    finally:
        database.dispose()

    assert evidence.complete is True
    assert evidence.unresolved_symbols == frozenset({"AAPL"})


@pytest.mark.parametrize(
    ("contract_id", "quantity", "limit_price"),
    [
        (0, 1, Decimal("1")),
        (101, 0, Decimal("1")),
        (101, 1, Decimal("0")),
    ],
)
def test_pre_submission_draft_requires_positive_numeric_fields(
    contract_id: int,
    quantity: int,
    limit_price: Decimal,
) -> None:
    database = _database()
    try:
        with database.sessions.begin() as session:
            session.add(
                OrderDraftRow(
                    correlation_id="CORR-DRAFT",
                    account_id_masked="****4567",
                    symbol="AAPL",
                    contract_id=contract_id,
                    intent=OrderIntent.OPEN_SHORT_PUT.value,
                    quantity=quantity,
                    limit_price=limit_price,
                    lifecycle=OrderLifecycle.DRAFT.value,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

        evidence = LocalReconciliationRepository(database.sessions).read(ACCOUNT_ID)
    finally:
        database.dispose()

    assert evidence.complete is False
    assert "Persisted order evidence is incomplete or malformed." in evidence.reasons


def test_active_draft_without_submitted_owner_is_incomplete() -> None:
    database = _database()
    try:
        with database.sessions.begin() as session:
            session.add(
                WheelCycleRow(
                    id="CYCLE-1",
                    symbol="AAPL",
                    status="OPEN",
                    opened_at=NOW,
                )
            )
            session.flush()
            session.add(
                OrderDraftRow(
                    correlation_id="CORR-ORPHAN",
                    wheel_cycle_id="CYCLE-1",
                    account_id_masked="****4567",
                    symbol="AAPL",
                    contract_id=101,
                    intent=OrderIntent.OPEN_SHORT_PUT.value,
                    quantity=1,
                    limit_price=Decimal("1"),
                    lifecycle=OrderLifecycle.SUBMITTED.value,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

        evidence = LocalReconciliationRepository(database.sessions).read(ACCOUNT_ID)
    finally:
        database.dispose()

    assert evidence.active_order_identities == frozenset()
    assert evidence.unresolved_symbols == frozenset({"AAPL"})
    assert evidence.complete is False
    assert "Persisted order evidence is incomplete or malformed." in evidence.reasons


def test_corrupted_foreign_keys_are_reported_as_safe_orphan_reasons() -> None:
    database = _database()
    try:
        with database.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            connection.execute(
                insert(StrategyStateRow).values(
                    symbol="AAPL",
                    wheel_cycle_id="MISSING-CYCLE",
                    wheel_stage=WheelStage.FLAT.value,
                    reconciliation_status=ReconciliationStatus.PENDING.value,
                    updated_at=NOW,
                )
            )
            connection.execute(
                insert(SubmittedOrderRow).values(
                    correlation_id="MISSING-DRAFT",
                    broker_order_id=99,
                    permanent_id=9099,
                    client_id=17,
                    order_ref="CHR-ORPHAN-99",
                    lifecycle=OrderLifecycle.SUBMITTED.value,
                    submitted_at=NOW,
                )
            )
            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()

        evidence = LocalReconciliationRepository(database.sessions).read(ACCOUNT_ID)
    finally:
        database.dispose()

    assert evidence.active_order_identities == frozenset()
    assert evidence.unresolved_symbols == frozenset({"AAPL"})
    assert evidence.complete is False
    assert "Persisted strategy-state evidence is incomplete or malformed." in evidence.reasons
    assert "Persisted order evidence is incomplete or malformed." in evidence.reasons


def test_scope_mismatch_is_generic_and_never_discloses_account_ids() -> None:
    database = _database()
    try:
        with pytest.raises(ValueError, match="pseudonymous database scope") as exc_info:
            LocalReconciliationRepository(database.sessions).read(OTHER_ACCOUNT_ID)
    finally:
        database.dispose()

    assert ACCOUNT_ID not in str(exc_info.value)
    assert OTHER_ACCOUNT_ID not in str(exc_info.value)


def test_local_read_uses_one_read_only_transaction_and_persists_no_raw_account(
    tmp_path: Path,
) -> None:
    database = _database(url=f"sqlite+pysqlite:///{tmp_path}/chronos.db")
    try:
        _seed_complete_active_order_evidence(database)
        connection_ids: set[int] = set()
        transaction_ids: set[int] = set()
        statements: list[str] = []

        def record_statement(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            del cursor, parameters, context, executemany
            transaction = connection.get_transaction()  # type: ignore[attr-defined]
            assert transaction is not None
            connection_ids.add(id(connection))
            transaction_ids.add(id(transaction))
            statements.append(statement.lstrip().split(maxsplit=1)[0].upper())

        event.listen(database.engine, "before_cursor_execute", record_statement)
        try:
            evidence = LocalReconciliationRepository(database.sessions).read(ACCOUNT_ID)
        finally:
            event.remove(database.engine, "before_cursor_execute", record_statement)

        with database.sessions() as session:
            scope = session.get(DatabaseScopeRow, 1)
            event_count = session.scalar(select(func.count()).select_from(ApplicationEventRow))
            drafts = tuple(session.scalars(select(OrderDraftRow)))
    finally:
        database.dispose()

    assert evidence.complete is True
    assert connection_ids and len(connection_ids) == 1
    assert transaction_ids and len(transaction_ids) == 1
    assert set(statements) == {"SELECT"}
    assert scope is not None
    assert scope.account_fingerprint == ACCOUNT_FINGERPRINT
    assert all(ACCOUNT_ID not in draft.account_id_masked for draft in drafts)
    assert event_count == 0
