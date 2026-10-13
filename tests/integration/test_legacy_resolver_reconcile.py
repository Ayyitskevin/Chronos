"""S-3 through the REAL reconciler: a contradictory REJECTED/CANCELLED row never clears the lock.

The pure-resolver pins live in ``tests/unit/test_legacy_resolver_lifecycle_first.py``. These run
``OrderRestartReconciler.reconcile_report`` and ``operator_resolve`` over a real (in-memory)
database, tracker and connection manager; only the broker is scripted, so the whole chain is
exercised: evidence -> resolver -> tracker -> ``remaining_active`` ->
``permits_new_opening_order`` (the opening-order lock the fail-open used to clear).

r1 (reader review of 9a163ec): the guard is an allowlist (quantity refines only working lifecycles
and a typed FILLED row), so an UNRECOGNISED broker status (typed SUBMISSION_UNKNOWN) with fills is
locked too; the typed reason says so; and ``operator_resolve``'s refusal no longer says "identity"
for a lifecycle conflict. LEGACY-5 (reader P2-2) was pinned as a strict xfail here and, after Muse's
ruling (option B), is fixed: a terminal row below the recorded fills settles nothing.

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

# The sent-active statuses the LEGACY resolver still decides. CANCEL_PENDING is not among them
# once the U-A settle planner owns it (design r3 §3.1 item 2; the S-3 packet anticipated this:
# "U-A ... moves CANCEL_PENDING off the legacy resolver"). Its counterparts of every case below are
# pinned against the planner in tests/integration/test_cancel_pending_recovery.py
# (test_r3_on_main_*): locked for REJECTED / unrecognised / no evidence / a zero-fill terminal row
# under recorded fills, fill progress in place for a working partial, C3 for a working row at Q.
SENT_ACTIVE = (
    OrderLifecycle.SUBMITTED,
    OrderLifecycle.SUBMISSION_UNKNOWN,
    OrderLifecycle.PARTIALLY_FILLED,
)
CONTRADICTED = (OrderLifecycle.REJECTED, OrderLifecycle.CANCELLED)
# The only other non-working typed value the official adapter can produce: an unrecognised status.
UNRECOGNISED = OrderLifecycle.SUBMISSION_UNKNOWN


def _row(lifecycle: OrderLifecycle, filled: str, *, ref: str | None = REF) -> BrokerOrder:
    filled_quantity = Decimal(filled)
    return BrokerOrder(
        broker_order_id=BROKER_ID,
        permanent_id=PERM_ID,
        client_id=CLIENT_ID,
        account_id=ACCOUNT,
        order_ref=ref,
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
    assert typed.value in item.reason
    assert "not a working lifecycle" in item.reason and "cannot settle" in item.reason


@pytest.mark.parametrize("typed", [OrderLifecycle.REJECTED, UNRECOGNISED], ids=lambda x: x.value)
def test_operator_resolve_refuses_a_non_working_row_with_fills(
    rig: _Rig, typed: OrderLifecycle
) -> None:
    rig.seed(OrderLifecycle.SUBMISSION_UNKNOWN)
    rig.venue.open = [_row(typed, "10")]
    events_before = rig.event_count()

    # P3-1: the refusal names what is wrong (the evidence does not settle it), not "identity"
    with pytest.raises(ValueError, match="does not settle this intent") as refusal:
        rig.reconciler.operator_resolve(
            "intent-1",
            operator_note="checked the TWS order list",
            current_account_id=ACCOUNT,
            now=NOW,
        )

    assert "lifecycle or fills" in str(
        refusal.value
    )  # it names the real possibilities, not just identity
    assert rig.status() is OrderLifecycle.SUBMISSION_UNKNOWN
    assert rig.event_count() == events_before


@pytest.mark.parametrize("filled", ["1", "4", "10"])
@pytest.mark.parametrize("local", SENT_ACTIVE, ids=lambda lifecycle: lifecycle.value)
def test_an_unrecognised_status_row_with_fills_leaves_the_intent_locked(
    rig: _Rig, local: OrderLifecycle, filled: str
) -> None:
    """P2-1: D-27: a novel status gains no working-order authority from quantities alone."""

    rig.seed(local)
    rig.venue.open = [_row(UNRECOGNISED, filled)]
    events_before = rig.event_count()

    report = rig.reconcile()

    assert rig.status() is local
    assert rig.event_count() == events_before
    assert [item.intent_id for item in report.remaining_active] == ["intent-1"]
    assert report.permits_new_opening_order is False
    (item,) = report.unresolved
    assert "absent" not in item.reason
    assert UNRECOGNISED.value in item.reason and "cannot settle" in item.reason


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


# --- LEGACY-5 (reader P2-2, Muse option B): a terminal row below the recorded fills settles nothing
# A locally PARTIALLY_FILLED intent has recorded fills. A listed, exactly owned CANCELLED/REJECTED
# row at ZERO fills used to be written as the terminal state with filled_quantity 0, which drops or
# contradicts the recorded fills and releases the lock (the adapter reports filled 0 whenever its
# order-status cache is empty, which is every order after a reconnect). The closed settle design
# says an under-reported venue figure "can never lower what we recorded". Ruled option B: both
# callers pass the tracker's recorded cumulative fills to the resolver, which leaves such an intent
# locked with a typed reason. These four cells were strict xfails and are now passing tests.


@pytest.mark.parametrize("typed", CONTRADICTED, ids=lambda lifecycle: lifecycle.value)
# "partial-then-cancel-pending" left this list with U-A: the settle planner already refuses it (a
# venue figure below the witnessed lower bound), pinned non-xfail in test_cancel_pending_recovery.py
@pytest.mark.parametrize("path", ["partial"])
def test_a_terminal_row_at_zero_fills_does_not_overwrite_recorded_fills(
    rig: _Rig, path: str, typed: OrderLifecycle
) -> None:
    rig.seed(OrderLifecycle.PARTIALLY_FILLED)  # 3 of 10 recorded
    if path == "partial-then-cancel-pending":
        rig._record(
            OrderLifecycle.CANCEL_PENDING,
            "cancel-after-partial",
            source="CANCEL",
            from_status=OrderLifecycle.PARTIALLY_FILLED,
            broker_order_id=BROKER_ID,
            occurred_at=PRIOR,
        )
    local = rig.status()
    rig.venue.open = [_row(typed, "0")]
    events_before = rig.event_count()

    report = rig.reconcile()

    assert rig.status() is local  # the recorded fills are not dropped by a terminal write
    assert rig.event_count() == events_before
    assert [item.intent_id for item in report.remaining_active] == ["intent-1"]
    assert report.permits_new_opening_order is False


# --- LEGACY-5 (option B): the pins above are the red; these are the controls and the reasons -----


@pytest.mark.parametrize("path", ["partial", "partial-then-cancel-pending"])
@pytest.mark.parametrize("typed", CONTRADICTED, ids=lambda lifecycle: lifecycle.value)
def test_the_refusal_names_the_recorded_fills_it_would_have_dropped(
    rig: _Rig, typed: OrderLifecycle, path: str
) -> None:
    rig.seed(OrderLifecycle.PARTIALLY_FILLED)
    if path == "partial-then-cancel-pending":
        rig._record(
            OrderLifecycle.CANCEL_PENDING,
            "cancel-after-partial",
            source="CANCEL",
            from_status=OrderLifecycle.PARTIALLY_FILLED,
            broker_order_id=BROKER_ID,
            occurred_at=PRIOR,
        )
    rig.venue.open = [_row(typed, "0")]

    (item,) = rig.reconcile().unresolved

    assert "absent" not in item.reason
    if path == "partial":
        # the legacy resolver's own words. A CANCEL_PENDING intent may instead be settled by the
        # U-A/U-B planner (#302), which locks it with its own typed reason for the same fact
        # ("venue fill figure 0 is below the witnessed lower bound 3"), so only the lock is exact.
        assert typed.value in item.reason
        assert "below the 3 filled already recorded" in item.reason
        assert "stays locked" in item.reason


def test_a_terminal_row_above_the_recorded_fills_still_settles(rig: _Rig) -> None:
    """Positive control: 3 of 10 recorded, the venue reports the order FILLED at 10."""

    rig.seed(OrderLifecycle.PARTIALLY_FILLED)
    rig.venue.open = [_row(OrderLifecycle.FILLED, "10")]

    report = rig.reconcile()

    assert rig.status() is OrderLifecycle.FILLED
    assert report.remaining_active == ()
    assert report.unresolved == ()


def test_the_genuine_partial_then_cancel_stays_locked_under_s3_unchanged(rig: _Rig) -> None:
    """3 of 10 recorded and the venue says CANCELLED at 3: S-3 locks it, and says so in S-3's words,
    not the new rule's (3 is not below 3)."""

    rig.seed(OrderLifecycle.PARTIALLY_FILLED)
    rig.venue.open = [_row(OrderLifecycle.CANCELLED, "3")]
    events_before = rig.event_count()

    report = rig.reconcile()

    assert rig.status() is OrderLifecycle.PARTIALLY_FILLED
    assert rig.event_count() == events_before
    assert report.permits_new_opening_order is False
    (item,) = report.unresolved
    assert "not a working lifecycle" in item.reason and "cannot settle" in item.reason
    assert "already recorded" not in item.reason


@pytest.mark.parametrize("typed", CONTRADICTED, ids=lambda lifecycle: lifecycle.value)
def test_operator_resolve_passes_the_recorded_fills_too(rig: _Rig, typed: OrderLifecycle) -> None:
    """An intent that is SUBMISSION_UNKNOWN but has fills on record is not settled by a zero row."""

    rig.seed(OrderLifecycle.PARTIALLY_FILLED)
    rig._record(
        OrderLifecycle.SUBMISSION_UNKNOWN,
        "unknown-after-partial",
        source="SUBMIT",
        from_status=OrderLifecycle.PARTIALLY_FILLED,
        occurred_at=EARLIER_TODAY,
    )
    assert rig.status() is OrderLifecycle.SUBMISSION_UNKNOWN
    rig.venue.open = [_row(typed, "0")]
    events_before = rig.event_count()

    with pytest.raises(ValueError, match="does not settle this intent"):
        rig.reconciler.operator_resolve(
            "intent-1",
            operator_note="checked the TWS order list",
            current_account_id=ACCOUNT,
            now=NOW,
        )

    assert rig.status() is OrderLifecycle.SUBMISSION_UNKNOWN
    assert rig.event_count() == events_before


def test_the_recorded_cumulative_fills_are_the_largest_figure_on_the_events(rig: _Rig) -> None:
    """The accessor the callers pass: a later event with a smaller figure lowers nothing."""

    tracker = OrderTracker(rig.intents, rig.repo)
    rig.seed(OrderLifecycle.PARTIALLY_FILLED)  # 0, then 3 recorded
    assert tracker.recorded_filled_quantity("intent-1", current_account_id=ACCOUNT) == Decimal(3)
    rig._record(
        OrderLifecycle.CANCEL_PENDING,
        "cancel-after-partial",
        source="CANCEL",
        from_status=OrderLifecycle.PARTIALLY_FILLED,
        broker_order_id=BROKER_ID,
        filled_quantity=Decimal(1),  # a later, smaller figure
        occurred_at=PRIOR,
    )
    assert tracker.recorded_filled_quantity("intent-1", current_account_id=ACCOUNT) == Decimal(3)


# --- S-12: the report says WHY a blank-reference observation did not settle the intent ---------
# The behaviour itself is pinned in tests/integration/test_legacy_resolver_blank_ref.py; the typed
# reason (not "absent from the snapshots", which would be false) is pinned here.


@pytest.mark.parametrize("local", SENT_ACTIVE[:1] + SENT_ACTIVE[2:], ids=lambda x: x.value)
def test_a_blank_reference_row_that_would_settle_is_refused_with_its_own_reason(
    rig: _Rig, local: OrderLifecycle
) -> None:
    rig.seed(local)
    rig.venue.open = [_row(OrderLifecycle.FILLED, "10", ref=None)]  # owned by the persisted permId

    report = rig.reconcile()

    assert rig.status() is local and report.permits_new_opening_order is False
    (item,) = report.unresolved
    assert "absent" not in item.reason
    assert "no order reference" in item.reason and "never settles a terminal state" in item.reason


# --- LEGACY-5 r1 (Daybreak P2-1): the recorded-fills check is atomic with the terminal write ----


def _late_execution(quantity: str) -> BrokerExecution:
    return BrokerExecution(
        execution_id=f"exec-late-{quantity}",
        account_id=ACCOUNT,
        broker_order_id=BROKER_ID,
        permanent_id=PERM_ID,
        client_id=CLIENT_ID,
        order_ref=REF,
        contract=UnderlyingContract(con_id=CON_ID, symbol="AAPL"),
        side=OrderSide.BUY,
        quantity=Decimal(quantity),
        price=LIMIT,
        timestamp=NOW,
    )


def _pause_after_the_operators_maximum_read(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, concurrent: object
) -> list[Decimal]:
    """Run ``concurrent()`` once, right after ``operator_resolve`` has read the recorded maximum
    and before its terminal write: the deterministic form of Daybreak's interleaving."""

    tracker = rig.reconciler._tracker
    real = tracker.recorded_filled_quantity
    reads: list[Decimal] = []

    def paused(intent_id: str, *, current_account_id: str) -> Decimal:
        value = real(intent_id, current_account_id=current_account_id)
        reads.append(value)
        if len(reads) == 1:
            concurrent()  # type: ignore[operator]
        return value

    monkeypatch.setattr(tracker, "recorded_filled_quantity", paused)
    return reads


def _late_fill_reconciled(rig: _Rig) -> OrderRestartReconciliationReport:
    """The concurrent writer: a late exact-reference execution of 5 becomes visible (the listed
    row has left the open-orders list) and a full reconcile records it."""

    rig.venue.open = []
    rig.venue.execs = [_late_execution("5")]
    return rig.reconcile()


@pytest.mark.parametrize("typed", CONTRADICTED, ids=lambda lifecycle: lifecycle.value)
def test_r1_a_fill_recorded_between_the_maximum_read_and_the_terminal_write_is_not_overwritten(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, typed: OrderLifecycle
) -> None:
    """Daybreak P2-1: SUBMISSION_UNKNOWN, the operator's two stable reads list an exact-owned
    ``typed``/0 row and it reads the recorded maximum 0; a concurrent reconcile then records
    PARTIALLY_FILLED/5; the operator resumes with its stale row and maximum. The terminal write
    must re-check the maximum in the same transaction: refused, PARTIALLY_FILLED/5 kept, no
    ``typed``/0 event, the opening-order lock held."""

    rig.seed(OrderLifecycle.SUBMISSION_UNKNOWN)
    rig.venue.open = [_row(typed, "0")]
    concurrent: list[OrderRestartReconciliationReport] = []
    reads = _pause_after_the_operators_maximum_read(
        rig, monkeypatch, lambda: concurrent.append(_late_fill_reconciled(rig))
    )

    with pytest.raises(ValueError, match="recorded fills advanced"):
        rig.reconciler.operator_resolve(
            "intent-1",
            operator_note="checked the TWS order list",
            current_account_id=ACCOUNT,
            now=NOW,
        )

    assert reads[0] == Decimal(0)  # the operator really read the stale maximum
    (during,) = concurrent
    assert [update.lifecycle for update in during.applied_updates] == [
        OrderLifecycle.PARTIALLY_FILLED
    ]  # the concurrent writer really recorded the late fill
    assert rig.status() is OrderLifecycle.PARTIALLY_FILLED
    events = rig.repo.events("intent-1", current_account_id=ACCOUNT)
    assert events[-1].to_status is OrderLifecycle.PARTIALLY_FILLED
    assert events[-1].filled_quantity == Decimal(5)
    assert typed not in {event.to_status for event in events}
    rig.venue.open, rig.venue.execs = [_row(typed, "0")], []
    after = rig.reconcile()
    assert [item.intent_id for item in after.remaining_active] == ["intent-1"]
    assert after.permits_new_opening_order is False


def test_r1_the_same_interleaving_without_a_new_fill_still_settles(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: the pause alone changes nothing. A concurrent reconcile that records no
    fill lets the operator's CANCELLED/0 write land (the guard refuses only an ADVANCED maximum)."""

    rig.seed(OrderLifecycle.SUBMISSION_UNKNOWN)
    rig.venue.open = [_row(OrderLifecycle.CANCELLED, "0")]
    _pause_after_the_operators_maximum_read(rig, monkeypatch, lambda: None)

    assert (
        rig.reconciler.operator_resolve(
            "intent-1",
            operator_note="checked the TWS order list",
            current_account_id=ACCOUNT,
            now=NOW,
        )
        is False
    )
    assert rig.status() is OrderLifecycle.CANCELLED


def test_r1_the_reconcile_report_path_refuses_the_same_stale_terminal_write(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy resolver's other caller: ``reconcile_report`` reads the maximum, then another
    tracker writer (a live order-status callback) records PARTIALLY_FILLED/5 before the pass's
    terminal write. The write is refused atomically and the report names it as unresolved."""

    from chronos.orders.tracker import OrderStatusUpdate

    rig.seed(OrderLifecycle.SUBMISSION_UNKNOWN)
    rig.venue.open = [_row(OrderLifecycle.CANCELLED, "0")]
    tracker = rig.reconciler._tracker
    real = tracker.recorded_filled_quantity
    reads: list[Decimal] = []

    def paused(intent_id: str, *, current_account_id: str) -> Decimal:
        value = real(intent_id, current_account_id=current_account_id)
        reads.append(value)
        if len(reads) == 1:
            outcome = tracker.ingest(
                OrderStatusUpdate(
                    intent_id="intent-1",
                    broker_order_id=BROKER_ID,
                    permanent_id=PERM_ID,
                    client_id=CLIENT_ID,
                    lifecycle=OrderLifecycle.PARTIALLY_FILLED,
                    filled_quantity=Decimal(5),
                    remaining_quantity=QUANTITY - 5,
                    occurred_at=NOW,
                ),
                current_account_id=ACCOUNT,
            )
            assert outcome.lifecycle_changed  # the concurrent fill really landed
        return value

    monkeypatch.setattr(tracker, "recorded_filled_quantity", paused)
    report = rig.reconcile()

    assert reads[0] == Decimal(0)
    assert report.applied_updates == ()
    (item,) = report.unresolved
    assert item.transition_applied is False
    assert item.reason.startswith("recorded fills advanced") and "5 filled on record" in item.reason
    assert rig.status() is OrderLifecycle.PARTIALLY_FILLED
    assert [i.intent_id for i in report.remaining_active] == ["intent-1"]
    assert report.permits_new_opening_order is False


# --- LEGACY-5 r1: the repository's conditional write, directly ---------------------------------


@pytest.mark.parametrize("attempted", ["0", "2", None], ids=["zero", "below", "missing"])
def test_r1_record_transition_refuses_a_figure_below_the_recorded_maximum(
    rig: _Rig, attempted: str | None
) -> None:
    from chronos.persistence.order_repositories import RecordedFillsAdvanced

    rig.seed(OrderLifecycle.PARTIALLY_FILLED)  # 0, then 3 recorded
    before = rig.repo.events("intent-1", current_account_id=ACCOUNT)

    with pytest.raises(RecordedFillsAdvanced) as refused:
        rig.repo.record_transition(
            intent_id="intent-1",
            event_key="intent-1:cancel-zero",
            source="RECONCILE",
            from_status=OrderLifecycle.PARTIALLY_FILLED,
            to_status=OrderLifecycle.CANCELLED,
            current_account_id=ACCOUNT,
            filled_quantity=None if attempted is None else Decimal(attempted),
            occurred_at=NOW,
            enforce_from_status=True,
            refuse_below_recorded_fills=True,
        )

    assert refused.value.recorded == Decimal(3)
    assert refused.value.attempted == Decimal(attempted or "0")
    assert rig.repo.events("intent-1", current_account_id=ACCOUNT) == before  # nothing written
    assert rig.status() is OrderLifecycle.PARTIALLY_FILLED  # status untouched


@pytest.mark.parametrize("attempted", ["3", "4"], ids=["at", "above"])
def test_r1_record_transition_writes_a_figure_at_or_above_the_recorded_maximum(
    rig: _Rig, attempted: str
) -> None:
    rig.seed(OrderLifecycle.PARTIALLY_FILLED)
    assert rig.repo.record_transition(
        intent_id="intent-1",
        event_key="intent-1:cancel-at",
        source="RECONCILE",
        from_status=OrderLifecycle.PARTIALLY_FILLED,
        to_status=OrderLifecycle.CANCELLED,
        current_account_id=ACCOUNT,
        filled_quantity=Decimal(attempted),
        occurred_at=NOW,
        enforce_from_status=True,
        refuse_below_recorded_fills=True,
    )
    assert rig.status() is OrderLifecycle.CANCELLED


def test_r1_the_guard_is_opt_in_other_writers_are_unchanged(rig: _Rig) -> None:
    """Every other ``record_transition`` caller keeps today's behaviour (the default is False)."""

    rig.seed(OrderLifecycle.PARTIALLY_FILLED)
    assert rig.repo.record_transition(
        intent_id="intent-1",
        event_key="intent-1:cancel-default",
        source="CANCEL",
        from_status=OrderLifecycle.PARTIALLY_FILLED,
        to_status=OrderLifecycle.CANCELLED,
        current_account_id=ACCOUNT,
        filled_quantity=Decimal(0),
        occurred_at=NOW,
    )
    assert rig.status() is OrderLifecycle.CANCELLED
