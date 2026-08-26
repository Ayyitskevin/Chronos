"""End-to-end order pipeline against a recording fake broker (Milestone 5).

Proves the safety-critical guarantees: the single ``transmit=True`` only ever
reaches the broker after the full gate chain passes; every refusal path leaves
``submit_calls == 0``; idempotency, partial-fill monotonicity, cancellation
through CANCEL_PENDING, modification, and SUBMISSION_UNKNOWN recovery all behave;
and no order is ever placed by a non-submission path.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
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

from chronos.broker.base import BrokerConnectionError, BrokerRefusedBeforeSend, BrokerSendGuard
from chronos.broker.connection import BrokerConnectionManager
from chronos.config.settings import Settings
from chronos.domain.enums import (
    IBEnvironment,
    OrderIntent,
    OrderLifecycle,
    ProductFamily,
    ReconciliationStatus,
)
from chronos.domain.models import (
    BrokerExecution,
    BrokerOrder,
    OrderRequest,
    OrderSubmission,
    UnderlyingContract,
)
from chronos.orders.intent import build_option_intent, build_stock_intent
from chronos.orders.mutations import OrderCancellationService, OrderModificationService
from chronos.orders.preview import OrderPreviewService
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.reconciliation_recovery import OrderRestartReconciler
from chronos.orders.risk import (
    OrderRiskEngine,
    QQQPositionManagementRiskEvidence,
    RiskEvidence,
)
from chronos.orders.service import OrderManagementService, RiskEvidenceProvider
from chronos.orders.submission import OrderSubmissionBoundary, SubmissionRefusalCode
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

    def __init__(
        self, broker: FakeBroker, settings: Settings, readiness: ReconciliationReadiness
    ) -> None:
        self._broker = broker
        self._settings = settings
        self._readiness = readiness

    def gather(self, intent: object, *, now: datetime) -> RiskEvidence:
        account = self._broker._positions
        del account
        readiness = self._readiness.snapshot()
        session = session_for(ProductFamily.OPTION, now=now, broker_confirms_open=True)
        return RiskEvidence(
            account=_account(),
            reconciliation_status=ReconciliationStatus.RECONCILED,
            reconciliation_generation=readiness.generation,
            reconciliation_session_id=readiness.session_id,
            session=session,
            wheel_eligible_action=OrderIntent.OPEN_SHORT_PUT,
            # Stated, not defaulted: since M10 the daily cap refuses an intent
            # whose opening count is unknown, so a canned provider has to say
            # what the count is (R-25).
            opening_orders_today=0,
        )


class _QQQEvidence:
    def gather(self, intent: object, *, now: datetime) -> RiskEvidence:
        return RiskEvidence(
            account=_account(),  # type: ignore[arg-type]
            reconciliation_status=ReconciliationStatus.RECONCILED,
            reconciliation_generation=0,
            reconciliation_session_id="qqq-entry-recon",
            session=session_for(ProductFamily.STOCK, now=now, broker_confirms_open=True),
            opening_orders_today=0,
            qqq_position_management=QQQPositionManagementRiskEvidence(
                candidate_spec_sha256=(
                    "59348ca3da9e9b68ec4edd1fc54572783e9256ae9c55ac18ffe844c0b4b78054"
                ),
                source_evidence_digest="d" * 64,
                as_of=FIXED_NOW - timedelta(minutes=1),
                signal_time_entry_basis_usd=Decimal(100),
                signal_time_initial_stop_price_usd=Decimal(99),
                signal_time_risk_distance_usd=Decimal(1),
                marked_strategy_nav_usd=Decimal(3000),
                unit_exposure_cvar_loss_fraction=Decimal("0.05"),
            ),
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
    def __init__(
        self,
        broker: FakeBroker,
        settings: Settings,
        *,
        evidence_provider: RiskEvidenceProvider | None = None,
    ) -> None:
        self.broker = broker
        self.settings = settings
        self.db = Database("sqlite:///:memory:")
        self.db.initialize()
        self.db.bind_scope(broker_mode="ibkr", environment="paper", account_id=PAPER_ACCOUNT)
        self.readiness = ReconciliationReadiness()
        self.connection = BrokerConnectionManager(broker, readiness=self.readiness)
        self.connection.start()
        assert self.readiness.complete(
            expected_generation=self.readiness.snapshot().generation,
            status=ReconciliationStatus.RECONCILED,
            reason="test harness broker/local parity",
            reconciled_at=FIXED_NOW,
        )
        intents = OrderIntentRepository(self.db.sessions)
        tracker_repo = OrderTrackerRepository(self.db.sessions)
        confirmations = OrderConfirmationRepository(self.db.sessions)
        self.confirmations = confirmations
        risk_decisions = RiskDecisionRepository(self.db.sessions)
        self.risk_decisions = risk_decisions
        tracker = OrderTracker(intents, tracker_repo)
        self.intents = intents
        self.tracker = tracker
        self.tracker_repo = tracker_repo
        boundary = OrderSubmissionBoundary(
            settings=settings,
            connection=self.connection,
            intents=intents,
            confirmations=confirmations,
            tracker=tracker_repo,
            reconciliation_readiness=self.readiness,
        )
        self.service = OrderManagementService(
            settings=settings,
            environment=IBEnvironment.PAPER,
            account_id=PAPER_ACCOUNT,
            evidence_provider=(
                evidence_provider or _CannedEvidence(broker, settings, self.readiness)
            ),
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
            tracker_repo=tracker_repo,
            reconciler=OrderRestartReconciler(
                connection=self.connection,
                intents=intents,
                tracker=tracker,
                reconciliation_readiness=self.readiness,
                expected_broker_client_id=settings.ib_client_id,
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


def _short_put_intent(
    correlation: str = "CHR-ORD-" + "A" * 32,
    *,
    intent_id: str = "intent-1",
    limit_price: Decimal = Decimal("1.20"),
) -> object:
    return build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=option_contract(),
        quantity=1,
        limit_price=limit_price,
        correlation_id=correlation,
        intent_id=intent_id,
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


def test_propose_persists_typed_qqq_management_risk_for_post_fill_admission() -> None:
    settings = paper_settings(symbol_allowlist=("QQQ",))
    readiness = ReconciliationReadiness(session_id="qqq-entry-recon")
    assert readiness.complete(
        expected_generation=0,
        status=ReconciliationStatus.RECONCILED,
        reason="QQQ entry evidence is current",
        reconciled_at=FIXED_NOW,
    )

    broker = FakeBroker()
    h = _Harness(broker, settings, evidence_provider=_QQQEvidence())
    try:
        intent = build_stock_intent(
            account_id=PAPER_ACCOUNT,
            intent=OrderIntent.OPEN_LONG_STOCK,
            contract=UnderlyingContract(con_id=320227571, symbol="QQQ"),
            quantity=3,
            limit_price=Decimal(101),
            correlation_id="CHR-ORD-" + "C" * 32,
            intent_id="intent-qqq-risk",
        )

        proposed = h.service.propose(intent, now=FIXED_NOW)
        stored = h.risk_decisions.get(
            proposed.risk.decision_id,
            current_account_id=PAPER_ACCOUNT,
        )

        assert proposed.risk.approved
        assert stored is not None
        assert stored.evidence["qqq_position_management"] == {
            "schema_version": "chronos-qqq-position-risk-v1",
            "candidate_spec_sha256": (
                "59348ca3da9e9b68ec4edd1fc54572783e9256ae9c55ac18ffe844c0b4b78054"
            ),
            "source_evidence_digest": "d" * 64,
            "as_of": "2026-07-17T14:59:00Z",
            "signal_time_entry_basis_usd": "100",
            "signal_time_initial_stop_price_usd": "99",
            "signal_time_risk_distance_usd": "1",
            "marked_strategy_nav_usd": "3000",
            "unit_exposure_cvar_loss_fraction": "0.05",
        }
    finally:
        h.close()


def test_qqq_management_risk_is_authorizing_not_inert() -> None:
    settings = paper_settings(symbol_allowlist=("QQQ",))
    h = _Harness(FakeBroker(), settings, evidence_provider=_QQQEvidence())
    try:
        intent = build_stock_intent(
            account_id=PAPER_ACCOUNT,
            intent=OrderIntent.OPEN_LONG_STOCK,
            contract=UnderlyingContract(con_id=320227571, symbol="QQQ"),
            quantity=31,
            limit_price=Decimal(101),
            correlation_id="CHR-ORD-" + "D" * 32,
            intent_id="intent-qqq-risk-breach",
        )

        proposed = h.service.propose(intent, now=FIXED_NOW)

        assert not proposed.risk.approved
        check = next(
            item for item in proposed.risk.checks if item.name == "qqq_position_management_risk"
        )
        assert check.status.value == "FAIL"
        assert "native stop" in check.detail
        assert "CVaR" in check.detail
        assert "gross" in check.detail
        assert h.service.get("intent-qqq-risk-breach").status is OrderLifecycle.REJECTED  # type: ignore[union-attr]
    finally:
        h.close()


def test_invalidated_reconciliation_refuses_before_persistence(harness: _Harness) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    harness.readiness.invalidate("broker disconnected after reconciliation")

    outcome = harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]

    assert outcome.refusal is SubmissionRefusalCode.RECONCILIATION_NOT_READY
    assert harness.broker.submit_calls == []
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.USER_CONFIRMED


def test_persisted_risk_decision_from_prior_process_session_is_stale(
    harness: _Harness,
) -> None:
    intent = _short_put_intent()
    proposal = harness.service.propose(intent, now=FIXED_NOW)  # type: ignore[arg-type]
    assert proposal.risk.approved
    harness.service.preview(intent, now=FIXED_NOW)  # type: ignore[arg-type]
    harness.service.confirm(
        intent,  # type: ignore[arg-type]
        risk_decision_id=proposal.risk.decision_id,
        now=FIXED_NOW,
    )
    persisted = harness.service._load_decision(proposal.risk.decision_id)
    old_provenance = harness.readiness.snapshot()

    fresh_readiness = ReconciliationReadiness(session_id="fresh-process-session")
    assert fresh_readiness.snapshot().generation == old_provenance.generation
    assert fresh_readiness.snapshot().session_id != old_provenance.session_id
    assert fresh_readiness.complete(
        expected_generation=old_provenance.generation,
        status=ReconciliationStatus.RECONCILED,
        reason="fresh process broker/local parity",
        reconciled_at=FIXED_NOW,
    )
    fresh_boundary = OrderSubmissionBoundary(
        settings=harness.settings,
        connection=harness.connection,
        intents=harness.intents,
        confirmations=harness.confirmations,
        tracker=harness.tracker_repo,
        reconciliation_readiness=fresh_readiness,
    )

    outcome = fresh_boundary.submit(
        intent=intent,  # type: ignore[arg-type]
        risk_decision=persisted,
        connected_account_id=PAPER_ACCOUNT,
        broker_environment_is_paper=True,
        writer_lease_held=True,
        now=FIXED_NOW,
    )
    assert outcome.refusal is SubmissionRefusalCode.RISK_EVIDENCE_STALE
    assert harness.broker.submit_calls == []


def test_reconciliation_generation_race_resolves_without_transmit(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    original_claim_check = harness.readiness.submission_claim_is_current
    checks = 0

    def invalidate_between_cas_and_transmit(*, expected_generation: int) -> bool:
        nonlocal checks
        checks += 1
        if checks == 1:
            harness.readiness.invalidate("disconnect raced pre-submit persistence")
        return original_claim_check(expected_generation=expected_generation)

    monkeypatch.setattr(
        harness.readiness, "submission_claim_is_current", invalidate_between_cas_and_transmit
    )
    outcome = harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]

    assert outcome.refusal is SubmissionRefusalCode.RECONCILIATION_NOT_READY
    assert harness.broker.submit_calls == []
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.REJECTED


def test_invalidation_after_claim_check_is_refused_at_atomic_send(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    original_run = harness.connection.run
    calls = 0

    def run_with_invalidation(coroutine: object, *, timeout: float | None = None) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            harness.readiness.invalidate("scope changed at the adapter send boundary")
        return original_run(coroutine, timeout=timeout)  # type: ignore[arg-type, return-value]

    monkeypatch.setattr(harness.connection, "run", run_with_invalidation)
    outcome = harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]

    assert outcome.refusal is SubmissionRefusalCode.BROKER_REFUSED_BEFORE_SEND
    assert harness.broker.submit_calls == []
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.REJECTED


def test_second_submit_never_transmits_twice(harness: _Harness) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    first = harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
    assert first.submitted is True
    # A repeat submit of the same (now SUBMITTED) intent is refused and never
    # calls the broker again — the pre-submit idempotency guard / gate 7.
    second = harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
    assert second.submitted is False
    assert second.refusal is SubmissionRefusalCode.RECONCILIATION_NOT_READY
    assert len(harness.broker.submit_calls) == 1


def test_one_reconciliation_cannot_authorize_two_distinct_intents(
    harness: _Harness,
) -> None:
    first = _short_put_intent(intent_id="intent-1")
    second = _short_put_intent(
        "CHR-ORD-" + "B" * 32,
        intent_id="intent-2",
        limit_price=Decimal("1.25"),
    )
    _drive_to_confirmed(harness, first, FIXED_NOW)
    _drive_to_confirmed(harness, second, FIXED_NOW)

    first_result = harness.service.submit(first, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
    second_result = harness.service.submit(second, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]

    assert first_result.submitted is True
    assert second_result.refusal is SubmissionRefusalCode.RECONCILIATION_NOT_READY
    assert len(harness.broker.submit_calls) == 1
    stored = harness.service.get("intent-2")
    assert stored is not None and stored.status is OrderLifecycle.USER_CONFIRMED


def test_cancelled_after_partial_fill_reaches_terminal(harness: _Harness) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
    harness.tracker.ingest(
        OrderStatusUpdate(
            intent_id="intent-1",
            broker_order_id=9001,
            lifecycle=OrderLifecycle.PARTIALLY_FILLED,
            filled_quantity=Decimal("1"),
            remaining_quantity=Decimal("1"),
            occurred_at=FIXED_NOW,
        ),
        current_account_id=PAPER_ACCOUNT,
    )
    # A cancel that arrives after a partial fill must reach CANCELLED (terminal),
    # not stay PARTIALLY_FILLED forever.
    harness.tracker.ingest(
        OrderStatusUpdate(
            intent_id="intent-1",
            broker_order_id=9001,
            lifecycle=OrderLifecycle.CANCELLED,
            filled_quantity=Decimal("1"),
            remaining_quantity=Decimal("1"),
            occurred_at=FIXED_NOW,
        ),
        current_account_id=PAPER_ACCOUNT,
    )
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.CANCELLED


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


def test_modify_reprices_and_restart_matches_latest_persisted_price(harness: _Harness) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
    harness.service.modify("intent-1", Decimal("1.50"), now=FIXED_NOW)
    assert len(harness.broker.modify_calls) == 1
    assert harness.broker.modify_calls[0].new_limit_price == Decimal("1.50")
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.SUBMITTED
    harness.broker._open_orders = (
        BrokerOrder(
            broker_order_id=9001,
            client_id=harness.settings.ib_client_id,
            account_id=PAPER_ACCOUNT,
            order_ref="CHR-ORD-" + "A" * 32,
            contract=option_contract(),
            side=intent.side,  # type: ignore[attr-defined]
            quantity=Decimal("1"),
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("1"),
            limit_price=Decimal("1.50"),
            lifecycle=OrderLifecycle.SUBMITTED,
        ),
    )

    report = harness.service.reconcile_on_restart_report(now=FIXED_NOW)

    assert report.unresolved == ()
    assert report.applied_updates == ()
    assert [item.intent_id for item in report.proven] == ["intent-1"]
    assert report.proven[0].transition_applied is False


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
    report = harness.service.reconcile_on_restart_report(now=FIXED_NOW)
    assert len(report.applied_updates) == 1
    assert [item.intent_id for item in report.proven] == ["intent-1"]
    assert report.proven[0].broker_status is OrderLifecycle.SUBMITTED
    assert report.unresolved == ()
    assert report.remaining_active[0].status is OrderLifecycle.SUBMITTED
    assert report.permits_new_opening_order is False
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.SUBMITTED
    # Recovery reconciles; it never submits.
    assert harness.broker.submit_calls == []


@pytest.mark.parametrize(
    ("broker_account_id", "broker_limit_price"),
    [
        ("DU7654321", Decimal("1.20")),
        (PAPER_ACCOUNT, Decimal("9.99")),
    ],
)
def test_restart_reconciliation_rejects_colliding_reference_identity(
    harness: _Harness,
    broker_account_id: str,
    broker_limit_price: Decimal,
) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    harness.tracker_repo.record_transition(
        intent_id="intent-1",
        event_key="intent-1:test:SUBMISSION_UNKNOWN",
        source="TEST",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=OrderLifecycle.SUBMISSION_UNKNOWN,
        current_account_id=PAPER_ACCOUNT,
        occurred_at=FIXED_NOW,
    )
    harness.broker._open_orders = (
        BrokerOrder(
            broker_order_id=9500,
            client_id=17,
            account_id=broker_account_id,
            order_ref="CHR-ORD-" + "A" * 32,
            contract=option_contract(),
            side=intent.side,  # type: ignore[attr-defined]
            quantity=Decimal("1"),
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("1"),
            limit_price=broker_limit_price,
            lifecycle=OrderLifecycle.SUBMITTED,
        ),
    )

    report = harness.service.reconcile_on_restart_report(now=FIXED_NOW)

    assert report.applied_updates == ()
    assert report.proven == ()
    assert "conflicting account or economic identity" in report.unresolved[0].reason
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.SUBMISSION_UNKNOWN


@pytest.mark.parametrize(
    (
        "second_execution_id",
        "second_account_id",
        "second_order_id",
        "second_permanent_id",
        "second_client_id",
    ),
    [
        ("exec-2", "DU7654321", 9501, 19501, 17),
        ("exec-1", PAPER_ACCOUNT, 9500, 19500, 17),
        ("exec-2", PAPER_ACCOUNT, 9500, 19500, 18),
    ],
)
def test_restart_reconciliation_rejects_mixed_execution_identity(
    harness: _Harness,
    second_execution_id: str,
    second_account_id: str,
    second_order_id: int,
    second_permanent_id: int,
    second_client_id: int,
) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    harness.tracker_repo.record_transition(
        intent_id="intent-1",
        event_key="intent-1:test:SUBMISSION_UNKNOWN",
        source="TEST",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=OrderLifecycle.SUBMISSION_UNKNOWN,
        current_account_id=PAPER_ACCOUNT,
        occurred_at=FIXED_NOW,
    )
    harness.broker._executions = (
        BrokerExecution(
            execution_id="exec-1",
            account_id=PAPER_ACCOUNT,
            broker_order_id=9500,
            permanent_id=19500,
            client_id=17,
            order_ref="CHR-ORD-" + "A" * 32,
            contract=option_contract(),
            side=intent.side,  # type: ignore[attr-defined]
            quantity=Decimal("0.25"),
            price=Decimal("1.20"),
            timestamp=FIXED_NOW,
        ),
        BrokerExecution(
            execution_id=second_execution_id,
            account_id=second_account_id,
            broker_order_id=second_order_id,
            permanent_id=second_permanent_id,
            client_id=second_client_id,
            order_ref="CHR-ORD-" + "A" * 32,
            contract=option_contract(),
            side=intent.side,  # type: ignore[attr-defined]
            quantity=Decimal("0.25"),
            price=Decimal("1.20"),
            timestamp=FIXED_NOW,
        ),
    )

    report = harness.service.reconcile_on_restart_report(now=FIXED_NOW)

    assert report.applied_updates == ()
    assert report.proven == ()
    assert "conflicting account or economic identity" in report.unresolved[0].reason
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.SUBMISSION_UNKNOWN


@pytest.mark.parametrize(
    ("local_status", "broker_status", "filled_quantity", "remaining_quantity"),
    [
        (
            OrderLifecycle.SUBMITTED,
            OrderLifecycle.FILLED,
            Decimal("1"),
            Decimal("0"),
        ),
        (
            OrderLifecycle.PARTIALLY_FILLED,
            OrderLifecycle.FILLED,
            Decimal("1"),
            Decimal("0"),
        ),
        (
            OrderLifecycle.CANCEL_PENDING,
            OrderLifecycle.CANCELLED,
            Decimal("0"),
            Decimal("1"),
        ),
    ],
)
def test_restart_reconciliation_rejects_different_persisted_broker_order_id(
    harness: _Harness,
    local_status: OrderLifecycle,
    broker_status: OrderLifecycle,
    filled_quantity: Decimal,
    remaining_quantity: Decimal,
) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    harness.tracker_repo.record_transition(
        intent_id="intent-1",
        event_key=f"intent-1:test:{local_status.value}",
        source="TEST",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=local_status,
        current_account_id=PAPER_ACCOUNT,
        broker_order_id=9500,
        occurred_at=FIXED_NOW,
    )
    harness.broker._open_orders = (
        BrokerOrder(
            broker_order_id=9501,
            client_id=harness.settings.ib_client_id,
            account_id=PAPER_ACCOUNT,
            order_ref="CHR-ORD-" + "A" * 32,
            contract=option_contract(),
            side=intent.side,  # type: ignore[attr-defined]
            quantity=Decimal("1"),
            filled_quantity=filled_quantity,
            remaining_quantity=remaining_quantity,
            limit_price=Decimal("1.20"),
            lifecycle=broker_status,
        ),
    )

    report = harness.service.reconcile_on_restart_report(now=FIXED_NOW)

    assert report.applied_updates == ()
    assert report.proven == ()
    assert "different persisted broker order id" in report.unresolved[0].reason
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is local_status


def test_restart_reconciliation_rejects_single_wrong_execution_client(
    harness: _Harness,
) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    harness.tracker_repo.record_transition(
        intent_id="intent-1",
        event_key="intent-1:test:SUBMISSION_UNKNOWN",
        source="TEST",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=OrderLifecycle.SUBMISSION_UNKNOWN,
        current_account_id=PAPER_ACCOUNT,
        occurred_at=FIXED_NOW,
    )
    harness.broker._executions = (
        BrokerExecution(
            execution_id="exec-other-client",
            account_id=PAPER_ACCOUNT,
            broker_order_id=9500,
            permanent_id=19500,
            client_id=harness.settings.ib_client_id + 1,
            order_ref="CHR-ORD-" + "A" * 32,
            contract=option_contract(),
            side=intent.side,  # type: ignore[attr-defined]
            quantity=Decimal("1"),
            price=Decimal("1.20"),
            timestamp=FIXED_NOW,
        ),
    )

    report = harness.service.reconcile_on_restart_report(now=FIXED_NOW)

    assert report.applied_updates == ()
    assert report.proven == ()
    assert "conflicting account or economic identity" in report.unresolved[0].reason
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.SUBMISSION_UNKNOWN


def test_restart_reconciliation_rejects_wrong_client_working_order(
    harness: _Harness,
) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    harness.tracker_repo.record_transition(
        intent_id="intent-1",
        event_key="intent-1:test:SUBMISSION_UNKNOWN",
        source="TEST",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=OrderLifecycle.SUBMISSION_UNKNOWN,
        current_account_id=PAPER_ACCOUNT,
        occurred_at=FIXED_NOW,
    )
    harness.broker._open_orders = (
        BrokerOrder(
            broker_order_id=9500,
            client_id=harness.settings.ib_client_id + 1,
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

    report = harness.service.reconcile_on_restart_report(now=FIXED_NOW)

    assert report.applied_updates == ()
    assert report.proven == ()
    assert "conflicting account or economic identity" in report.unresolved[0].reason
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.SUBMISSION_UNKNOWN


@pytest.mark.parametrize(
    "active_status",
    [
        OrderLifecycle.SUBMITTED,
        OrderLifecycle.SUBMISSION_UNKNOWN,
        OrderLifecycle.PARTIALLY_FILLED,
        OrderLifecycle.CANCEL_PENDING,
    ],
)
def test_sent_active_intent_absent_from_broker_stays_unresolved(
    harness: _Harness, active_status: OrderLifecycle
) -> None:
    # Mere absence is not positive evidence that a sent order is terminal.
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    harness.tracker_repo.record_transition(
        intent_id="intent-1",
        event_key=f"intent-1:test:{active_status.value}",
        source="TEST",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=active_status,
        current_account_id=PAPER_ACCOUNT,
        occurred_at=FIXED_NOW,
    )

    report = harness.service.reconcile_on_restart_report(now=FIXED_NOW)

    assert report.applied_updates == ()
    assert [item.intent_id for item in report.unresolved] == ["intent-1"]
    assert report.unresolved[0].local_status is active_status
    assert report.broker_evidence_complete is False
    assert [(item.intent_id, item.status) for item in report.remaining_active] == [
        ("intent-1", active_status)
    ]
    assert report.permits_new_opening_order is False
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is active_status
    assert harness.broker.submit_calls == []


def test_restart_report_distinguishes_proven_noop_from_broker_absence(
    harness: _Harness,
) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    harness.tracker_repo.record_transition(
        intent_id="intent-1",
        event_key="intent-1:test:SUBMITTED",
        source="TEST",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=OrderLifecycle.SUBMITTED,
        current_account_id=PAPER_ACCOUNT,
        occurred_at=FIXED_NOW,
    )
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

    report = harness.service.reconcile_on_restart_report(now=FIXED_NOW)

    assert report.applied_updates == ()
    assert report.unresolved == ()
    assert report.broker_evidence_complete is True
    assert [item.intent_id for item in report.proven] == ["intent-1"]
    assert report.proven[0].transition_applied is False
    assert report.remaining_active[0].status is OrderLifecycle.SUBMITTED
    assert report.permits_new_opening_order is False
    assert harness.broker.submit_calls == []


# --- M8b chaos: real fault injection at broker.submit_order (paper path) ------
# The tests above force SUBMISSION_UNKNOWN synthetically; these drive the actual
# transmit-path exception handling by making broker.submit_order raise mid-call.


class _UnknownAcknowledgementBroker(FakeBroker):
    async def submit_order(
        self,
        request: OrderRequest,
        *,
        send_guard: BrokerSendGuard | None = None,
    ) -> OrderSubmission:
        submission = await super().submit_order(request, send_guard=send_guard)
        return submission.model_copy(update={"lifecycle": OrderLifecycle.SUBMISSION_UNKNOWN})


class _ReconciledBeforeAcknowledgementBroker(FakeBroker):
    def __init__(self) -> None:
        super().__init__()
        self.after_send: Callable[[OrderSubmission], None] | None = None

    async def submit_order(
        self,
        request: OrderRequest,
        *,
        send_guard: BrokerSendGuard | None = None,
    ) -> OrderSubmission:
        submission = await super().submit_order(request, send_guard=send_guard)
        if self.after_send is not None:
            self.after_send(submission)
        return submission


def test_broker_error_during_submit_leaves_recoverable_submission_unknown(
    harness: _Harness,
) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    # The socket drops mid-send: bytes may have left, so the outcome is ambiguous.
    harness.broker.submit_error = BrokerConnectionError("socket dropped mid-send")

    outcome = harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]

    assert outcome.submitted is False
    assert outcome.refusal is SubmissionRefusalCode.BROKER_SUBMIT_FAILED
    # The pre-submit CAS already persisted SUBMISSION_UNKNOWN; a failed send never
    # rolls it back and never auto-retries (the fake raised before recording).
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.SUBMISSION_UNKNOWN
    assert harness.broker.submit_calls == []

    # It is recoverable: the broker later shows the order working under our ref,
    # and restart reconciliation resolves it WITHOUT ever re-submitting.
    harness.broker.submit_error = None
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
    recovered = harness.service.get("intent-1")
    assert recovered is not None and recovered.status is OrderLifecycle.SUBMITTED
    assert harness.broker.submit_calls == []  # never re-sent


def test_unknown_ack_persists_broker_identity_and_rejects_wrong_id_recovery() -> None:
    broker = _UnknownAcknowledgementBroker()
    h = _Harness(broker, paper_settings())
    try:
        intent = _short_put_intent()
        _drive_to_confirmed(h, intent, FIXED_NOW)

        outcome = h.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]

        assert outcome.submitted is False
        assert outcome.refusal is SubmissionRefusalCode.BROKER_SUBMIT_FAILED
        assert outcome.submission is not None
        assert outcome.submission.broker_order_id == 9001
        assert outcome.submission.lifecycle is OrderLifecycle.SUBMISSION_UNKNOWN
        assert h.tracker.broker_order_id("intent-1", current_account_id=PAPER_ACCOUNT) == 9001
        identity_event = h.tracker_repo.events("intent-1", current_account_id=PAPER_ACCOUNT)[-1]
        assert identity_event.broker_order_id == 9001
        assert identity_event.evidence["permanent_id"] == 109001

        broker._open_orders = (
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
        report = h.service.reconcile_on_restart_report(now=FIXED_NOW)

        assert report.applied_updates == ()
        assert len(report.unresolved) == 1
        assert "different persisted broker order id" in report.unresolved[0].reason
        stored = h.service.get("intent-1")
        assert stored is not None and stored.status is OrderLifecycle.SUBMISSION_UNKNOWN
    finally:
        h.close()


def test_late_submit_ack_cannot_downgrade_reconciled_fill() -> None:
    broker = _ReconciledBeforeAcknowledgementBroker()
    h = _Harness(broker, paper_settings())
    try:
        intent = _short_put_intent()
        _drive_to_confirmed(h, intent, FIXED_NOW)

        def publish_fill(submission: OrderSubmission) -> None:
            assert h.tracker.ingest(
                OrderStatusUpdate(
                    intent_id="intent-1",
                    broker_order_id=submission.broker_order_id,
                    permanent_id=submission.permanent_id,
                    lifecycle=OrderLifecycle.FILLED,
                    filled_quantity=Decimal("1"),
                    remaining_quantity=Decimal("0"),
                    source="RECONCILE",
                    occurred_at=FIXED_NOW,
                ),
                current_account_id=PAPER_ACCOUNT,
            )

        broker.after_send = publish_fill
        outcome = h.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]

        assert outcome.submitted is True
        assert outcome.submission is not None
        assert outcome.submission.lifecycle is OrderLifecycle.FILLED
        stored = h.service.get("intent-1")
        assert stored is not None and stored.status is OrderLifecycle.FILLED
        events = h.tracker_repo.events("intent-1", current_account_id=PAPER_ACCOUNT)
        assert not any(
            event.source == "SUBMIT"
            and event.from_status is OrderLifecycle.SUBMISSION_UNKNOWN
            and event.to_status is OrderLifecycle.SUBMITTED
            for event in events
        )
    finally:
        h.close()


def test_broker_refused_before_send_during_submit_resolves_rejected_without_wedge(
    harness: _Harness,
) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    # The adapter refuses locally before any network send: the venue provably
    # never saw the order, so it resolves to REJECTED synchronously (no wedge).
    harness.broker.submit_error = BrokerRefusedBeforeSend("refused locally before send")

    outcome = harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]

    assert outcome.submitted is False
    assert outcome.refusal is SubmissionRefusalCode.BROKER_REFUSED_BEFORE_SEND
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
