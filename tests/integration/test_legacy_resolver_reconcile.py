"""S-3 through the REAL reconciler: a contradictory REJECTED/CANCELLED row never clears the lock.

The pure-resolver pins live in ``tests/unit/test_legacy_resolver_lifecycle_first.py``. These run
``OrderRestartReconciler.reconcile_report`` and ``operator_resolve`` over a real (in-memory)
database, tracker and connection manager; only the broker is scripted, so the whole chain is
exercised: evidence -> resolver -> tracker -> ``remaining_active`` ->
``permits_new_opening_order`` (the opening-order lock the fail-open used to clear).

Local statuses are reached the way the system reaches them: SUBMITTED by a submit with its ids,
PARTIALLY_FILLED and CANCEL_PENDING by a later event, SUBMISSION_UNKNOWN by a submit that raised
(no broker id, no permId, no client id).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.support.order_fakes import PAPER_ACCOUNT, FakeBroker

from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.enums import OrderLifecycle, OrderSide, ProductFamily
from chronos.domain.models import BrokerExecution, BrokerOrder, UnderlyingContract
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.reconciliation_recovery import (
    OrderRestartReconciler,
    OrderRestartReconciliationReport,
)
from chronos.orders.tracker import OrderTracker
from chronos.persistence.database import Database
from chronos.persistence.order_repositories import (
    OrderIntentRecord,
    OrderIntentRepository,
    OrderTrackerRepository,
)
from chronos.utils.identifiers import account_fingerprint

ACCOUNT = PAPER_ACCOUNT
CLIENT_ID = 17
REF = "CHR-ORD-" + "S" * 32
BROKER_ID = 9001
PERM_ID = 109001
QUANTITY = Decimal("10")
LIMIT = Decimal("150.00")
CON_ID = 265598
PRIOR = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)  # the previous Friday
EARLIER_TODAY = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)  # 10:00 New York, same session as NOW
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)  # 11:00 New York, Monday

SENT_ACTIVE = (
    OrderLifecycle.SUBMITTED,
    OrderLifecycle.SUBMISSION_UNKNOWN,
    OrderLifecycle.PARTIALLY_FILLED,
    OrderLifecycle.CANCEL_PENDING,
)
CONTRADICTED = (OrderLifecycle.REJECTED, OrderLifecycle.CANCELLED)


def _row(lifecycle: OrderLifecycle, filled: str) -> BrokerOrder:
    filled_quantity = Decimal(filled)
    return BrokerOrder(
        broker_order_id=BROKER_ID,
        permanent_id=PERM_ID,
        client_id=CLIENT_ID,
        account_id=ACCOUNT,
        order_ref=REF,
        contract=UnderlyingContract(con_id=CON_ID, symbol="AAPL"),
        side=OrderSide.BUY,
        quantity=QUANTITY,
        filled_quantity=filled_quantity,
        remaining_quantity=QUANTITY - filled_quantity,
        limit_price=LIMIT,
        lifecycle=lifecycle,
    )


class _Venue(FakeBroker):
    """A broker double whose listed rows and executions are exactly what the test says."""

    def __init__(self) -> None:
        super().__init__(account_id=ACCOUNT)
        self.open: list[BrokerOrder] = []
        self.execs: list[BrokerExecution] = []

    async def open_orders(self) -> tuple[BrokerOrder, ...]:
        return tuple(self.open)

    async def executions(self, since: datetime | None = None) -> tuple[BrokerExecution, ...]:
        return tuple(self.execs)


@dataclass
class _Rig:
    db: Database
    venue: _Venue
    intents: OrderIntentRepository
    repo: OrderTrackerRepository
    connection: BrokerConnectionManager
    reconciler: OrderRestartReconciler

    def status(self) -> OrderLifecycle:
        record = self.intents.get("intent-1", current_account_id=ACCOUNT)
        assert record is not None
        return record.status

    def event_count(self) -> int:
        return len(self.repo.events("intent-1", current_account_id=ACCOUNT))

    def reconcile(self) -> OrderRestartReconciliationReport:
        return self.reconciler.reconcile_report(current_account_id=ACCOUNT, now=NOW)

    def _record(self, to: OrderLifecycle, key: str, **fields: object) -> None:
        assert self.repo.record_transition(
            intent_id="intent-1",
            event_key=f"intent-1:{key}",
            source=str(fields.pop("source", "TEST")),
            from_status=fields.pop("from_status"),  # type: ignore[arg-type]
            to_status=to,
            current_account_id=ACCOUNT,
            **fields,  # type: ignore[arg-type]
        )

    def seed(self, local: OrderLifecycle) -> None:
        """One 10-share BUY intent whose persisted ids are the ones the real path writes."""

        record = OrderIntentRecord(
            intent_id="intent-1",
            idempotency_key="idem-intent-1",
            account_fingerprint=account_fingerprint(ACCOUNT),
            environment="paper",
            product_family=ProductFamily.STOCK,
            wheel_cycle_id=None,
            symbol="AAPL",
            con_id=CON_ID,
            local_symbol=None,
            action=OrderSide.BUY,
            open_close_effect="OPEN",
            quantity=QUANTITY,
            order_type="LMT",
            limit_price=LIMIT,
            time_in_force="DAY",
            outside_rth=False,
            quote_snapshot_id=None,
            risk_snapshot_id=None,
            preview_id=None,
            confirmation_hash=None,
            order_ref=REF,
            status=OrderLifecycle.USER_CONFIRMED,
            created_at=PRIOR,
            confirmed_at=PRIOR,
            submitted_at=None,
            expires_at=None,
        )
        assert self.intents.create(record, current_account_id=ACCOUNT)
        if local is OrderLifecycle.SUBMISSION_UNKNOWN:
            # a submit that raised or timed out: no broker id, permId or client id
            self._record(
                OrderLifecycle.SUBMISSION_UNKNOWN,
                "unknown",
                source="SUBMIT",
                from_status=OrderLifecycle.USER_CONFIRMED,
                occurred_at=EARLIER_TODAY,
            )
            return
        self._record(
            OrderLifecycle.SUBMITTED,
            "submit",
            source="SUBMIT",
            from_status=OrderLifecycle.USER_CONFIRMED,
            broker_order_id=BROKER_ID,
            permanent_id=PERM_ID,
            client_id=CLIENT_ID,
            filled_quantity=Decimal(0),
            remaining_quantity=QUANTITY,
            occurred_at=PRIOR,
        )
        if local is OrderLifecycle.PARTIALLY_FILLED:
            self._record(
                OrderLifecycle.PARTIALLY_FILLED,
                "partial",
                from_status=OrderLifecycle.SUBMITTED,
                broker_order_id=BROKER_ID,
                filled_quantity=Decimal(3),
                remaining_quantity=QUANTITY - 3,
                occurred_at=PRIOR,
            )
        elif local is OrderLifecycle.CANCEL_PENDING:
            self._record(
                OrderLifecycle.CANCEL_PENDING,
                "cancel",
                source="CANCEL",
                from_status=OrderLifecycle.SUBMITTED,
                broker_order_id=BROKER_ID,
                occurred_at=PRIOR,
            )
        assert self.status() is local


@pytest.fixture
def rig() -> Iterator[_Rig]:
    venue = _Venue()
    db = Database("sqlite:///:memory:")
    db.initialize()
    db.bind_scope(broker_mode="ibkr", environment="paper", account_id=ACCOUNT)
    intents = OrderIntentRepository(db.sessions)
    repo = OrderTrackerRepository(db.sessions)
    tracker = OrderTracker(intents, repo)
    readiness = ReconciliationReadiness()
    connection = BrokerConnectionManager(venue, readiness=readiness)
    connection.start()
    reconciler = OrderRestartReconciler(
        connection=connection,
        intents=intents,
        tracker=tracker,
        reconciliation_readiness=readiness,
        expected_broker_client_id=CLIENT_ID,
    )
    try:
        yield _Rig(db, venue, intents, repo, connection, reconciler)
    finally:
        connection.close()
        db.dispose()


# --- RED: the contradictory row leaves the intent locked, with a typed reason --------------------


@pytest.mark.parametrize("filled", ["1", "4", "10"])
@pytest.mark.parametrize("typed", CONTRADICTED, ids=lambda lifecycle: lifecycle.value)
@pytest.mark.parametrize("local", SENT_ACTIVE, ids=lambda lifecycle: lifecycle.value)
def test_a_rejected_or_cancelled_listed_row_with_fills_leaves_the_intent_locked(
    rig: _Rig, local: OrderLifecycle, typed: OrderLifecycle, filled: str
) -> None:
    rig.seed(local)
    rig.venue.open = [_row(typed, filled)]
    events_before = rig.event_count()

    report = rig.reconcile()

    assert rig.status() is local  # not FILLED (filled = Q), not PARTIALLY_FILLED (0 < filled < Q)
    assert rig.event_count() == events_before  # and nothing was written at all
    assert [item.intent_id for item in report.remaining_active] == ["intent-1"]
    assert report.permits_new_opening_order is False  # the opening-order lock is NOT cleared
    (item,) = report.unresolved
    # a typed reason that says what is wrong, not the false "absent from the snapshots"
    assert "absent" not in item.reason
    assert "contradict" in item.reason and typed.value in item.reason


def test_operator_resolve_refuses_a_rejected_row_with_fills(rig: _Rig) -> None:
    rig.seed(OrderLifecycle.SUBMISSION_UNKNOWN)
    rig.venue.open = [_row(OrderLifecycle.REJECTED, "10")]
    events_before = rig.event_count()

    with pytest.raises(ValueError, match="broker evidence conflicts"):
        rig.reconciler.operator_resolve(
            "intent-1",
            operator_note="checked the TWS order list",
            current_account_id=ACCOUNT,
            now=NOW,
        )

    assert rig.status() is OrderLifecycle.SUBMISSION_UNKNOWN
    assert rig.event_count() == events_before


# --- positive controls: these pass on main today and must keep passing ---------------------------


@pytest.mark.parametrize("working", [OrderLifecycle.SUBMITTED, OrderLifecycle.PARTIALLY_FILLED])
@pytest.mark.parametrize("local", SENT_ACTIVE, ids=lambda lifecycle: lifecycle.value)
def test_a_working_row_filled_to_quantity_is_still_recorded_filled(
    rig: _Rig, local: OrderLifecycle, working: OrderLifecycle
) -> None:
    """Design C3: a WORKING row with filled >= Q is FILLED, and that does release the lock."""

    rig.seed(local)
    rig.venue.open = [_row(working, "10")]

    report = rig.reconcile()

    assert rig.status() is OrderLifecycle.FILLED
    assert report.remaining_active == ()
    assert report.permits_new_opening_order is True


@pytest.mark.parametrize("local", SENT_ACTIVE, ids=lambda lifecycle: lifecycle.value)
def test_a_working_row_with_a_partial_fill_stays_a_working_partial(
    rig: _Rig, local: OrderLifecycle
) -> None:
    rig.seed(local)
    rig.venue.open = [_row(OrderLifecycle.SUBMITTED, "4")]

    report = rig.reconcile()

    assert rig.status() is OrderLifecycle.PARTIALLY_FILLED
    assert [item.intent_id for item in report.remaining_active] == ["intent-1"]
    assert report.permits_new_opening_order is False


@pytest.mark.parametrize("typed", CONTRADICTED, ids=lambda lifecycle: lifecycle.value)
@pytest.mark.parametrize(
    "local", [OrderLifecycle.SUBMITTED, OrderLifecycle.SUBMISSION_UNKNOWN], ids=lambda x: x.value
)
def test_a_rejected_or_cancelled_row_without_fills_still_gets_its_terminal_write(
    rig: _Rig, local: OrderLifecycle, typed: OrderLifecycle
) -> None:
    """Q3: an exactly owned listed rejection (or cancel) with nothing filled IS evidence."""

    rig.seed(local)
    rig.venue.open = [_row(typed, "0")]

    report = rig.reconcile()

    assert rig.status() is typed
    assert report.remaining_active == ()


@pytest.mark.parametrize("local", SENT_ACTIVE, ids=lambda lifecycle: lifecycle.value)
def test_no_evidence_leaves_the_intent_locked_with_the_absence_reason(
    rig: _Rig, local: OrderLifecycle
) -> None:
    """Real absence keeps its own reason text; the new refusal must not have replaced it."""

    rig.seed(local)

    report = rig.reconcile()

    assert rig.status() is local
    assert report.permits_new_opening_order is False
    (item,) = report.unresolved
    assert "absent from the broker order and execution snapshots" in item.reason
