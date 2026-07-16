from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, insert, select

from chronos.domain.enums import OrderIntent, OrderLifecycle, OrderSide
from chronos.domain.models import BrokerOrderIdentity
from chronos.persistence.database import Database
from chronos.persistence.repositories import OrderOwnershipRepository
from chronos.persistence.schema import (
    ApplicationEventRow,
    DatabaseScopeRow,
    OrderDraftRow,
    SubmittedOrderRow,
    WheelCycleRow,
)
from chronos.utils.identifiers import account_fingerprint

NOW = datetime(2026, 1, 15, 15, 30, tzinfo=UTC)
ACCOUNT_ID = "DU1234567"
OTHER_ACCOUNT_ID = "DU7654321"


def _database(*, cycle_symbol: str = "AAPL", cycle_status: str = "OPEN") -> Database:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    database.bind_scope(broker_mode="demo", environment="DEMO", account_id=ACCOUNT_ID)
    with database.sessions.begin() as session:
        session.add(
            WheelCycleRow(
                id="CYCLE-1",
                symbol=cycle_symbol,
                status=cycle_status,
                opened_at=NOW,
            )
        )
    return database


def _add_order(
    database: Database,
    *,
    correlation_id: str,
    broker_order_id: int,
    order_ref: str,
    intent: str = OrderIntent.OPEN_SHORT_PUT.value,
    lifecycle: str = OrderLifecycle.SUBMITTED.value,
    draft_lifecycle: str | None = None,
    wheel_cycle_id: str | None = "CYCLE-1",
    symbol: str = "AAPL",
    contract_id: int = 101,
    quantity: int = 1,
    permanent_id: int | None = 9001,
    client_id: int = 17,
) -> None:
    with database.sessions.begin() as session:
        session.add(
            OrderDraftRow(
                correlation_id=correlation_id,
                wheel_cycle_id=wheel_cycle_id,
                account_id_masked="****4567",
                symbol=symbol,
                contract_id=contract_id,
                intent=intent,
                quantity=quantity,
                limit_price=Decimal("1.25"),
                lifecycle=draft_lifecycle or lifecycle,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            SubmittedOrderRow(
                correlation_id=correlation_id,
                broker_order_id=broker_order_id,
                permanent_id=permanent_id,
                client_id=client_id,
                order_ref=order_ref,
                lifecycle=lifecycle,
                submitted_at=NOW,
            )
        )


def test_active_identities_reconstructs_every_exact_broker_identity_field() -> None:
    database = _database()
    try:
        _add_order(
            database,
            correlation_id="CORR-PUT",
            broker_order_id=11,
            order_ref="CHR-PUT-11",
            intent=OrderIntent.OPEN_SHORT_PUT.value,
            contract_id=101,
            quantity=2,
        )
        _add_order(
            database,
            correlation_id="CORR-CALL",
            broker_order_id=12,
            order_ref="CHR-CALL-12",
            intent=OrderIntent.OPEN_COVERED_CALL.value,
            lifecycle=OrderLifecycle.PARTIALLY_FILLED.value,
            contract_id=102,
            permanent_id=None,
            client_id=22,
        )
        _add_order(
            database,
            correlation_id="CORR-CLOSE",
            broker_order_id=13,
            order_ref="CHR-CLOSE-13",
            intent=OrderIntent.CLOSE_SHORT_OPTION.value,
            contract_id=103,
            quantity=3,
            permanent_id=9003,
        )

        identities = OrderOwnershipRepository(database.sessions).active_identities(ACCOUNT_ID)
    finally:
        database.dispose()

    assert identities == frozenset(
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
                quantity=Decimal("2"),
            ),
            BrokerOrderIdentity(
                account_id=ACCOUNT_ID,
                client_id=22,
                broker_order_id=12,
                permanent_id=None,
                order_ref="CHR-CALL-12",
                symbol="AAPL",
                contract_id=102,
                side=OrderSide.SELL,
                quantity=Decimal("1"),
            ),
            BrokerOrderIdentity(
                account_id=ACCOUNT_ID,
                client_id=17,
                broker_order_id=13,
                permanent_id=9003,
                order_ref="CHR-CLOSE-13",
                symbol="AAPL",
                contract_id=103,
                side=OrderSide.BUY,
                quantity=Decimal("3"),
            ),
        }
    )


def test_active_identities_filters_valid_terminal_orders() -> None:
    database = _database()
    try:
        for index, lifecycle in enumerate(
            (
                OrderLifecycle.FILLED,
                OrderLifecycle.CANCELLED,
                OrderLifecycle.REJECTED,
            ),
            start=1,
        ):
            _add_order(
                database,
                correlation_id=f"CORR-TERMINAL-{index}",
                broker_order_id=100 + index,
                order_ref=f"CHR-TERMINAL-{index}",
                lifecycle=lifecycle.value,
                contract_id=200 + index,
            )

        identities = OrderOwnershipRepository(database.sessions).active_identities(ACCOUNT_ID)
    finally:
        database.dispose()

    assert identities == frozenset()


def test_active_identities_rejects_current_account_scope_mismatch_without_disclosure() -> None:
    database = _database()
    try:
        repository = OrderOwnershipRepository(database.sessions)

        with pytest.raises(ValueError, match="pseudonymous database scope") as exc_info:
            repository.active_identities(OTHER_ACCOUNT_ID)
    finally:
        database.dispose()

    assert ACCOUNT_ID not in str(exc_info.value)
    assert OTHER_ACCOUNT_ID not in str(exc_info.value)


@pytest.mark.parametrize(
    ("submitted_lifecycle", "draft_lifecycle"),
    [
        (OrderLifecycle.SUBMITTED, OrderLifecycle.PARTIALLY_FILLED),
        (OrderLifecycle.FILLED, OrderLifecycle.CANCELLED),
    ],
)
def test_active_identities_rejects_disagreeing_persisted_lifecycles(
    submitted_lifecycle: OrderLifecycle,
    draft_lifecycle: OrderLifecycle,
) -> None:
    database = _database()
    try:
        _add_order(
            database,
            correlation_id="CORR-LIFECYCLE-MISMATCH",
            broker_order_id=25,
            order_ref="CHR-LIFECYCLE-MISMATCH-25",
            lifecycle=submitted_lifecycle.value,
            draft_lifecycle=draft_lifecycle.value,
        )

        with pytest.raises(ValueError, match="lifecycles do not agree"):
            OrderOwnershipRepository(database.sessions).active_identities(ACCOUNT_ID)
    finally:
        database.dispose()


def test_active_identity_requires_an_open_wheel_cycle() -> None:
    database = _database(cycle_status="CLOSED")
    try:
        _add_order(
            database,
            correlation_id="CORR-CLOSED-CYCLE",
            broker_order_id=26,
            order_ref="CHR-CLOSED-CYCLE-26",
        )

        with pytest.raises(ValueError, match="open Wheel cycle"):
            OrderOwnershipRepository(database.sessions).active_identities(ACCOUNT_ID)
    finally:
        database.dispose()


def test_terminal_identity_may_belong_to_a_closed_wheel_cycle() -> None:
    database = _database(cycle_status="CLOSED")
    try:
        _add_order(
            database,
            correlation_id="CORR-CLOSED-TERMINAL",
            broker_order_id=27,
            order_ref="CHR-CLOSED-TERMINAL-27",
            lifecycle=OrderLifecycle.FILLED.value,
        )

        identities = OrderOwnershipRepository(database.sessions).active_identities(ACCOUNT_ID)
    finally:
        database.dispose()

    assert identities == frozenset()


@pytest.mark.parametrize(
    ("lifecycle", "draft_lifecycle", "intent", "message"),
    [
        ("UNKNOWN", None, OrderIntent.OPEN_SHORT_PUT.value, "unknown lifecycle"),
        (
            OrderLifecycle.DRAFT.value,
            None,
            OrderIntent.OPEN_SHORT_PUT.value,
            "invalid pre-submission lifecycle",
        ),
        (
            OrderLifecycle.SUBMITTED.value,
            "UNKNOWN",
            OrderIntent.OPEN_SHORT_PUT.value,
            "draft has an unknown lifecycle",
        ),
        (OrderLifecycle.SUBMITTED.value, None, "UNKNOWN", "draft has an unknown intent"),
        (OrderLifecycle.FILLED.value, None, "UNKNOWN", "draft has an unknown intent"),
    ],
)
def test_active_identities_fails_closed_for_unknown_or_malformed_state(
    lifecycle: str,
    draft_lifecycle: str | None,
    intent: str,
    message: str,
) -> None:
    database = _database()
    try:
        _add_order(
            database,
            correlation_id="CORR-BAD",
            broker_order_id=99,
            order_ref="CHR-BAD-99",
            lifecycle=lifecycle,
            draft_lifecycle=draft_lifecycle,
            intent=intent,
        )

        with pytest.raises(ValueError, match=message):
            OrderOwnershipRepository(database.sessions).active_identities(ACCOUNT_ID)
    finally:
        database.dispose()


def test_active_identities_requires_non_null_cycle_owner() -> None:
    database = _database()
    try:
        _add_order(
            database,
            correlation_id="CORR-NO-CYCLE",
            broker_order_id=21,
            order_ref="CHR-NO-CYCLE-21",
            wheel_cycle_id=None,
        )

        with pytest.raises(ValueError, match="no Wheel cycle owner"):
            OrderOwnershipRepository(database.sessions).active_identities(ACCOUNT_ID)
    finally:
        database.dispose()


def test_active_identities_rejects_cycle_symbol_mismatch() -> None:
    database = _database(cycle_symbol="MSFT")
    try:
        _add_order(
            database,
            correlation_id="CORR-WRONG-SYMBOL",
            broker_order_id=22,
            order_ref="CHR-WRONG-SYMBOL-22",
        )

        with pytest.raises(ValueError, match="symbol does not match"):
            OrderOwnershipRepository(database.sessions).active_identities(ACCOUNT_ID)
    finally:
        database.dispose()


def test_active_identities_rejects_orphaned_submitted_order() -> None:
    database = _database()
    try:
        with database.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            connection.execute(
                insert(SubmittedOrderRow).values(
                    correlation_id="CORR-ORPHAN",
                    broker_order_id=23,
                    permanent_id=9023,
                    client_id=17,
                    order_ref="CHR-ORPHAN-23",
                    lifecycle=OrderLifecycle.SUBMITTED.value,
                    submitted_at=NOW,
                )
            )
            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()

        with pytest.raises(ValueError, match="no draft owner"):
            OrderOwnershipRepository(database.sessions).active_identities(ACCOUNT_ID)
    finally:
        database.dispose()


def test_active_identity_read_never_persists_the_raw_account_id() -> None:
    database = _database()
    try:
        _add_order(
            database,
            correlation_id="CORR-READONLY",
            broker_order_id=24,
            order_ref="CHR-READONLY-24",
        )

        identities = OrderOwnershipRepository(database.sessions).active_identities(ACCOUNT_ID)

        with database.sessions() as session:
            scope = session.get(DatabaseScopeRow, 1)
            assert scope is not None
            drafts = tuple(session.scalars(select(OrderDraftRow)))
            submitted = tuple(session.scalars(select(SubmittedOrderRow)))
            event_count = session.scalar(select(func.count()).select_from(ApplicationEventRow))
    finally:
        database.dispose()

    assert len(identities) == 1
    persisted_strings = (
        scope.broker_mode,
        scope.environment,
        scope.account_fingerprint,
        *(draft.account_id_masked for draft in drafts),
        *(row.order_ref for row in submitted),
    )
    assert all(ACCOUNT_ID not in value for value in persisted_strings)
    assert scope.account_fingerprint == account_fingerprint(ACCOUNT_ID)
    assert event_count == 0
