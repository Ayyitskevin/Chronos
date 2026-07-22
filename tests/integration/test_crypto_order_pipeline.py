"""End-to-end crypto order pipeline against a recording fake broker (M7C).

The crypto counterpart of ``test_order_pipeline`` and the M7 live-submission
spy suite. It proves the two guarantees the crypto family must uphold once the
intent-validator refusal is lifted (ADR-0010 §7, final commit):

1. The happy path emits **exactly one** order and it carries the correct
   *fractional* ``Decimal`` quantity, the family TIF, and ``transmit=True`` at
   the single paper transmit site.
2. **Every** refusal path — below venue min-size, over the per-order notional
   cap, over the allocation cap, family disabled (empty allowlist), a SELL
   beyond holdings, an un-permitted TIF — leaves ``submit_calls == []``: no
   order ever reaches the broker.

The fake broker reaches NO venue, so "no order is placed by any test path"
holds structurally; the paper boundary is the same single transmit line the
option/stock families use.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal

import pytest
from tests.support.order_fakes import (
    FIXED_NOW,
    PAPER_ACCOUNT,
    FakeBroker,
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
from chronos.domain.models import AccountSummary, CryptoContract
from chronos.orders.intent import WheelOrderIntent, build_crypto_intent
from chronos.orders.mutations import OrderCancellationService, OrderModificationService
from chronos.orders.preview import OrderPreviewService
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.reconciliation_recovery import OrderRestartReconciler
from chronos.orders.risk import OrderRiskEngine, RiskEvidence
from chronos.orders.service import OrderManagementService
from chronos.orders.submission import OrderSubmissionBoundary
from chronos.orders.tracker import OrderTracker
from chronos.persistence.database import Database
from chronos.persistence.order_repositories import (
    OrderConfirmationRepository,
    OrderIntentRepository,
    OrderTrackerRepository,
    RiskDecisionRepository,
)
from chronos.services.trading_hours import session_for


def _crypto_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "crypto_allowlist": ("BTC", "ETH"),
        "max_crypto_notional_per_order_usd": Decimal("1000"),
        "max_crypto_allocation_pct": Decimal("0.10"),
        "min_cash_buffer_usd": Decimal("0"),
        "min_cash_buffer_pct": Decimal("0"),
    }
    base.update(overrides)
    return paper_settings(**base)


def _crypto_account() -> AccountSummary:
    return AccountSummary(
        account_id=PAPER_ACCOUNT,
        net_liquidation=Decimal("100000"),
        total_cash=Decimal("80000"),
        buying_power=Decimal("160000"),
        as_of=FIXED_NOW,
    )


class _CryptoEvidence:
    """Canned crypto risk evidence; every field is overridable per test."""

    def __init__(
        self,
        *,
        held_crypto: Decimal = Decimal("0"),
        sell_reserved: Decimal = Decimal("0"),
        allocation: Decimal = Decimal("0"),
        marked: bool = True,
        pending_buy: Decimal = Decimal("0"),
    ) -> None:
        self._held = held_crypto
        self._reserved = sell_reserved
        self._allocation = allocation
        self._marked = marked
        self._pending = pending_buy
        self._readiness: ReconciliationReadiness | None = None

    def bind_readiness(self, readiness: ReconciliationReadiness) -> None:
        self._readiness = readiness

    def gather(self, intent: object, *, now: datetime) -> RiskEvidence:
        del intent
        if self._readiness is None:
            raise RuntimeError("crypto test evidence is not bound to readiness")
        readiness = self._readiness.snapshot()
        return RiskEvidence(
            account=_crypto_account(),
            reconciliation_status=ReconciliationStatus.RECONCILED,
            reconciliation_generation=readiness.generation,
            reconciliation_session_id=readiness.session_id,
            session=session_for(ProductFamily.CRYPTO, now=now, broker_confirms_open=True),
            held_crypto_quantity=self._held,
            crypto_sell_reserved_quantity=self._reserved,
            current_crypto_allocation=self._allocation,
            crypto_allocation_marked=self._marked,
            pending_crypto_buy_notional=self._pending,
        )


class _CryptoHarness:
    def __init__(self, broker: FakeBroker, settings: Settings, evidence: _CryptoEvidence) -> None:
        self.broker = broker
        self.settings = settings
        self.db = Database("sqlite:///:memory:")
        self.db.initialize()
        self.db.bind_scope(broker_mode="ibkr", environment="paper", account_id=PAPER_ACCOUNT)
        self.readiness = ReconciliationReadiness()
        evidence.bind_readiness(self.readiness)
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
        risk_decisions = RiskDecisionRepository(self.db.sessions)
        tracker = OrderTracker(intents, tracker_repo)
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
            evidence_provider=evidence,
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


def _make_harness(evidence: _CryptoEvidence, **settings_overrides: object) -> _CryptoHarness:
    return _CryptoHarness(FakeBroker(), _crypto_settings(**settings_overrides), evidence)


def _crypto_contract(**overrides: object) -> CryptoContract:
    base: dict[str, object] = {
        "con_id": 479624278,
        "symbol": "BTC",
        "min_size": Decimal("0.0001"),
        "size_increment": Decimal("0.0001"),
    }
    base.update(overrides)
    return CryptoContract(**base)  # type: ignore[arg-type]


def _crypto_intent(
    *,
    intent: OrderIntent = OrderIntent.OPEN_LONG_CRYPTO,
    quantity: Decimal = Decimal("0.005"),
    limit_price: Decimal = Decimal("64000"),
    contract: CryptoContract | None = None,
    time_in_force: str = "DAY",
    correlation: str = "CHR-ORD-" + "C" * 32,
    intent_id: str = "crypto-intent-1",
) -> WheelOrderIntent:
    return build_crypto_intent(
        account_id=PAPER_ACCOUNT,
        intent=intent,
        contract=contract or _crypto_contract(),
        quantity=quantity,
        limit_price=limit_price,
        time_in_force=time_in_force,  # type: ignore[arg-type]
        correlation_id=correlation,
        intent_id=intent_id,
    )


def _drive_to_confirmed(h: _CryptoHarness, intent: WheelOrderIntent, now: datetime) -> None:
    proposal = h.service.propose(intent, now=now)
    if not proposal.risk.approved:
        return  # a refusal path never advances to preview/confirm
    h.service.preview(intent, now=now)
    h.service.confirm(intent, risk_decision_id=proposal.risk.decision_id, now=now)


@pytest.fixture
def happy_harness() -> Iterator[_CryptoHarness]:
    h = _make_harness(_CryptoEvidence())
    try:
        yield h
    finally:
        h.close()


def test_crypto_happy_path_transmits_exactly_one_fractional_order(
    happy_harness: _CryptoHarness,
) -> None:
    intent = _crypto_intent(quantity=Decimal("0.005"))
    _drive_to_confirmed(happy_harness, intent, FIXED_NOW)

    outcome = happy_harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)

    assert outcome.submitted is True
    # Exactly one order, carrying the fractional quantity untouched, the family
    # TIF, transmit=True, to the paper account.
    assert len(happy_harness.broker.submit_calls) == 1
    call = happy_harness.broker.submit_calls[0]
    assert call.quantity == Decimal("0.005")
    assert call.transmit is True
    assert call.time_in_force == "DAY"
    assert call.account_id == PAPER_ACCOUNT
    stored = happy_harness.service.get("crypto-intent-1")
    assert stored is not None and stored.status is OrderLifecycle.SUBMITTED


def test_ioc_happy_path_transmits_one_order_when_setting_permits() -> None:
    h = _make_harness(_CryptoEvidence(), crypto_time_in_force="IOC")
    try:
        intent = _crypto_intent(time_in_force="IOC")
        _drive_to_confirmed(h, intent, FIXED_NOW)
        outcome = h.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)
        assert outcome.submitted is True
        assert len(h.broker.submit_calls) == 1
        assert h.broker.submit_calls[0].time_in_force == "IOC"
    finally:
        h.close()


# --- every refusal path leaves submit_calls == [] -------------------------


def test_below_min_size_never_reaches_submit() -> None:
    h = _make_harness(_CryptoEvidence())
    try:
        intent = _crypto_intent(
            quantity=Decimal("0.001"), contract=_crypto_contract(min_size=Decimal("0.01"))
        )
        _drive_to_confirmed(h, intent, FIXED_NOW)
        outcome = h.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)
        assert outcome.submitted is False
        assert h.broker.submit_calls == []
    finally:
        h.close()


def test_over_notional_cap_never_reaches_submit() -> None:
    h = _make_harness(_CryptoEvidence())
    try:
        # 0.02 * 64000 = 1280 > 1000 cap.
        intent = _crypto_intent(quantity=Decimal("0.02"))
        _drive_to_confirmed(h, intent, FIXED_NOW)
        outcome = h.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)
        assert outcome.submitted is False
        assert h.broker.submit_calls == []
    finally:
        h.close()


def test_allocation_cap_breach_never_reaches_submit() -> None:
    # ceiling = 0.10 * 100000 = 10000; current 9990 + order 320 > ceiling.
    h = _make_harness(_CryptoEvidence(allocation=Decimal("9990")))
    try:
        intent = _crypto_intent(quantity=Decimal("0.005"))
        _drive_to_confirmed(h, intent, FIXED_NOW)
        outcome = h.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)
        assert outcome.submitted is False
        assert h.broker.submit_calls == []
    finally:
        h.close()


def test_unmarked_allocation_is_unknown_and_never_submits() -> None:
    h = _make_harness(_CryptoEvidence(marked=False))
    try:
        intent = _crypto_intent(quantity=Decimal("0.005"))
        _drive_to_confirmed(h, intent, FIXED_NOW)
        outcome = h.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)
        assert outcome.submitted is False
        assert h.broker.submit_calls == []
    finally:
        h.close()


def test_empty_allowlist_disables_family_and_never_submits() -> None:
    h = _make_harness(_CryptoEvidence(), crypto_allowlist=())
    try:
        intent = _crypto_intent(quantity=Decimal("0.005"))
        _drive_to_confirmed(h, intent, FIXED_NOW)
        outcome = h.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)
        assert outcome.submitted is False
        assert h.broker.submit_calls == []
    finally:
        h.close()


def test_sell_exceeding_holdings_never_submits() -> None:
    # held 0.2 minus reserved 0.0 => sellable 0.2; selling 0.5 fails (no short).
    h = _make_harness(_CryptoEvidence(held_crypto=Decimal("0.2")))
    try:
        intent = _crypto_intent(intent=OrderIntent.CLOSE_LONG_CRYPTO, quantity=Decimal("0.5"))
        _drive_to_confirmed(h, intent, FIXED_NOW)
        outcome = h.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)
        assert outcome.submitted is False
        assert h.broker.submit_calls == []
    finally:
        h.close()


def test_ioc_when_setting_is_day_never_submits() -> None:
    # Default crypto_time_in_force is DAY; an IOC intent fails the limit_only gate.
    h = _make_harness(_CryptoEvidence())
    try:
        intent = _crypto_intent(time_in_force="IOC")
        _drive_to_confirmed(h, intent, FIXED_NOW)
        outcome = h.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)
        assert outcome.submitted is False
        assert h.broker.submit_calls == []
    finally:
        h.close()


def test_read_only_lease_never_submits_crypto(happy_harness: _CryptoHarness) -> None:
    intent = _crypto_intent(quantity=Decimal("0.005"))
    _drive_to_confirmed(happy_harness, intent, FIXED_NOW)
    # A conforming order still never transmits without the single-writer lease.
    outcome = happy_harness.service.submit(intent, writer_lease_held=False, now=FIXED_NOW)
    assert outcome.submitted is False
    assert happy_harness.broker.submit_calls == []
