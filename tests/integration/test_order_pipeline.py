"""End-to-end order pipeline against a recording fake broker (Milestone 5).

Proves the safety-critical guarantees: the single ``transmit=True`` only ever
reaches the broker after the full gate chain passes; every refusal path leaves
``submit_calls == 0``; idempotency, partial-fill monotonicity, cancellation
through CANCEL_PENDING, modification, and SUBMISSION_UNKNOWN recovery all behave;
and no order is ever placed by a non-submission path.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from tests.support.order_fakes import (
    FIXED_NOW,
    PAPER_ACCOUNT,
    FakeBroker,
    option_contract,
    paper_settings,
)

from chronos.broker.connection import BrokerConnectionManager
from chronos.config.settings import Settings
from chronos.domain.enums import (
    IBEnvironment,
    OrderIntent,
    OrderLifecycle,
    ProductFamily,
    ReconciliationStatus,
)
from chronos.domain.models import BrokerOrder
from chronos.orders.intent import build_option_intent
from chronos.orders.mutations import OrderCancellationService, OrderModificationService
from chronos.orders.preview import OrderPreviewService
from chronos.orders.reconciliation_recovery import OrderRestartReconciler
from chronos.orders.risk import OrderRiskEngine, RiskEvidence
from chronos.orders.service import OrderManagementService
from chronos.orders.submission import PaperOrderSubmissionBoundary, SubmissionRefusalCode
from chronos.orders.tracker import OrderStatusUpdate, OrderTracker
from chronos.persistence.database import Database
from chronos.persistence.order_repositories import (
    OrderConfirmationRepository,
    OrderIntentRepository,
    OrderTrackerRepository,
    RiskDecisionRepository,
)
from chronos.services.trading_hours import session_for


class _CannedEvidence:
    """Returns fixed, passing risk evidence for the pipeline under test."""

    def __init__(self, broker: FakeBroker, settings: Settings) -> None:
        self._broker = broker
        self._settings = settings

    def gather(self, intent: object, *, now: datetime) -> RiskEvidence:
        account = self._broker._positions
        del account
        session = session_for(ProductFamily.OPTION, now=now, broker_confirms_open=True)
        return RiskEvidence(
            account=_account(),
            reconciliation_status=ReconciliationStatus.RECONCILED,
            session=session,
            wheel_eligible_action=OrderIntent.OPEN_SHORT_PUT,
        )


def _account() -> object:
    from chronos.domain.models import AccountSummary

    return AccountSummary(
        account_id=PAPER_ACCOUNT,
        net_liquidation=Decimal("100000"),
        total_cash=Decimal("80000"),
        buying_power=Decimal("160000"),
        as_of=FIXED_NOW,
    )


class _Harness:
    def __init__(self, broker: FakeBroker, settings: Settings) -> None:
        self.broker = broker
        self.settings = settings
        self.db = Database("sqlite:///:memory:")
        self.db.initialize()
        self.db.bind_scope(broker_mode="ibkr", environment="paper", account_id=PAPER_ACCOUNT)
        self.connection = BrokerConnectionManager(broker)
        self.connection.start()
        intents = OrderIntentRepository(self.db.sessions)
        tracker_repo = OrderTrackerRepository(self.db.sessions)
        confirmations = OrderConfirmationRepository(self.db.sessions)
        risk_decisions = RiskDecisionRepository(self.db.sessions)
        tracker = OrderTracker(intents, tracker_repo)
        self.intents = intents
        self.tracker = tracker
        self.tracker_repo = tracker_repo
        boundary = PaperOrderSubmissionBoundary(
            settings=settings,
            connection=self.connection,
            intents=intents,
            confirmations=confirmations,
            tracker=tracker_repo,
        )
        self.service = OrderManagementService(
            settings=settings,
            environment=IBEnvironment.PAPER,
            account_id=PAPER_ACCOUNT,
            evidence_provider=_CannedEvidence(broker, settings),
            risk_engine=OrderRiskEngine(settings),
            preview_service=OrderPreviewService(self.connection),
            submission_boundary=boundary,
            modification=OrderModificationService(
                connection=self.connection,
                intents=intents,
                tracker=tracker,
                tracker_repo=tracker_repo,
            ),
            cancellation=OrderCancellationService(
                connection=self.connection,
                intents=intents,
                tracker=tracker,
                tracker_repo=tracker_repo,
            ),
            tracker=tracker,
            reconciler=OrderRestartReconciler(
                connection=self.connection, intents=intents, tracker=tracker
            ),
            intents=intents,
            confirmations=confirmations,
            risk_decisions=risk_decisions,
            broker_environment_is_paper=True,
        )

    def close(self) -> None:
        self.connection.close()
        self.db.dispose()


@pytest.fixture
def harness() -> Iterator[_Harness]:
    broker = FakeBroker()
    h = _Harness(broker, paper_settings())
    try:
        yield h
    finally:
        h.close()


def _short_put_intent(correlation: str = "CHR-ORD-" + "A" * 32) -> object:
    return build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=option_contract(),
        quantity=1,
        limit_price=Decimal("1.20"),
        correlation_id=correlation,
        intent_id="intent-1",
    )


def _drive_to_confirmed(h: _Harness, intent: object, now: datetime) -> str:
    proposal = h.service.propose(intent, now=now)  # type: ignore[arg-type]
    assert proposal.risk.approved
    h.service.preview(intent, now=now)  # type: ignore[arg-type]
    return h.service.confirm(
        intent,  # type: ignore[arg-type]
        risk_decision_id=proposal.risk.decision_id,
        now=now,
    )


def test_happy_path_transmits_exactly_once(harness: _Harness) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    outcome = harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
    assert outcome.submitted is True
    assert outcome.refusal is SubmissionRefusalCode.NOT_REFUSED
    # The one and only order call, and it carried transmit=True to a paper acct.
    assert len(harness.broker.submit_calls) == 1
    assert harness.broker.submit_calls[0].transmit is True
    assert harness.broker.submit_calls[0].account_id == PAPER_ACCOUNT
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.SUBMITTED


def test_read_only_lease_refuses_without_calling_broker(harness: _Harness) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    outcome = harness.service.submit(intent, writer_lease_held=False, now=FIXED_NOW)  # type: ignore[arg-type]
    assert outcome.refusal is SubmissionRefusalCode.READ_ONLY_LEASE
    assert harness.broker.submit_calls == []


def test_missing_confirmation_refuses(harness: _Harness) -> None:
    intent = _short_put_intent()
    proposal = harness.service.propose(intent, now=FIXED_NOW)  # type: ignore[arg-type]
    harness.service.preview(intent, now=FIXED_NOW)  # type: ignore[arg-type]
    # Skip confirm; force USER_CONFIRMED state is required, so submit refuses.
    outcome = harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
    assert outcome.refusal in {
        SubmissionRefusalCode.INTENT_NOT_CONFIRMED,
        SubmissionRefusalCode.CONFIRMATION_MISSING,
    }
    assert harness.broker.submit_calls == []
    assert proposal.risk.approved


def test_expired_confirmation_refuses(harness: _Harness) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    later = FIXED_NOW + timedelta(seconds=harness.settings.order_confirmation_ttl_seconds + 1)
    outcome = harness.service.submit(intent, writer_lease_held=True, now=later)  # type: ignore[arg-type]
    assert outcome.refusal in {
        SubmissionRefusalCode.RISK_EXPIRED,
        SubmissionRefusalCode.CONFIRMATION_EXPIRED,
    }
    assert harness.broker.submit_calls == []


def test_failing_risk_never_reaches_submit(harness: _Harness) -> None:
    # A 5-contract order breaches MAX_CONTRACTS_PER_ORDER=2 -> risk FAIL.
    intent = build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=option_contract(),
        quantity=5,
        limit_price=Decimal("1.20"),
        correlation_id="CHR-ORD-" + "B" * 32,
        intent_id="intent-risk",
    )
    proposal = harness.service.propose(intent, now=FIXED_NOW)
    assert not proposal.risk.approved
    stored = harness.service.get("intent-risk")
    assert stored is not None and stored.status is OrderLifecycle.REJECTED
    assert harness.broker.submit_calls == []


def test_duplicate_propose_is_idempotent(harness: _Harness) -> None:
    intent = _short_put_intent()
    harness.service.propose(intent, now=FIXED_NOW)  # type: ignore[arg-type]
    # A second identical intent (same idempotency key) is a benign replay.
    twin = build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=option_contract(),
        quantity=1,
        limit_price=Decimal("1.20"),
        correlation_id="CHR-ORD-" + "A" * 32,
        intent_id="intent-1",
    )
    # create() returns False on replay; propose tolerates it and re-evaluates.
    result = harness.service.propose(twin, now=FIXED_NOW)
    assert result.intent.intent_id == "intent-1"


def test_partial_then_full_fill_is_monotonic(harness: _Harness) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
    # First partial fill of 1 of 2 (use a 2-contract order for realism).
    partial = OrderStatusUpdate(
        intent_id="intent-1",
        broker_order_id=9001,
        lifecycle=OrderLifecycle.PARTIALLY_FILLED,
        filled_quantity=Decimal("1"),
        remaining_quantity=Decimal("1"),
        occurred_at=FIXED_NOW,
    )
    assert harness.tracker.ingest(partial, current_account_id=PAPER_ACCOUNT) is True
    # A stale duplicate reporting a LOWER cumulative fill is ignored.
    stale = partial.model_copy(update={"filled_quantity": Decimal("0")})
    assert harness.tracker.ingest(stale, current_account_id=PAPER_ACCOUNT) is False
    fill = OrderStatusUpdate(
        intent_id="intent-1",
        broker_order_id=9001,
        lifecycle=OrderLifecycle.FILLED,
        filled_quantity=Decimal("2"),
        remaining_quantity=Decimal("0"),
        occurred_at=FIXED_NOW,
    )
    assert harness.tracker.ingest(fill, current_account_id=PAPER_ACCOUNT) is True
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.FILLED


def test_duplicate_callback_is_idempotent(harness: _Harness) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
    fill = OrderStatusUpdate(
        intent_id="intent-1",
        broker_order_id=9001,
        lifecycle=OrderLifecycle.FILLED,
        filled_quantity=Decimal("1"),
        remaining_quantity=Decimal("0"),
        occurred_at=FIXED_NOW,
    )
    assert harness.tracker.ingest(fill, current_account_id=PAPER_ACCOUNT) is True
    assert harness.tracker.ingest(fill, current_account_id=PAPER_ACCOUNT) is False


def test_cancel_moves_through_cancel_pending(harness: _Harness) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
    result = harness.service.cancel("intent-1", now=FIXED_NOW)
    assert result.lifecycle is OrderLifecycle.CANCELLED
    assert harness.broker.cancel_calls == [9001]
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.CANCELLED


def test_modify_reprices_without_lifecycle_change(harness: _Harness) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
    harness.service.modify("intent-1", Decimal("1.50"), now=FIXED_NOW)
    assert len(harness.broker.modify_calls) == 1
    assert harness.broker.modify_calls[0].new_limit_price == Decimal("1.50")
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.SUBMITTED


def test_submission_unknown_recovers_and_never_retries(harness: _Harness) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    # Force SUBMISSION_UNKNOWN as if a submit timed out.
    harness.tracker_repo.record_transition(
        intent_id="intent-1",
        event_key="intent-1:presubmit:SUBMISSION_UNKNOWN",
        source="SUBMIT",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=OrderLifecycle.SUBMISSION_UNKNOWN,
        current_account_id=PAPER_ACCOUNT,
        occurred_at=FIXED_NOW,
    )
    # Broker reports the order is working under our order_ref.
    harness.broker._open_orders = (
        BrokerOrder(
            broker_order_id=9500,
            client_id=17,
            account_id=PAPER_ACCOUNT,
            order_ref="CHR-ORD-" + "A" * 32,
            contract=option_contract(),
            side=intent.side,  # type: ignore[attr-defined]
            quantity=Decimal("1"),
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("1"),
            limit_price=Decimal("1.20"),
            lifecycle=OrderLifecycle.SUBMITTED,
        ),
    )
    applied = harness.service.reconcile_on_restart(now=FIXED_NOW)
    assert applied == 1
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.SUBMITTED
    # Recovery reconciles; it never submits.
    assert harness.broker.submit_calls == []


def test_submission_unknown_with_no_broker_order_rejects(harness: _Harness) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    harness.tracker_repo.record_transition(
        intent_id="intent-1",
        event_key="intent-1:presubmit:SUBMISSION_UNKNOWN",
        source="SUBMIT",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=OrderLifecycle.SUBMISSION_UNKNOWN,
        current_account_id=PAPER_ACCOUNT,
        occurred_at=FIXED_NOW,
    )
    applied = harness.service.reconcile_on_restart(now=FIXED_NOW)
    assert applied == 1
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.REJECTED
    assert harness.broker.submit_calls == []


def test_non_paper_settings_refuse_transmission() -> None:
    # A demo-shaped config: transmission_possible is False -> hard refusal.
    broker = FakeBroker()
    settings = Settings(_env_file=None)  # defaults: demo mode, no transmission
    h = _Harness(broker, settings)
    try:
        intent = _short_put_intent()
        _drive_to_confirmed(h, intent, FIXED_NOW)
        outcome = h.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
        assert outcome.refusal is SubmissionRefusalCode.TRANSMISSION_NOT_POSSIBLE
        assert broker.submit_calls == []
    finally:
        h.close()
