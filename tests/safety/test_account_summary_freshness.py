"""Account-summary freshness must fail closed before risk evidence is built."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.account_freshness import (
    AccountSummaryFreshnessError,
    AccountSummaryFreshnessReason,
    account_summary_check_time,
    require_account_summary_freshness,
)
from chronos.domain.enums import AccountSummaryProvenance, BrokerMode, OrderIntent
from chronos.domain.models import AccountSummary
from chronos.orders.evidence import BrokerRiskEvidenceProvider
from chronos.orders.intent import build_option_intent
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.risk import OrderRiskEngine
from chronos.orders.submission import OrderSubmissionBoundary
from tests.support.order_fakes import PAPER_ACCOUNT, FakeBroker, option_contract, paper_settings
from tests.support.persistence_keys import TEST_PERSISTENCE_KEY

DEMO_NOW = datetime(2036, 5, 4, 15, 0, tzinfo=UTC)


class _SummaryBroker(FakeBroker):
    def __init__(self, summary: AccountSummary) -> None:
        super().__init__(account_id=summary.account_id)
        self.summary = summary

    async def account_summary(self) -> AccountSummary:
        return self.summary


def _account_summary(as_of: datetime | None) -> AccountSummary:
    if as_of is None:
        # Model adapters promise AwareDatetime. This constructs the malformed
        # state a broken/custom adapter could still return at runtime.
        return AccountSummary.model_construct(
            provenance=AccountSummaryProvenance.VERIFIED_RESPONSE,
            account_id=PAPER_ACCOUNT,
            net_liquidation=Decimal("100000"),
            total_cash=Decimal("80000"),
            buying_power=Decimal("160000"),
            as_of=None,
        )
    return AccountSummary(
        provenance=AccountSummaryProvenance.VERIFIED_RESPONSE,
        account_id=PAPER_ACCOUNT,
        net_liquidation=Decimal("100000"),
        total_cash=Decimal("80000"),
        buying_power=Decimal("160000"),
        as_of=as_of,
    )


def _gather(
    summary: AccountSummary,
    *,
    now: datetime,
    clock: Callable[[], datetime] | None = None,
    broker_mode: str = "demo",
):
    broker = _SummaryBroker(summary)
    readiness = ReconciliationReadiness()
    connection = BrokerConnectionManager(broker, readiness=readiness)
    connection.start()
    settings = paper_settings(broker_mode=broker_mode)
    provider = BrokerRiskEvidenceProvider(
        connection=connection,
        settings=settings,
        reconciliation_readiness=readiness,
        clock=clock,
    )
    intent = build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=option_contract(),
        quantity=1,
        limit_price=Decimal("1.20"),
    )
    try:
        evidence = provider.gather(intent, now=now)
        return settings, intent, evidence
    finally:
        connection.close()


def _gather_with_direct_runner(
    summary: AccountSummary,
    *,
    clock: Callable[[], datetime],
):
    broker = _SummaryBroker(summary)
    readiness = ReconciliationReadiness()
    settings = paper_settings()
    provider = BrokerRiskEvidenceProvider(
        connection=SimpleNamespace(
            broker=broker,
            run=lambda awaitable: asyncio.run(awaitable),
        ),
        settings=settings,
        reconciliation_readiness=readiness,
        clock=clock,
    )
    intent = build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=option_contract(),
        quantity=1,
        limit_price=Decimal("1.20"),
    )
    return provider.gather(intent, now=DEMO_NOW)


@pytest.mark.parametrize(
    ("as_of", "reason"),
    [
        (DEMO_NOW - timedelta(seconds=31), "STALE"),
        (DEMO_NOW + timedelta(microseconds=1), "FUTURE_DATED"),
        (None, "MISSING_TIMESTAMP"),
    ],
)
def test_invalid_account_summary_refuses_before_risk_evidence(
    as_of: datetime | None,
    reason: str,
) -> None:
    with pytest.raises(ValueError) as refusal:
        _gather(_account_summary(as_of), now=DEMO_NOW)

    assert type(refusal.value).__name__ == "AccountSummaryFreshnessError"
    actual_reason = getattr(refusal.value, "reason", None)
    assert getattr(actual_reason, "value", actual_reason) == reason


def test_account_summary_exactly_at_configured_age_boundary_reaches_risk_engine() -> None:
    settings, intent, evidence = _gather(
        _account_summary(DEMO_NOW - timedelta(seconds=30)),
        now=DEMO_NOW,
    )

    decision = OrderRiskEngine(settings, persistence_key=TEST_PERSISTENCE_KEY).evaluate(
        intent,
        evidence,
        now=DEMO_NOW,
    )
    assert decision.decided_at == DEMO_NOW
    assert evidence.account.as_of == DEMO_NOW - timedelta(seconds=30)


def test_demo_account_summary_uses_the_demo_clock_for_freshness() -> None:
    settings, intent, evidence = _gather(
        _account_summary(DEMO_NOW),
        now=DEMO_NOW + timedelta(hours=1),
        clock=lambda: DEMO_NOW,
    )

    decision = OrderRiskEngine(settings, persistence_key=TEST_PERSISTENCE_KEY).evaluate(
        intent,
        evidence,
        now=DEMO_NOW,
    )
    assert decision.decided_at == DEMO_NOW
    assert evidence.account.as_of == DEMO_NOW


def test_risk_evidence_accepts_a_twenty_nine_second_summary_without_delay() -> None:
    evidence = _gather_with_direct_runner(
        _account_summary(DEMO_NOW),
        clock=lambda: DEMO_NOW + timedelta(seconds=29),
    )

    assert evidence.account.as_of == DEMO_NOW


def test_risk_evidence_rechecks_after_the_blocking_broker_reads() -> None:
    checks = iter(
        (
            DEMO_NOW + timedelta(seconds=29),
            DEMO_NOW + timedelta(seconds=31),
        )
    )

    with pytest.raises(AccountSummaryFreshnessError) as refusal:
        _gather_with_direct_runner(
            _account_summary(DEMO_NOW),
            clock=lambda: next(checks),
        )

    assert refusal.value.reason is AccountSummaryFreshnessReason.STALE


def test_live_account_summary_clock_uses_wall_time_after_the_read() -> None:
    before = datetime.now(UTC)
    checked_at = account_summary_check_time(broker_mode=BrokerMode.IBKR, demo_now=DEMO_NOW)
    after = datetime.now(UTC)

    assert before <= checked_at <= after


def test_account_summary_age_setting_is_positive_and_capped_at_thirty_seconds() -> None:
    assert paper_settings().account_summary_max_age_seconds == 30
    for invalid_age in (0, 31):
        with pytest.raises(ValidationError):
            paper_settings(account_summary_max_age_seconds=invalid_age)


def test_account_summary_guard_hard_caps_non_settings_callers() -> None:
    with pytest.raises(AccountSummaryFreshnessError) as refusal:
        require_account_summary_freshness(
            DEMO_NOW,
            now=DEMO_NOW,
            max_age_seconds=31,
        )

    assert refusal.value.reason is AccountSummaryFreshnessReason.INVALID_MAX_AGE


def test_account_summary_guard_rejects_naive_timestamp_and_clock() -> None:
    with pytest.raises(AccountSummaryFreshnessError) as timestamp_refusal:
        require_account_summary_freshness(
            DEMO_NOW.replace(tzinfo=None),
            now=DEMO_NOW,
            max_age_seconds=30,
        )
    assert timestamp_refusal.value.reason is AccountSummaryFreshnessReason.INVALID_TIMESTAMP

    with pytest.raises(AccountSummaryFreshnessError) as clock_refusal:
        require_account_summary_freshness(
            DEMO_NOW,
            now=DEMO_NOW.replace(tzinfo=None),
            max_age_seconds=30,
        )
    assert clock_refusal.value.reason is AccountSummaryFreshnessReason.INVALID_CLOCK


def test_live_drawdown_refuses_stale_account_summary_without_breaker_side_effects() -> None:
    broker = _SummaryBroker(_account_summary(DEMO_NOW - timedelta(seconds=31)))

    class _Connection:
        def __init__(self) -> None:
            self.broker = broker

        def run(self, awaitable: object) -> object:
            return asyncio.run(awaitable)  # type: ignore[arg-type]

    class _Breaker:
        called = False

        def check(self, _nlv: Decimal, *, now: datetime) -> object:
            self.called = True
            return SimpleNamespace(breached=False, reason="")

    breaker = _Breaker()
    boundary = SimpleNamespace(
        _connection=_Connection(),
        _clock=lambda: DEMO_NOW,
        _settings=paper_settings(),
        _session_drawdown=breaker,
    )

    nlv, _as_of, _provenance, error = OrderSubmissionBoundary._read_net_liquidation(boundary)
    refused, reason = OrderSubmissionBoundary._drawdown_evidence(
        boundary,
        nlv=nlv,
        nlv_error=error,
        now=DEMO_NOW,
    )

    assert nlv is None
    assert "stale" in error
    assert refused
    assert "without engaging the kill switch" in reason
    assert not breaker.called


def test_live_drawdown_rechecks_account_age_at_the_post_io_clock() -> None:
    boundary = SimpleNamespace(_settings=paper_settings())

    accepted, accepted_error = OrderSubmissionBoundary._recheck_net_liquidation(
        boundary,
        nlv=Decimal("100000"),
        as_of=DEMO_NOW,
        provenance=AccountSummaryProvenance.VERIFIED_RESPONSE,
        nlv_error="",
        now=DEMO_NOW + timedelta(seconds=29),
    )
    refused, refused_error = OrderSubmissionBoundary._recheck_net_liquidation(
        boundary,
        nlv=Decimal("100000"),
        as_of=DEMO_NOW,
        provenance=AccountSummaryProvenance.VERIFIED_RESPONSE,
        nlv_error="",
        now=DEMO_NOW + timedelta(seconds=31),
    )

    assert accepted == Decimal("100000")
    assert accepted_error == ""
    assert refused is None
    assert "account summary freshness refused: stale" in refused_error
