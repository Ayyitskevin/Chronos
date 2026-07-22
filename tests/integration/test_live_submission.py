"""Milestone 7 recording-spy validation of the LIVE submission branch (ADR-0009 §10).

The spy broker is the only "venue": the happy path emits exactly ONE correct
live order object (LMT, DAY, RTH-only, tick-valid, verified live account,
transmit=True), and every adversarial case leaves ``submit_calls == 0``. The
refused-before-send and bounded recovery paths are proven; broker absence stays
locked rather than fabricating a terminal rejection.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.support.order_fakes import (
    FIXED_NOW,
    FakeBroker,
    option_contract,
)

from chronos.broker.base import BrokerError, BrokerRefusedBeforeSend, BrokerSendGuard
from chronos.broker.connection import BrokerConnectionManager
from chronos.config.settings import Settings
from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    DisplayEnvironment,
    IBEnvironment,
    OrderIntent,
    OrderLifecycle,
    ProductFamily,
    ReconciliationStatus,
)
from chronos.domain.models import (
    AccountSummary,
    BrokerOrder,
    ConnectionStatus,
    MarketQuote,
    OrderRequest,
    OrderSubmission,
)
from chronos.orders.arming import REQUIRED_ARM_PHRASE, LiveArmingService
from chronos.orders.intent import WheelOrderIntent, build_option_intent
from chronos.orders.kill_switch import LiveKillSwitch
from chronos.orders.mutations import (
    OrderCancellationService,
    OrderModificationService,
    OrderMutationError,
)
from chronos.orders.preview import OrderPreviewService
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.reconciliation_recovery import OrderRestartReconciler
from chronos.orders.risk import OrderRiskEngine, RiskEvidence
from chronos.orders.service import OrderManagementService
from chronos.orders.session_drawdown import SessionDrawdownBreaker
from chronos.orders.submission import OrderSubmissionBoundary, SubmissionRefusalCode
from chronos.orders.tracker import OrderTracker
from chronos.persistence.database import Database
from chronos.persistence.order_repositories import (
    OrderConfirmationRepository,
    OrderIntentRepository,
    OrderTrackerRepository,
    RiskDecisionRepository,
)
from chronos.services.trading_hours import session_for

LIVE_ACCOUNT = "U7654321"


def live_settings(**overrides: object) -> Settings:
    """Settings satisfying the full ADR-0009 live conjunction (fake account)."""

    base: dict[str, object] = {
        "_env_file": None,
        "broker_mode": "ibkr",
        "broker_adapter": "official_ibkr",
        "ib_environment": "live",
        "ib_port": 7496,
        "allow_order_transmit": True,
        "allow_live_trading": True,
        "ib_account_id": LIVE_ACCOUNT,
        "ib_account_allowlist": (LIVE_ACCOUNT,),
        "symbol_allowlist": ("AAPL", "MSFT", "SPY"),
        "max_contracts_per_order": 2,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class LiveSpyBroker(FakeBroker):
    """FakeBroker reporting live-gateway evidence; behavior injectable."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(account_id=LIVE_ACCOUNT, **kwargs)  # type: ignore[arg-type]
        self.managed_accounts: tuple[str, ...] = (LIVE_ACCOUNT,)
        self.nlv = Decimal("100000")
        self.connection_status_error: BrokerError | None = None
        self.account_summary_error: BrokerError | None = None
        # submit_error is initialized by the FakeBroker base (M8b).

    async def connection_status(self) -> ConnectionStatus:
        if self.connection_status_error is not None:
            raise self.connection_status_error
        return ConnectionStatus(
            state=ConnectionState.CONNECTED,
            environment=DisplayEnvironment.LIVE,
            connected=True,
            account_id=self.account_id,
            managed_accounts=self.managed_accounts,
        )

    async def account_summary(self) -> AccountSummary:
        if self.account_summary_error is not None:
            raise self.account_summary_error
        return AccountSummary(
            account_id=self.account_id,
            net_liquidation=self.nlv,
            total_cash=Decimal("80000"),
            buying_power=Decimal("160000"),
            as_of=FIXED_NOW,
        )

    async def submit_order(
        self,
        request: OrderRequest,
        *,
        send_guard: BrokerSendGuard | None = None,
    ) -> OrderSubmission:
        if self.submit_error is not None:
            raise self.submit_error
        return await super().submit_order(request, send_guard=send_guard)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _CannedEvidence:
    def __init__(self, readiness: ReconciliationReadiness) -> None:
        self._readiness = readiness

    def gather(self, intent: object, *, now: datetime) -> RiskEvidence:
        del intent
        readiness = self._readiness.snapshot()
        return RiskEvidence(
            account=AccountSummary(
                account_id=LIVE_ACCOUNT,
                net_liquidation=Decimal("100000"),
                total_cash=Decimal("80000"),
                buying_power=Decimal("160000"),
                as_of=FIXED_NOW,
            ),
            reconciliation_status=ReconciliationStatus.RECONCILED,
            reconciliation_generation=readiness.generation,
            reconciliation_session_id=readiness.session_id,
            session=session_for(ProductFamily.OPTION, now=now, broker_confirms_open=True),
            wheel_eligible_action=OrderIntent.OPEN_SHORT_PUT,
        )


class _LiveHarness:
    def __init__(self, broker: LiveSpyBroker, settings: Settings, tmp_path: Path) -> None:
        self.broker = broker
        self.settings = settings
        self.clock = _Clock(FIXED_NOW)
        self.db = Database("sqlite:///:memory:")
        self.db.initialize()
        self.db.bind_scope(broker_mode="ibkr", environment="live", account_id=LIVE_ACCOUNT)
        self.readiness = ReconciliationReadiness()
        self.connection = BrokerConnectionManager(broker, readiness=self.readiness)
        self.connection.start()
        self.mark_reconciled()
        intents = OrderIntentRepository(self.db.sessions)
        tracker_repo = OrderTrackerRepository(self.db.sessions)
        confirmations = OrderConfirmationRepository(self.db.sessions)
        risk_decisions = RiskDecisionRepository(self.db.sessions)
        tracker = OrderTracker(intents, tracker_repo)
        self.intents = intents
        self.tracker = tracker
        self.tracker_repo = tracker_repo
        self.kill_switch = LiveKillSwitch(tmp_path / "ks.json")
        self.arming = LiveArmingService(ttl_minutes=15)
        self.drawdown = SessionDrawdownBreaker(
            tmp_path / "baseline.json",
            max_drawdown_usd=Decimal("1000"),
            max_drawdown_pct=Decimal("0.02"),
            market_timezone="America/New_York",
            kill_switch=self.kill_switch,
        )
        self.quote: MarketQuote | None = None  # set per test; default fresh in submit helpers
        self.quote_reader_error: Exception | None = None
        self.boundary = OrderSubmissionBoundary(
            settings=settings,
            connection=self.connection,
            intents=intents,
            confirmations=confirmations,
            tracker=tracker_repo,
            live_arming=self.arming,
            live_kill_switch=self.kill_switch,
            session_drawdown=self.drawdown,
            quote_reader=self._read_quote,
            clock=self.clock,
            reconciliation_readiness=self.readiness,
        )
        self.service = OrderManagementService(
            settings=settings,
            environment=IBEnvironment.LIVE,
            account_id=LIVE_ACCOUNT,
            evidence_provider=_CannedEvidence(self.readiness),
            risk_engine=OrderRiskEngine(settings),
            preview_service=OrderPreviewService(self.connection),
            submission_boundary=self.boundary,
            modification=OrderModificationService(
                connection=self.connection,
                intents=intents,
                tracker=tracker,
                tracker_repo=tracker_repo,
                live_environment=True,
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
            broker_environment_is_paper=False,
        )

    def _read_quote(self, intent: WheelOrderIntent) -> MarketQuote | None:
        if self.quote_reader_error is not None:
            raise self.quote_reader_error
        if self.quote is not None:
            return self.quote
        return MarketQuote(
            contract=intent.contract,
            timestamp=self.clock.now,
            data_quality=DataQuality.LIVE,
            bid=Decimal("1.15"),
            ask=Decimal("1.25"),
        )

    def mark_reconciled(self) -> None:
        assert self.readiness.complete(
            expected_generation=self.readiness.snapshot().generation,
            status=ReconciliationStatus.RECONCILED,
            reason="test harness broker/local parity",
            reconciled_at=FIXED_NOW,
        )

    def arm(self) -> None:
        self.arming.arm(REQUIRED_ARM_PHRASE, now=self.clock.now)

    def close(self) -> None:
        self.connection.close()
        self.db.dispose()


@pytest.fixture
def live(tmp_path: Path) -> Iterator[_LiveHarness]:
    h = _LiveHarness(LiveSpyBroker(), live_settings(), tmp_path)
    try:
        yield h
    finally:
        h.close()


def _live_intent(
    intent_id: str = "live-1",
    correlation: str = "CHR-ORD-" + "B" * 32,
    limit_price: Decimal = Decimal("1.20"),
) -> WheelOrderIntent:
    return build_option_intent(
        account_id=LIVE_ACCOUNT,
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=option_contract(),
        quantity=1,
        limit_price=limit_price,
        correlation_id=correlation,
        intent_id=intent_id,
    )


def _confirmed(h: _LiveHarness, intent: WheelOrderIntent) -> None:
    proposal = h.service.propose(intent, now=h.clock.now)
    assert proposal.risk.approved
    h.service.preview(intent, now=h.clock.now)
    h.service.confirm(intent, risk_decision_id=proposal.risk.decision_id, now=h.clock.now)


def _submit(h: _LiveHarness, intent: WheelOrderIntent) -> object:
    return h.service.submit(intent, writer_lease_held=True, now=h.clock.now)


# --- the one happy path ----------------------------------------------------


def test_live_happy_path_emits_exactly_one_correct_order(live: _LiveHarness) -> None:
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    outcome = _submit(live, intent)
    assert outcome.submitted is True  # type: ignore[attr-defined]
    assert len(live.broker.submit_calls) == 1
    request = live.broker.submit_calls[0]
    # The correct live order object, and nothing reached any venue (the spy IS
    # the venue): LMT-only by type, tick-valid price, RTH-only, verified acct.
    assert request.transmit is True
    assert request.account_id == LIVE_ACCOUNT
    assert request.outside_rth is False
    assert request.limit_price == Decimal("1.20")
    assert request.limit_price % Decimal("0.01") == 0
    assert request.contract.con_id == 111
    stored = live.service.get("live-1")
    assert stored is not None and stored.status is OrderLifecycle.SUBMITTED


def test_definitive_venue_reject_is_not_persisted_as_submitted(tmp_path: Path) -> None:
    class _RejectingLiveSpyBroker(LiveSpyBroker):
        async def submit_order(
            self,
            request: OrderRequest,
            *,
            send_guard: BrokerSendGuard | None = None,
        ) -> OrderSubmission:
            submission = await super().submit_order(request, send_guard=send_guard)
            return submission.model_copy(update={"lifecycle": OrderLifecycle.REJECTED})

    h = _LiveHarness(_RejectingLiveSpyBroker(), live_settings(), tmp_path)
    try:
        h.arm()
        intent = _live_intent()
        _confirmed(h, intent)
        outcome = _submit(h, intent)
        assert outcome.submitted is False  # type: ignore[attr-defined]
        assert outcome.refusal is SubmissionRefusalCode.BROKER_SUBMIT_FAILED  # type: ignore[attr-defined]
        assert len(h.broker.submit_calls) == 1
        stored = h.service.get("live-1")
        assert stored is not None and stored.status is OrderLifecycle.REJECTED
    finally:
        h.close()


# --- adversarial walk: every case leaves submit_calls == 0 ----------------


def _assert_refused(live: _LiveHarness, outcome: object, code: SubmissionRefusalCode) -> None:
    assert outcome.submitted is False  # type: ignore[attr-defined]
    assert outcome.refusal is code  # type: ignore[attr-defined]
    assert live.broker.submit_calls == []


def test_missing_live_dependencies_fail_loud_at_construction(tmp_path: Path) -> None:
    # M7 review finding F6: a live-environment boundary missing its safety
    # machinery must never exist — construction fails loudly, not per-submit.
    h = _LiveHarness(LiveSpyBroker(), live_settings(), tmp_path)
    try:
        with pytest.raises(RuntimeError, match="LIVE-environment submission boundary"):
            OrderSubmissionBoundary(
                settings=h.settings,
                connection=h.connection,
                intents=h.intents,
                confirmations=OrderConfirmationRepository(h.db.sessions),
                tracker=h.tracker_repo,
            )
        assert h.broker.submit_calls == []
    finally:
        h.close()


def test_tampered_settings_cannot_pass_the_config_gate(live: _LiveHarness) -> None:
    # model_copy(update=...) bypasses validators; the property re-derives.
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    tampered = live.settings.model_copy(update={"ib_account_allowlist": ()})
    boundary = OrderSubmissionBoundary(
        settings=tampered,
        connection=live.connection,
        intents=live.intents,
        confirmations=OrderConfirmationRepository(live.db.sessions),
        tracker=live.tracker_repo,
        live_arming=live.arming,
        live_kill_switch=live.kill_switch,
        session_drawdown=live.drawdown,
        quote_reader=live._read_quote,
        clock=live.clock,
        reconciliation_readiness=live.readiness,
    )
    outcome = boundary.submit(
        intent=intent,
        risk_decision=live.service.propose(
            _live_intent("live-2", "CHR-ORD-" + "E" * 32, Decimal("1.25")),
            now=live.clock.now,
        ).risk,
        connected_account_id=LIVE_ACCOUNT,
        broker_environment_is_paper=False,
        writer_lease_held=True,
        now=live.clock.now,
    )
    _assert_refused(live, outcome, SubmissionRefusalCode.TRANSMISSION_NOT_POSSIBLE)


def test_paper_gateway_with_live_settings_is_grant_denied(live: _LiveHarness) -> None:
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    # The gateway observes PAPER accounts: settings say live, evidence says no.
    live.broker.managed_accounts = ("DU1234567",)
    outcome = _submit(live, intent)
    _assert_refused(live, outcome, SubmissionRefusalCode.LIVE_GRANT_DENIED)


def test_unobserved_environment_is_grant_denied(live: _LiveHarness) -> None:
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    live.broker.managed_accounts = ()
    outcome = _submit(live, intent)
    _assert_refused(live, outcome, SubmissionRefusalCode.LIVE_GRANT_DENIED)


def test_connection_status_failure_is_grant_denied(live: _LiveHarness) -> None:
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    live.broker.connection_status_error = BrokerError("gateway unreachable")
    outcome = _submit(live, intent)
    _assert_refused(live, outcome, SubmissionRefusalCode.LIVE_GRANT_DENIED)


def test_not_armed_blocks(live: _LiveHarness) -> None:
    intent = _live_intent()
    _confirmed(live, intent)
    outcome = _submit(live, intent)
    _assert_refused(live, outcome, SubmissionRefusalCode.LIVE_GATE_BLOCKED)


def test_arm_expiry_blocks(live: _LiveHarness) -> None:
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    live.clock.now = FIXED_NOW + timedelta(minutes=16)
    outcome = _submit(live, intent)
    _assert_refused(live, outcome, SubmissionRefusalCode.LIVE_GATE_BLOCKED)


def test_confirmation_expiring_mid_walk_blocks(live: _LiveHarness) -> None:
    # Confirmed at FIXED_NOW (20s TTL); the fresh post-I/O clock is past it.
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    live.clock.now = FIXED_NOW + timedelta(seconds=21)
    outcome = _submit(live, intent)
    _assert_refused(live, outcome, SubmissionRefusalCode.LIVE_GATE_BLOCKED)


def test_kill_switch_engaged_blocks(live: _LiveHarness) -> None:
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    live.kill_switch.engage(reason="operator halt", initiated_by="op", now=live.clock.now)
    outcome = _submit(live, intent)
    _assert_refused(live, outcome, SubmissionRefusalCode.LIVE_GATE_BLOCKED)


class _FlippingKillSwitch:
    """Disengaged during the gate walk; ENGAGED at the final pre-transmit read."""

    def __init__(self) -> None:
        self.reads = 0

    def is_engaged(self) -> bool:
        self.reads += 1
        return self.reads >= 2


def test_kill_switch_engaged_between_cas_and_transmit(
    tmp_path: Path,
) -> None:
    h = _LiveHarness(LiveSpyBroker(), live_settings(), tmp_path)
    try:
        flipping = _FlippingKillSwitch()
        h.boundary._live_kill_switch = flipping  # type: ignore[assignment]
        h.arm()
        intent = _live_intent()
        _confirmed(h, intent)
        outcome = _submit(h, intent)
        _assert_refused(h, outcome, SubmissionRefusalCode.LIVE_GATE_BLOCKED)
        assert flipping.reads >= 2
        # Provably nothing was sent, so the intent resolved to REJECTED — the
        # reconciliation gate is NOT wedged for future orders.
        stored = h.service.get("live-1")
        assert stored is not None and stored.status is OrderLifecycle.REJECTED
    finally:
        h.close()


def test_drawdown_breach_blocks_and_engages_kill_switch(live: _LiveHarness) -> None:
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    live.drawdown.check(Decimal("100000"), now=live.clock.now)  # establish baseline
    live.broker.nlv = Decimal("98000")  # -2000 breaches both limits
    outcome = _submit(live, intent)
    _assert_refused(live, outcome, SubmissionRefusalCode.LIVE_GATE_BLOCKED)
    assert live.kill_switch.is_engaged()


def test_unreadable_nlv_blocks_without_engaging_kill_switch(live: _LiveHarness) -> None:
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    live.broker.account_summary_error = BrokerError("account farm timeout")
    outcome = _submit(live, intent)
    _assert_refused(live, outcome, SubmissionRefusalCode.LIVE_GATE_BLOCKED)
    assert not live.kill_switch.is_engaged()  # cannot-evaluate != breached


def test_stale_quote_blocks(live: _LiveHarness) -> None:
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    live.quote = MarketQuote(
        contract=intent.contract,
        timestamp=FIXED_NOW - timedelta(seconds=30),
        data_quality=DataQuality.LIVE,
        bid=Decimal("1.15"),
        ask=Decimal("1.25"),
    )
    outcome = _submit(live, intent)
    _assert_refused(live, outcome, SubmissionRefusalCode.LIVE_GATE_BLOCKED)


def test_frozen_quote_quality_blocks(live: _LiveHarness) -> None:
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    live.quote = MarketQuote(
        contract=intent.contract,
        timestamp=live.clock.now,
        data_quality=DataQuality.FROZEN,
        bid=Decimal("1.15"),
        ask=Decimal("1.25"),
    )
    outcome = _submit(live, intent)
    _assert_refused(live, outcome, SubmissionRefusalCode.LIVE_GATE_BLOCKED)


def test_quote_reader_failure_blocks(live: _LiveHarness) -> None:
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    live.quote_reader_error = RuntimeError("pacing violation")
    outcome = _submit(live, intent)
    _assert_refused(live, outcome, SubmissionRefusalCode.LIVE_GATE_BLOCKED)


def test_tick_invalid_limit_price_blocks(live: _LiveHarness) -> None:
    live.arm()
    intent = _live_intent(limit_price=Decimal("1.205"))  # min_tick is 0.01
    _confirmed(live, intent)
    outcome = _submit(live, intent)
    _assert_refused(live, outcome, SubmissionRefusalCode.LIVE_GATE_BLOCKED)


def test_connection_uncertainty_blocks_before_stuck_intent_scan(live: _LiveHarness) -> None:
    live.arm()
    # First order strands in SUBMISSION_UNKNOWN (ambiguous after-send failure).
    stuck = _live_intent("live-stuck", "CHR-ORD-" + "C" * 32)
    _confirmed(live, stuck)
    live.broker.submit_error = BrokerError("socket dropped mid-send")
    outcome = _submit(live, stuck)
    assert outcome.refusal is SubmissionRefusalCode.BROKER_SUBMIT_FAILED  # type: ignore[attr-defined]
    live.broker.submit_error = None
    # A second, fully-valid order is now blocked by the reconciliation gate.
    intent = _live_intent("live-2", "CHR-ORD-" + "D" * 32, Decimal("1.25"))
    _confirmed(live, intent)
    outcome = _submit(live, intent)
    assert outcome.submitted is False  # type: ignore[attr-defined]
    assert outcome.refusal is SubmissionRefusalCode.RECONCILIATION_NOT_READY  # type: ignore[attr-defined]
    assert "PENDING" in outcome.detail  # type: ignore[attr-defined]
    assert live.broker.submit_calls == []


def test_operator_absence_remains_unknown_and_locked(
    live: _LiveHarness,
) -> None:
    live.arm()
    stuck = _live_intent("live-stuck", "CHR-ORD-" + "C" * 32)
    _confirmed(live, stuck)
    live.broker.submit_error = BrokerError("socket dropped mid-send")
    _submit(live, stuck)
    live.broker.submit_error = None
    # Snapshot absence cannot prove that no order reached the venue.
    with pytest.raises(ValueError, match="cannot safely prove rejection"):
        live.service.resolve_submission_unknown(
            "live-stuck",
            operator_note="verified absent in TWS orders + trade log",
            now=FIXED_NOW,
        )
    stored = live.service.get("live-stuck")
    assert stored is not None and stored.status is OrderLifecycle.SUBMISSION_UNKNOWN
    assert live.broker.submit_calls == []


def test_operator_resolution_refuses_when_broker_knows_the_order(
    live: _LiveHarness,
) -> None:
    live.arm()
    correlation = "CHR-ORD-" + "C" * 32
    stuck = _live_intent("live-stuck", correlation)
    _confirmed(live, stuck)
    live.broker.submit_error = BrokerError("timeout after send")
    _submit(live, stuck)
    live.broker.submit_error = None
    # The broker DOES know the order: truth is applied, rejection refused.
    live.broker._open_orders = (
        BrokerOrder(
            broker_order_id=9001,
            client_id=17,
            account_id=LIVE_ACCOUNT,
            order_ref=correlation,
            contract=option_contract(),
            side=stuck.side,
            quantity=Decimal("1"),
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("1"),
            limit_price=Decimal("1.20"),
            lifecycle=OrderLifecycle.SUBMITTED,
        ),
    )
    resolved = live.service.resolve_submission_unknown(
        "live-stuck", operator_note="attempting manual rejection", now=FIXED_NOW
    )
    assert resolved is False
    stored = live.service.get("live-stuck")
    assert stored is not None and stored.status is OrderLifecycle.SUBMITTED


def test_refused_before_send_resolves_to_rejected_without_wedging(
    live: _LiveHarness,
) -> None:
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    live.broker.submit_error = BrokerRefusedBeforeSend("adapter re-validation refused")
    outcome = _submit(live, intent)
    assert outcome.refusal is SubmissionRefusalCode.BROKER_REFUSED_BEFORE_SEND  # type: ignore[attr-defined]
    stored = live.service.get("live-1")
    assert stored is not None and stored.status is OrderLifecycle.REJECTED
    live.broker.submit_error = None
    live.mark_reconciled()
    follow_up = _live_intent("live-2", "CHR-ORD-" + "D" * 32, Decimal("1.25"))
    _confirmed(live, follow_up)
    outcome = _submit(live, follow_up)
    assert outcome.submitted is True  # type: ignore[attr-defined]


def test_true_cas_refuses_after_status_flip(live: _LiveHarness) -> None:
    # The repository CAS primitive the boundary relies on (ADR-0009 §4 step 9).
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    live.intents.set_status(
        "live-1",
        OrderLifecycle.REJECTED,
        current_account_id=LIVE_ACCOUNT,
        at=live.clock.now,
    )
    won = live.tracker_repo.record_transition(
        intent_id="live-1",
        event_key="live-1:presubmit:SUBMISSION_UNKNOWN",
        source="SUBMIT",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=OrderLifecycle.SUBMISSION_UNKNOWN,
        current_account_id=LIVE_ACCOUNT,
        occurred_at=live.clock.now,
        enforce_from_status=True,
    )
    assert won is False
    stored = live.service.get("live-1")
    assert stored is not None and stored.status is OrderLifecycle.REJECTED  # not clobbered


def test_second_submit_never_transmits_twice(live: _LiveHarness) -> None:
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    first = _submit(live, intent)
    assert first.submitted is True  # type: ignore[attr-defined]
    second = _submit(live, intent)
    assert second.submitted is False  # type: ignore[attr-defined]
    assert len(live.broker.submit_calls) == 1


def test_live_modify_is_refused(live: _LiveHarness) -> None:
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    assert _submit(live, intent).submitted is True  # type: ignore[attr-defined]
    with pytest.raises(OrderMutationError, match="live order modification is refused"):
        live.service.modify("live-1", Decimal("1.10"))
    assert live.broker.modify_calls == []


def test_cancel_works_with_kill_switch_engaged(live: _LiveHarness) -> None:
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    assert _submit(live, intent).submitted is True  # type: ignore[attr-defined]
    live.kill_switch.engage(reason="emergency stop", initiated_by="op", now=live.clock.now)
    result = live.service.cancel("live-1")
    assert result.requested is True
    assert live.broker.cancel_calls == [9001]  # risk-reducing action still works


# --- review-remediation regression tests (M7 adversarial findings) ---------


def test_operator_resolution_refuses_stale_session_window(live: _LiveHarness) -> None:
    # F2 (the dangerous direction): executions cover the CURRENT session only,
    # so a prior-session SUBMISSION_UNKNOWN must never resolve on "absence".
    live.arm()
    stuck = _live_intent("live-stuck", "CHR-ORD-" + "C" * 32)
    _confirmed(live, stuck)
    live.broker.submit_error = BrokerError("socket dropped mid-send")
    _submit(live, stuck)
    live.broker.submit_error = None
    next_day = FIXED_NOW + timedelta(days=3)  # Friday submit, Monday resolve
    with pytest.raises(ValueError, match="broker absence is not provable"):
        live.service.resolve_submission_unknown(
            "live-stuck", operator_note="looks absent to me", now=next_day
        )
    stored = live.service.get("live-stuck")
    assert stored is not None and stored.status is OrderLifecycle.SUBMISSION_UNKNOWN


def test_non_positive_nlv_never_establishes_a_baseline(live: _LiveHarness) -> None:
    # F3: an implausible first NLV read refuses without a baseline write and
    # without the durable kill-switch side effect.
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    live.broker.nlv = Decimal("0")
    outcome = _submit(live, intent)
    _assert_refused(live, outcome, SubmissionRefusalCode.LIVE_GATE_BLOCKED)
    assert not live.kill_switch.is_engaged()
    # A later, sane NLV establishes normally and the walk proceeds.
    live.broker.nlv = Decimal("100000")
    outcome = _submit(live, intent)
    assert outcome.submitted is True  # type: ignore[attr-defined]


def test_declined_preview_never_advances_or_confirms(live: _LiveHarness) -> None:
    # F5: a broker-declined what-if leaves VALIDATED, records no preview_id,
    # and blocks confirmation — the live preview gate can never see fake True.
    live.arm()
    intent = _live_intent()
    proposal = live.service.propose(intent, now=live.clock.now)
    live.broker._preview_accepted = False
    result = live.service.preview(intent, now=live.clock.now)
    assert result.accepted is False
    stored = live.service.get("live-1")
    assert stored is not None
    assert stored.status is OrderLifecycle.VALIDATED
    assert stored.preview_id is None
    from chronos.orders.service import OrderPipelineError

    with pytest.raises(OrderPipelineError, match="previewed before confirmation"):
        live.service.confirm(intent, risk_decision_id=proposal.risk.decision_id, now=live.clock.now)
    assert live.broker.submit_calls == []


def test_confirmation_fresh_at_entry_but_expired_on_fresh_clock_blocks(
    live: _LiveHarness,
) -> None:
    # F7: distinguishes the fresh-clock discipline — the caller's `now` says
    # the confirmation is still valid; only the boundary's own clock knows
    # it expired mid-walk.
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    live.clock.now = FIXED_NOW + timedelta(seconds=21)
    outcome = live.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)
    _assert_refused(live, outcome, SubmissionRefusalCode.LIVE_GATE_BLOCKED)


def test_live_account_mismatch_refuses(tmp_path: Path) -> None:
    # F8: the broker-observed live account differs from the intent's account.
    # (A foreign-account intent cannot even be proposed — the DB scope guard
    # refuses it — so the mismatch is staged by flipping the OBSERVED account
    # to a second allowlisted live account after confirmation.)
    settings = live_settings(ib_account_allowlist=(LIVE_ACCOUNT, "U9999999"))
    h = _LiveHarness(LiveSpyBroker(), settings, tmp_path)
    try:
        h.arm()
        intent = _live_intent()
        _confirmed(h, intent)
        h.broker.account_id = "U9999999"
        h.broker.managed_accounts = ("U9999999",)
        outcome = _submit(h, intent)
        _assert_refused(h, outcome, SubmissionRefusalCode.ACCOUNT_MISMATCH)
    finally:
        h.close()


def test_wrong_lifecycle_refuses_with_intent_not_confirmed(live: _LiveHarness) -> None:
    # F8: the exact refusal code is asserted on the live branch.
    live.arm()
    intent = _live_intent()
    proposal = live.service.propose(intent, now=live.clock.now)
    del proposal
    outcome = _submit(live, intent)  # still VALIDATED — never confirmed
    _assert_refused(live, outcome, SubmissionRefusalCode.INTENT_NOT_CONFIRMED)


def test_corrupt_baseline_through_the_boundary_blocks_and_latches(
    live: _LiveHarness, tmp_path: Path
) -> None:
    # F8: corrupt in-session baseline, exercised through the boundary walk.
    live.arm()
    intent = _live_intent()
    _confirmed(live, intent)
    live.drawdown.check(Decimal("100000"), now=live.clock.now)  # establish
    (tmp_path / "baseline.json").write_text("{ not valid json", encoding="utf-8")
    outcome = _submit(live, intent)
    _assert_refused(live, outcome, SubmissionRefusalCode.LIVE_GATE_BLOCKED)
    assert live.kill_switch.is_engaged()  # real corruption DOES latch


def test_runtime_wiring_shares_live_instances() -> None:
    # F6: the boundary must hold the SAME live-safety instances as AppRuntime
    # (arming is process-memory; a copy would ignore /live/arm forever).
    import chronos.runtime as runtime_module

    source = Path(runtime_module.__file__).read_text(encoding="utf-8")
    assert "live_arming=live_arming" in source
    assert "live_kill_switch=live_kill_switch" in source
    assert "session_drawdown=session_drawdown" in source


def test_operator_resolution_refuses_changing_broker_snapshot(
    live: _LiveHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live.arm()
    correlation = "CHR-ORD-" + "C" * 32
    stuck = _live_intent("live-stuck", correlation)
    _confirmed(live, stuck)
    live.broker.submit_error = BrokerError("timeout after send")
    _submit(live, stuck)
    live.broker.submit_error = None
    working = BrokerOrder(
        broker_order_id=9001,
        client_id=17,
        account_id=LIVE_ACCOUNT,
        order_ref=correlation,
        contract=option_contract(),
        side=stuck.side,
        quantity=Decimal("1"),
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal("1"),
        limit_price=Decimal("1.20"),
        lifecycle=OrderLifecycle.SUBMITTED,
    )
    observations = iter(((), (working,)))

    async def changing_open_orders() -> tuple[BrokerOrder, ...]:
        return next(observations)

    monkeypatch.setattr(live.broker, "open_orders", changing_open_orders)

    with pytest.raises(ValueError, match="changed across the bounded recovery observation"):
        live.service.resolve_submission_unknown(
            "live-stuck",
            operator_note="attempted against a changing snapshot",
            now=FIXED_NOW,
        )

    stored = live.service.get("live-stuck")
    assert stored is not None and stored.status is OrderLifecycle.SUBMISSION_UNKNOWN
