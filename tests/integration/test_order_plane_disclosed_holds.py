"""Documents current behaviour: the two INTEGRATION-READ findings (2026-11-06), not fixed.

These pins are NOT the wanted behaviour. Each fix is the owner's decision (an audited operator exit;
a propose that gathers evidence before it creates the row), so these tests make that fix deliberate:
a change that clears the hold or stops counting the draft must change the matching assertion here
and the sentences in ``docs/limitations.md`` and ``docs/IBKR_RUNBOOK.md`` together.

INTEGRATION P3-1: a recorded fill figure above the venue's final terminal figure holds the intent
in CANCEL_PENDING across passes, a restart and the next session, blocks every new opening order,
and no operator route clears it. INTEGRATION P3-2: a propose abandoned by a broker read failure
leaves a DRAFT with no risk decision that the daily opening cap still counts.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from tests.integration.test_order_pipeline import _Harness, _short_put_intent
from tests.support.order_fakes import FIXED_NOW, FakeBroker, paper_settings
from tests.support.persistence_keys import TEST_PERSISTENCE_KEY
from tests.support.settle_rig import (
    CLIENT_ID,
    EARLIER_TODAY,
    NOW,
    Rig,
    ScriptedVenue,
    make_rig,
    venue_execution,
    venue_order,
)

from chronos.broker.base import BrokerError
from chronos.domain.enums import OrderLifecycle as L
from chronos.domain.models import BrokerPosition, CancellationResult
from chronos.orders.evidence import BrokerRiskEvidenceProvider
from chronos.orders.mutations import OrderCancellationService, OrderMutationError
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.reconciliation_recovery import OrderRestartReconciler
from chronos.orders.risk import OrderRiskCheck, OrderRiskDecision, RiskEvidence
from chronos.orders.tracker import OrderTracker
from chronos.persistence.order_repositories import OrderIntentRepository, OrderTrackerRepository

FLOOR_HOLD = "below the witnessed lower bound"


@pytest.fixture
def rig() -> Iterator[Rig]:
    instance = make_rig()
    try:
        yield instance
    finally:
        instance.close()


def _cancellation(rig: Rig) -> OrderCancellationService:
    return OrderCancellationService(
        connection=rig.connection,
        intents=rig.intents,
        tracker=rig.tracker,
        tracker_repo=rig.repo,
        reconciliation_readiness=rig.readiness,
    )


def _recorded_seven_then_venue_five(rig: Rig) -> None:
    """FILL_PROGRESS 7 on the book; the venue ends at CANCELLED@5 and agrees with itself."""

    rig.seed()
    rig.event(L.PARTIALLY_FILLED, at=EARLIER_TODAY, filled="7", source="FILL_PROGRESS")
    rig.pending(at=EARLIER_TODAY)
    rig.venue.open = [venue_order(L.CANCELLED, "5")]
    rig.venue.execs = [venue_execution("venue-five", "5", EARLIER_TODAY)]


# --- INTEGRATION P3-1 ------------------------------------------------------------------------


def test_documents_the_recorded_floor_hold_persisting_across_passes_restart_and_sessions(
    rig: Rig,
) -> None:
    _recorded_seven_then_venue_five(rig)
    before = rig.events()

    for _ in range(2):
        report = rig.reconcile()
        assert rig.status() is L.CANCEL_PENDING and rig.events() == before
        assert [FLOOR_HOLD in item.reason for item in report.unresolved] == [True]
        assert not report.permits_new_opening_order

    # a fresh process over the same database: the floor is persisted, the hold needs no memory
    intents = OrderIntentRepository(rig.db.sessions)
    repo = OrderTrackerRepository(rig.db.sessions)
    restarted = OrderRestartReconciler(
        connection=rig.connection,
        intents=intents,
        tracker=OrderTracker(intents, repo),
        reconciliation_readiness=ReconciliationReadiness(),
        expected_broker_client_id=CLIENT_ID,
        persistence_key=TEST_PERSISTENCE_KEY,
    )
    report = restarted.reconcile_report(current_account_id=rig.account, now=NOW)
    assert FLOOR_HOLD in report.unresolved[0].reason
    assert not report.permits_new_opening_order

    # the next session: the open-book row and the execution are gone; a completed record keeps 5
    next_session = NOW + timedelta(days=1)
    rig.venue.open, rig.venue.execs = [], []
    rig.venue.completed = [venue_order(L.CANCELLED, "5")]
    report = rig.reconcile(now=next_session)
    assert FLOOR_HOLD in report.unresolved[0].reason
    rig.venue.completed = []
    report = rig.reconcile(now=next_session)
    assert report.unresolved[0].reason == "cancel pending; order absent; no terminal record"
    assert rig.status() is L.CANCEL_PENDING and rig.events() == before
    assert not report.permits_new_opening_order


def test_documents_that_no_operator_route_clears_the_recorded_floor_hold(rig: Rig) -> None:
    _recorded_seven_then_venue_five(rig)
    rig.reconcile()
    assert rig.status() is L.CANCEL_PENDING

    with pytest.raises(ValueError, match="applies only to SUBMISSION_UNKNOWN"):
        rig.reconciler.operator_resolve(
            "intent-1",
            operator_note="checked in TWS: 5 filled",
            current_account_id=rig.account,
            now=NOW,
        )
    with pytest.raises(OrderMutationError, match="a cancel is already pending"):
        _cancellation(rig).cancel("intent-1", current_account_id=rig.account, now=NOW)
    assert rig.status() is L.CANCEL_PENDING


class _AcknowledgesFive(ScriptedVenue):
    """The D-67a route: the cancel acknowledgement carries the venue's explicit lower figure."""

    async def cancel_order(self, broker_order_id: int) -> CancellationResult:
        self.cancel_calls.append(broker_order_id)
        return CancellationResult(
            broker_order_id=broker_order_id,
            requested=True,
            lifecycle=L.CANCELLED,
            message="cancelled",
            timestamp=NOW,
            filled_quantity=Decimal("5"),
        )


def test_documents_that_a_lower_cancel_acknowledgement_lands_in_the_same_hold() -> None:
    rig = make_rig(_AcknowledgesFive())
    try:
        rig.seed()
        rig.event(L.PARTIALLY_FILLED, at=EARLIER_TODAY, filled="7", source="FILL_PROGRESS")

        with pytest.raises(OrderMutationError, match="reports 5 filled, below the 7 already"):
            _cancellation(rig).cancel("intent-1", current_account_id=rig.account, now=NOW)

        assert rig.status() is L.CANCEL_PENDING
        assert not rig.readiness.snapshot().ready
        rig.venue.open = [venue_order(L.CANCELLED, "5")]
        rig.venue.execs = [venue_execution("venue-five", "5", EARLIER_TODAY)]
        report = rig.reconcile()
        assert FLOOR_HOLD in report.unresolved[0].reason
        assert rig.status() is L.CANCEL_PENDING
    finally:
        rig.close()


# --- INTEGRATION P3-2 ------------------------------------------------------------------------


class _FailsPositionsOnce(FakeBroker):
    """A broker whose positions read fails once when told to (a transient outage at propose)."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next = 0

    async def positions(self) -> tuple[BrokerPosition, ...]:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise BrokerError("positions unavailable (injected transient failure)")
        return await super().positions()


class _LateProvider:
    """Lets the REAL evidence provider be built on the harness's own connection after it exists."""

    inner: BrokerRiskEvidenceProvider | None = None

    def gather(self, intent: object, *, now: datetime) -> RiskEvidence:
        assert self.inner is not None
        return self.inner.gather(intent, now=now)  # type: ignore[arg-type]


def _cap_check(decision: OrderRiskDecision) -> OrderRiskCheck:
    return next(check for check in decision.checks if check.name == "max_opening_orders_per_day")


@pytest.mark.parametrize("failures", [0, 3], ids=["control", "three-abandoned-drafts"])
def test_documents_that_a_draft_abandoned_by_a_broker_read_failure_counts_against_the_cap(
    failures: int,
) -> None:
    broker = _FailsPositionsOnce()
    settings = paper_settings(max_opening_orders_per_day=3)
    late = _LateProvider()
    harness = _Harness(broker, settings, evidence_provider=late)
    try:
        late.inner = BrokerRiskEvidenceProvider(
            connection=harness.connection,
            settings=settings,
            reconciliation_readiness=harness.readiness,
            intents=harness.intents,
        )
        account = harness.service.account_id
        for index in range(1, failures + 1):
            broker.fail_next = 1
            intent = _short_put_intent(
                "CHR-ORD-" + chr(64 + index) * 32,
                intent_id=f"intent-{index}",
                limit_price=Decimal(f"1.{20 + index}"),
            )
            with pytest.raises(BrokerError):
                harness.service.propose(intent, now=FIXED_NOW)  # type: ignore[arg-type]
            abandoned = harness.intents.get(f"intent-{index}", current_account_id=account)
            assert abandoned is not None and abandoned.status is L.DRAFT
            assert abandoned.risk_snapshot_id is None  # no decision was ever stored

        clean = _short_put_intent(
            "CHR-ORD-" + "Z" * 32, intent_id="intent-clean", limit_price=Decimal("1.30")
        )
        result = harness.service.propose(clean, now=FIXED_NOW)  # type: ignore[arg-type]
        check = _cap_check(result.risk)
        if failures:
            assert check.status.value == "FAIL"
            assert check.detail == "4 would exceed the 3 daily cap"
        else:
            assert check.status.value == "PASS"
            assert check.detail == "1 <= 3"
    finally:
        harness.close()
