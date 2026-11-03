"""Account-summary freshness must fail closed before risk evidence is built."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.enums import OrderIntent
from chronos.domain.models import AccountSummary
from chronos.orders.evidence import BrokerRiskEvidenceProvider
from chronos.orders.intent import build_option_intent
from chronos.orders.risk import OrderRiskEngine
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
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
            account_id=PAPER_ACCOUNT,
            net_liquidation=Decimal("100000"),
            total_cash=Decimal("80000"),
            buying_power=Decimal("160000"),
            as_of=None,
        )
    return AccountSummary(
        account_id=PAPER_ACCOUNT,
        net_liquidation=Decimal("100000"),
        total_cash=Decimal("80000"),
        buying_power=Decimal("160000"),
        as_of=as_of,
    )


def _gather(summary: AccountSummary, *, now: datetime):
    broker = _SummaryBroker(summary)
    readiness = ReconciliationReadiness()
    connection = BrokerConnectionManager(broker, readiness=readiness)
    connection.start()
    settings = paper_settings(broker_mode="demo")
    provider = BrokerRiskEvidenceProvider(
        connection=connection,
        settings=settings,
        reconciliation_readiness=readiness,
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
    settings, intent, evidence = _gather(_account_summary(DEMO_NOW), now=DEMO_NOW)

    decision = OrderRiskEngine(settings, persistence_key=TEST_PERSISTENCE_KEY).evaluate(
        intent,
        evidence,
        now=DEMO_NOW,
    )
    assert decision.decided_at == DEMO_NOW
    assert evidence.account.as_of == DEMO_NOW
