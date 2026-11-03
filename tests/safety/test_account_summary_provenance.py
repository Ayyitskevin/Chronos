"""Unverified broker caches must stay displayable but cannot supply money evidence."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from ib_async import IB

from chronos.broker.ibkr import IBKRBroker
from chronos.domain.account_freshness import AccountSummaryFreshnessError
from chronos.domain.enums import OrderIntent, ReconciliationStatus
from chronos.orders.evidence import BrokerRiskEvidenceProvider
from chronos.orders.intent import build_stock_intent
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.risk import OrderRiskEngine
from tests.support.order_fakes import FIXED_NOW, PAPER_ACCOUNT, paper_settings, stock_contract
from tests.support.persistence_keys import TEST_PERSISTENCE_KEY
from tests.unit.test_ibkr_broker import FakeIB


class _CachedIB(FakeIB):
    accountSummaryAsync = IB.accountSummaryAsync

    def __init__(self) -> None:
        super().__init__()
        self.wrapper = SimpleNamespace(acctSummary=dict(enumerate(self.account_values)))
        self.summary_requests = 0

    async def reqAccountSummaryAsync(self) -> None:
        self.summary_requests += 1
        raise AssertionError("a cache read must not start option-B refresh traffic")


def test_cached_real_ib_async_summary_cannot_authorize_stock_cash_risk() -> None:
    moment = [FIXED_NOW]
    clock = lambda: moment[0]
    settings = paper_settings(
        broker_adapter="ib_async", allow_order_transmit=False, allow_live_trading=False
    )
    client = _CachedIB()
    broker = IBKRBroker(settings, client=client, clock=clock)
    asyncio.run(broker.connect())
    try:
        first = asyncio.run(broker.account_summary())
        moment[0] += timedelta(hours=1)
        displayed = asyncio.run(broker.account_summary())
        assert first.total_cash == displayed.total_cash
        assert displayed.as_of == moment[0]
        assert (displayed.as_of - first.as_of).total_seconds() == 3600
        assert client.summary_requests == 0
        readiness = ReconciliationReadiness(
            max_evidence_age=timedelta(seconds=300), clock=clock
        )
        assert readiness.complete(
            expected_generation=readiness.snapshot().generation,
            status=ReconciliationStatus.RECONCILED,
            reason="synthetic parity control",
            reconciled_at=clock(),
        )
        provider = BrokerRiskEvidenceProvider(
            connection=SimpleNamespace(broker=broker, run=asyncio.run),
            settings=settings,
            reconciliation_readiness=readiness,
            intents=SimpleNamespace(count_opening_since=lambda **_: 0),
            clock=clock,
        )
        contract = stock_contract().model_copy(
            update={
                "liquid_hours": "20260717:0930-1600",
                "time_zone_id": "America/New_York",
            }
        )
        intent = build_stock_intent(
            account_id=PAPER_ACCOUNT,
            intent=OrderIntent.OPEN_LONG_STOCK,
            contract=contract,
            quantity=1,
            limit_price=Decimal("100"),
        )
        try:
            evidence = provider.gather(intent, now=clock())
        except AccountSummaryFreshnessError as refusal:
            assert refusal.reason.value == "UNVERIFIED_PROVENANCE"
            assert PAPER_ACCOUNT not in str(refusal)
        else:
            decision = OrderRiskEngine(
                settings, persistence_key=TEST_PERSISTENCE_KEY
            ).evaluate(intent, evidence, now=clock())
            assert decision.approved, decision.checks
            pytest.fail("cached rows with no refresh reached stock/cash risk approval: approved=True")
        assert client.summary_requests == 0
    finally:
        asyncio.run(broker.disconnect())
