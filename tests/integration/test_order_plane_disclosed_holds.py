"""Documents current behaviour: the two INTEGRATION-READ findings (2026-11-06), not fixed.

These pins are NOT the wanted behaviour. Each fix is the owner's decision (an audited operator exit;
a propose that gathers evidence before it creates the row), so these tests make that fix deliberate:
a change that clears the hold or stops counting the draft must change the matching assertion here
and the sentences in ``docs/limitations.md`` and ``docs/IBKR_RUNBOOK.md`` together.

INTEGRATION P3-1: a recorded fill figure above the venue's final terminal figure holds the intent
in CANCEL_PENDING across passes, a restart and the next session, blocks every new order submission
through Chronos (closing orders included: the readiness gate has no open/close distinction), clears
only if the venue later reports a figure at or above the recorded one, and no operator route clears
it. INTEGRATION P3-2: a propose abandoned by a broker read failure
leaves a DRAFT with no risk decision that the daily opening cap still counts.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.integration.test_order_pipeline import _Harness, _short_put_intent
from tests.support.order_fakes import (
    FIXED_NOW,
    PAPER_ACCOUNT,
    FakeBroker,
    paper_settings,
    stock_contract,
)
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
from chronos.domain.account_freshness import AccountSummaryFreshnessError
from chronos.domain.enums import OrderIntent, OrderSide, ProductFamily, ReconciliationStatus
from chronos.domain.enums import OrderLifecycle as L
from chronos.domain.models import AccountSummary, BrokerPosition, CancellationResult
from chronos.orders.evidence import BrokerRiskEvidenceProvider
from chronos.orders.intent import build_stock_intent
from chronos.orders.mutations import OrderCancellationService, OrderMutationError
from chronos.orders.reconciliation_readiness import ReadinessBlocker, ReconciliationReadiness
from chronos.orders.reconciliation_recovery import OrderRestartReconciler
from chronos.orders.risk import OrderRiskCheck, OrderRiskDecision, RiskEvidence
from chronos.orders.submission import SubmissionRefusalCode
from chronos.orders.tracker import OrderTracker
from chronos.persistence.order_repositories import OrderIntentRepository, OrderTrackerRepository
from chronos.persistence.repositories import LocalReconciliationRepository
from chronos.runtime import AppRuntime
from chronos.services.reconciliation import ReconciliationCoordinator, ReconciliationResult
from chronos.services.trading_hours import session_for

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


def test_documents_that_a_manual_close_in_tws_neither_clears_the_hold_nor_is_recorded(
    rig: Rig,
) -> None:
    """A close the operator makes in TWS is a trade Chronos did not originate: the snapshot then
    carries a SELL execution with no reference, which is neither a witness nor an own fill, so the
    hold stands, nothing is written, and the permit stays refused."""

    _recorded_seven_then_venue_five(rig)
    rig.reconcile()
    before = rig.events()
    manual_close = venue_execution("manual-close", "5", NOW, ref=None, perm=None)
    rig.venue.execs.append(manual_close.model_copy(update={"side": OrderSide.SELL}))

    report = rig.reconcile()

    assert rig.status() is L.CANCEL_PENDING and rig.events() == before
    assert FLOOR_HOLD in report.unresolved[0].reason
    assert not report.permits_new_opening_order


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


@pytest.mark.parametrize(
    "figure,outcome",
    [("5", "holds"), ("7", "settles"), ("9", "settles")],
    ids=["below-recorded", "at-recorded", "above-recorded"],
)
def test_documents_that_only_a_venue_figure_at_or_above_the_recorded_one_clears_the_hold(
    rig: Rig, figure: str, outcome: str
) -> None:
    rig.seed()
    rig.event(L.PARTIALLY_FILLED, at=EARLIER_TODAY, filled="7", source="FILL_PROGRESS")
    rig.pending(at=EARLIER_TODAY)
    rig.venue.open = [venue_order(L.CANCELLED, figure)]
    rig.venue.execs = [venue_execution("venue", figure, EARLIER_TODAY)]

    report = rig.reconcile()

    if outcome == "holds":
        assert rig.status() is L.CANCEL_PENDING and FLOOR_HOLD in report.unresolved[0].reason
        assert not report.permits_new_opening_order
    else:
        assert rig.status() is L.CANCELLED and report.unresolved == ()
        assert rig.events()[-1].filled_quantity == Decimal(figure)
        assert report.permits_new_opening_order


class _StockCloseEvidence:
    """Canned risk evidence for closing 100 settled shares, at readiness' current generation."""

    def __init__(self) -> None:
        self.readiness: ReconciliationReadiness | None = None

    def gather(self, intent: object, *, now: datetime) -> RiskEvidence:
        assert self.readiness is not None
        snapshot = self.readiness.snapshot()
        return RiskEvidence(
            account=AccountSummary(
                account_id=PAPER_ACCOUNT,
                net_liquidation=Decimal("100000"),
                total_cash=Decimal("80000"),
                buying_power=Decimal("160000"),
                as_of=FIXED_NOW,
            ),
            reconciliation_status=ReconciliationStatus.RECONCILED,
            reconciliation_generation=snapshot.generation,
            reconciliation_session_id=snapshot.session_id,
            session=session_for(ProductFamily.STOCK, now=now, broker_confirms_open=True),
            opening_orders_today=0,
            settled_long_shares=Decimal(100),
        )


class _CleanPortfolio:
    """The portfolio half of readiness: reconciled, no broker open orders; orders are under test."""

    def reconcile(self) -> ReconciliationResult:
        return ReconciliationResult.model_construct(
            status=ReconciliationStatus.RECONCILED,
            snapshot=SimpleNamespace(open_orders=()),
            symbols=(),
            opening_actions_locked=True,
            reasons=(),
        )


def _runtime_over(harness: _Harness, venue: ScriptedVenue) -> AppRuntime:
    """The REAL readiness publisher over the harness's real service, readiness and connection."""

    runtime = AppRuntime.__new__(AppRuntime)
    runtime.reconciliation_readiness = harness.readiness
    runtime.order_management = harness.service
    runtime.reconciliation = cast(Any, _CleanPortfolio())
    runtime.broker = cast(Any, venue)
    runtime.connection = harness.connection
    runtime.persistence_key = TEST_PERSISTENCE_KEY
    return runtime


@pytest.mark.parametrize(
    "hold", [False, True], ids=["control-close-transmits", "hold-blocks-close"]
)
def test_documents_the_readiness_gate_has_no_open_close_distinction_with_the_portfolio_stubbed(
    hold: bool,
) -> None:
    """What this stubs and what it proves, and what it does NOT prove.

    Stubbed: the portfolio half of readiness (``_CleanPortfolio``: reconciled, no broker open
    orders, no positions seen), the risk evidence (``_StockCloseEvidence``: canned RECONCILED
    evidence with 100 settled shares at readiness' current generation, so the risk engine approves
    the close) and the venue (the scripted fake broker). Proved: the readiness gate has no
    open/close distinction: with only the order half able to block, the real planner's hold
    (FILL_PROGRESS 7 against the venue's CANCELLED@5) makes the real
    ``AppRuntime.reconcile_submission_readiness`` publish PENDING on the restart blockers alone,
    and the real paper boundary refuses a real CLOSE_LONG_STOCK with RECONCILIATION_NOT_READY and
    no broker call, while the same close transmits without the hold. NOT proved: that the hold is
    what stops closes in a real runtime. It is not: the real portfolio half refuses a close for any
    held position, with or without the hold (the next pin), because a nonzero broker position is
    MANUAL_REVIEW with HELD_POSITION_UNPROVEN (services/reconciliation.py). The live branch's
    identical ``readiness.ready`` check (submission.py) is read, not run, because live stays off."""

    venue = ScriptedVenue()
    evidence = _StockCloseEvidence()
    harness = _Harness(venue, paper_settings(), evidence_provider=cast(Any, evidence))
    evidence.readiness = harness.readiness
    try:
        if hold:
            book = Rig(
                harness.db,
                venue,
                harness.intents,
                harness.tracker_repo,
                harness.tracker,
                harness.readiness,
                harness.connection,
                cast(Any, None),
                PAPER_ACCOUNT,
            )
            _recorded_seven_then_venue_five(book)
        published = _runtime_over(harness, venue).reconcile_submission_readiness(
            now=FIXED_NOW, trigger="operator"
        )
        close = build_stock_intent(
            account_id=PAPER_ACCOUNT,
            intent=OrderIntent.CLOSE_LONG_STOCK,
            contract=stock_contract(),
            quantity=100,
            limit_price=Decimal("100"),
            correlation_id="CHR-ORD-" + "C" * 32,
            intent_id="close-1",
        )
        proposal = harness.service.propose(close, now=FIXED_NOW)
        assert proposal.risk.approved
        harness.service.preview(close, now=FIXED_NOW)
        harness.service.confirm(close, risk_decision_id=proposal.risk.decision_id, now=FIXED_NOW)

        outcome = harness.service.submit(close, writer_lease_held=True, now=FIXED_NOW)

        if hold:
            assert published.readiness.status is ReconciliationStatus.PENDING
            assert {
                ReadinessBlocker.RESTART_SENT_ACTIVE,
                ReadinessBlocker.RESTART_BROKER_EVIDENCE_INCOMPLETE,
            } <= published.readiness.blockers
            assert FLOOR_HOLD in published.restart.unresolved[0].reason
            assert not outcome.submitted
            assert outcome.refusal is SubmissionRefusalCode.RECONCILIATION_NOT_READY
            assert venue.submit_calls == []
        else:
            assert published.readiness.ready
            assert outcome.submitted and outcome.refusal is SubmissionRefusalCode.NOT_REFUSED
            sent = [(r.side.value, str(r.quantity), r.transmit) for r in venue.submit_calls]
            assert sent == [("SELL", "100", True)]
    finally:
        harness.close()


def _held_five_shares(venue: ScriptedVenue) -> None:
    venue._positions = (
        BrokerPosition(
            account_id=PAPER_ACCOUNT,
            contract=stock_contract(),
            quantity=Decimal(5),
            average_cost=Decimal("100"),
            market_price=Decimal("100"),
        ),
    )


@pytest.mark.parametrize("hold", [False, True], ids=["position-no-hold", "position-and-hold"])
def test_documents_that_a_held_position_alone_is_manual_review_and_refuses_a_close_hold_or_not(
    hold: bool,
) -> None:
    """The fact an operator needs: through the REAL portfolio half (``ReconciliationCoordinator``
    over the harness's own database and the fake broker's positions), a 5-share position with no
    hold anywhere publishes MANUAL_REVIEW with HELD_POSITION_UNPROVEN and a real close is refused
    with no broker call; the floor hold only adds its restart blockers. Closes through Chronos are
    unavailable for every held position today, hold or not (docs/limitations.md, the reconciliation
    bullet); the exit for a position is TWS."""

    venue = ScriptedVenue()
    evidence = _StockCloseEvidence()
    harness = _Harness(venue, paper_settings(), evidence_provider=cast(Any, evidence))
    evidence.readiness = harness.readiness
    try:
        _held_five_shares(venue)
        if hold:
            book = Rig(
                harness.db,
                venue,
                harness.intents,
                harness.tracker_repo,
                harness.tracker,
                harness.readiness,
                harness.connection,
                cast(Any, None),
                PAPER_ACCOUNT,
            )
            _recorded_seven_then_venue_five(book)
        runtime = _runtime_over(harness, venue)
        runtime.reconciliation = cast(
            Any,
            ReconciliationCoordinator(
                harness.connection, LocalReconciliationRepository(harness.db.sessions), ("AAPL",)
            ),
        )
        published = runtime.reconcile_submission_readiness(now=FIXED_NOW, trigger="operator")
        close = build_stock_intent(
            account_id=PAPER_ACCOUNT,
            intent=OrderIntent.CLOSE_LONG_STOCK,
            contract=stock_contract(),
            quantity=5,
            limit_price=Decimal("100"),
            correlation_id="CHR-ORD-" + "H" * 32,
            intent_id="close-held",
        )
        proposal = harness.service.propose(close, now=FIXED_NOW)
        assert proposal.risk.approved
        harness.service.preview(close, now=FIXED_NOW)
        harness.service.confirm(close, risk_decision_id=proposal.risk.decision_id, now=FIXED_NOW)

        outcome = harness.service.submit(close, writer_lease_held=True, now=FIXED_NOW)

        assert published.readiness.status is ReconciliationStatus.MANUAL_REVIEW
        assert ReadinessBlocker.HELD_POSITION_UNPROVEN in published.readiness.blockers
        restart_blockers = {
            ReadinessBlocker.RESTART_SENT_ACTIVE,
            ReadinessBlocker.RESTART_BROKER_EVIDENCE_INCOMPLETE,
        }
        if hold:
            assert restart_blockers <= published.readiness.blockers
        else:
            assert not (restart_blockers & published.readiness.blockers)
        assert not outcome.submitted
        assert outcome.refusal is SubmissionRefusalCode.RECONCILIATION_NOT_READY
        assert venue.submit_calls == []
    finally:
        harness.close()


def test_documents_that_the_hold_alone_keeps_readiness_pending_on_a_flat_account() -> None:
    """What the hold adds beyond what already exists: with the venue row and its execution gone
    and no position anywhere, the REAL portfolio half is reconciled and readiness stays PENDING on
    the restart blockers alone, so no new opening order can be submitted on a flat account."""

    venue = ScriptedVenue()
    harness = _Harness(venue, paper_settings())
    try:
        book = Rig(
            harness.db,
            venue,
            harness.intents,
            harness.tracker_repo,
            harness.tracker,
            harness.readiness,
            harness.connection,
            cast(Any, None),
            PAPER_ACCOUNT,
        )
        _recorded_seven_then_venue_five(book)
        venue.open, venue.execs = [], []
        venue.completed = [venue_order(L.CANCELLED, "5")]
        runtime = _runtime_over(harness, venue)
        runtime.reconciliation = cast(
            Any,
            ReconciliationCoordinator(
                harness.connection, LocalReconciliationRepository(harness.db.sessions), ("AAPL",)
            ),
        )

        published = runtime.reconcile_submission_readiness(
            now=FIXED_NOW + timedelta(days=1), trigger="operator"
        )

        assert published.portfolio.status is ReconciliationStatus.RECONCILED
        assert published.readiness.status is ReconciliationStatus.PENDING
        # FLOORHOLD: the hold now also shows as itself, ADDED to the two restart blockers
        assert published.readiness.blockers == {
            ReadinessBlocker.RESTART_SENT_ACTIVE,
            ReadinessBlocker.RESTART_BROKER_EVIDENCE_INCOMPLETE,
            ReadinessBlocker.FILL_FLOOR_HOLD,
        }
        assert FLOOR_HOLD in published.restart.unresolved[0].reason
        (typed,) = published.restart.fill_floor_holds
        assert typed.intent_id == "intent-1" and typed.fill_floor_hold is not None
        assert typed.fill_floor_hold.as_dict() == {
            "basis": "WITNESSED_LOWER_BOUND",
            "floor_figure": "7",
            "venue_figure": "5",
        }
    finally:
        harness.close()


# --- INTEGRATION P3-2 ------------------------------------------------------------------------


class _FailsOnce(FakeBroker):
    """A broker that, when told to, fails the next propose: a positions read error, or an account
    summary older than ``account_summary_max_age_seconds`` (the #353 freshness refusal)."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next = 0
        self.stale_next = 0

    async def account_summary(self) -> AccountSummary:
        summary = await super().account_summary()
        if self.stale_next > 0:
            self.stale_next -= 1
            return summary.model_copy(update={"as_of": FIXED_NOW - timedelta(hours=1)})
        return summary

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


@pytest.mark.parametrize(
    "failures,cause",
    [(0, None), (3, "broker-read-failure"), (3, "stale-account-summary")],
    ids=["control", "three-drafts-broker-read-failure", "three-drafts-stale-account"],
)
def test_documents_that_a_draft_abandoned_inside_propose_counts_against_the_cap(
    failures: int, cause: str | None
) -> None:
    """Since #353 a stale account summary refuses inside evidence gathering exactly where a broker
    read error does: after the DRAFT row exists. Either leaves a draft with no decision that the
    cap counts, and the draft holds the order's idempotency key for the rest of the day."""

    broker = _FailsOnce()
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
        expected = AccountSummaryFreshnessError if cause == "stale-account-summary" else BrokerError
        for index in range(1, failures + 1):
            if cause == "stale-account-summary":
                broker.stale_next = 1
            else:
                broker.fail_next = 1
            intent = _short_put_intent(
                "CHR-ORD-" + chr(64 + index) * 32,
                intent_id=f"intent-{index}",
                limit_price=Decimal(f"1.{20 + index}"),
            )
            with pytest.raises(expected):
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
        if failures:
            # the abandoned draft holds the order's idempotency key: a fresh propose of the same
            # order (new intent and correlation ids, as the route always makes) is a duplicate
            same_order_again = _short_put_intent(
                "CHR-ORD-" + "Y" * 32, intent_id="intent-again", limit_price=Decimal("1.21")
            )
            with pytest.raises(ValueError, match="Idempotency key already maps"):
                harness.service.propose(same_order_again, now=FIXED_NOW)  # type: ignore[arg-type]
    finally:
        harness.close()
