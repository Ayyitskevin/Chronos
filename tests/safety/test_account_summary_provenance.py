"""Unverified broker caches must stay displayable but cannot supply money evidence."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from ib_async import IB

from chronos.broker.ibkr import IBKRBroker
from chronos.domain.account_freshness import (
    AccountSummaryFreshnessError,
    account_summary_provenance,
    require_account_summary_freshness,
)
from chronos.domain.enums import AccountSummaryProvenance, OrderIntent, ReconciliationStatus
from chronos.domain.models import AccountSummary
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

    def clock() -> datetime:
        return moment[0]

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
        readiness = ReconciliationReadiness(max_evidence_age=timedelta(seconds=300), clock=clock)
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
            decision = OrderRiskEngine(settings, persistence_key=TEST_PERSISTENCE_KEY).evaluate(
                intent, evidence, now=clock()
            )
            assert decision.approved, decision.checks
            pytest.fail(
                "cached rows with no refresh reached stock/cash risk approval: approved=True"
            )
        assert client.summary_requests == 0
    finally:
        asyncio.run(broker.disconnect())


@pytest.mark.parametrize("metadata", [None, "VERIFIED_RESPONSE", "UNKNOWN", True, object()])
def test_untyped_proof_cannot_become_financial_evidence(metadata: object) -> None:
    summary = SimpleNamespace(provenance=metadata)
    proof = account_summary_provenance(summary)
    assert proof is AccountSummaryProvenance.UNKNOWN
    with pytest.raises(AccountSummaryFreshnessError, match="unverified_provenance"):
        require_account_summary_freshness(
            FIXED_NOW, now=FIXED_NOW, max_age_seconds=30, provenance=proof
        )


def test_missing_proof_defaults_unknown_and_verified_label_survives_serialization() -> None:
    summary = AccountSummary(
        account_id=PAPER_ACCOUNT,
        net_liquidation=Decimal("100000"),
        total_cash=Decimal("80000"),
        buying_power=Decimal("160000"),
        as_of=FIXED_NOW,
    )
    assert summary.provenance is AccountSummaryProvenance.UNKNOWN
    assert account_summary_provenance(SimpleNamespace()) is AccountSummaryProvenance.UNKNOWN
    with pytest.raises(AccountSummaryFreshnessError, match="unverified_provenance"):
        require_account_summary_freshness(FIXED_NOW, now=FIXED_NOW, max_age_seconds=30)
    verified = summary.model_copy(update={"provenance": AccountSummaryProvenance.VERIFIED_RESPONSE})
    roundtrip = AccountSummary.model_validate_json(verified.model_dump_json())
    assert roundtrip.provenance is AccountSummaryProvenance.VERIFIED_RESPONSE
    require_account_summary_freshness(
        roundtrip.as_of,
        now=FIXED_NOW,
        max_age_seconds=30,
        provenance=account_summary_provenance(roundtrip),
    )


def test_broken_provenance_property_is_unknown() -> None:
    class Broken:
        @property
        def provenance(self) -> object:
            raise RuntimeError("synthetic adapter bug")

    assert account_summary_provenance(Broken()) is AccountSummaryProvenance.UNKNOWN


def test_verified_and_unverified_summaries_at_real_option_risk_seam() -> None:
    from tests.safety.test_account_summary_freshness import (
        DEMO_NOW,
        _account_summary,
        _gather_with_direct_runner,
    )

    verified = _account_summary(DEMO_NOW)
    evidence = _gather_with_direct_runner(verified, clock=lambda: DEMO_NOW)
    assert evidence.account.total_cash == Decimal("80000")
    for proof in (AccountSummaryProvenance.UNKNOWN, None, "VERIFIED_RESPONSE"):
        unverified = verified.model_copy(update={"provenance": proof})
        with pytest.raises(AccountSummaryFreshnessError, match="unverified_provenance"):
            _gather_with_direct_runner(unverified, clock=lambda: DEMO_NOW)


def test_demo_response_still_supplies_verified_financial_balances() -> None:
    from chronos.broker.demo import DemoBroker

    broker = DemoBroker()
    asyncio.run(broker.connect())
    try:
        summary = asyncio.run(broker.account_summary())
    finally:
        asyncio.run(broker.disconnect())
    assert summary.provenance is AccountSummaryProvenance.VERIFIED_RESPONSE
    assert summary.total_cash > 0
    require_account_summary_freshness(
        summary.as_of,
        now=summary.as_of,
        max_age_seconds=30,
        provenance=account_summary_provenance(summary),
    )


@pytest.mark.parametrize(
    "proof", [AccountSummaryProvenance.UNKNOWN, AccountSummaryProvenance.VERIFIED_RESPONSE]
)
def test_reconciliation_and_json_facts_preserve_provenance(proof) -> None:
    from chronos.persistence.reconciliation_repository import ReconciliationSnapshot
    from tests.unit.test_reconciliation import NOW, _coordinator, _ScriptedBroker

    broker = _ScriptedBroker()
    broker.account_snapshots = tuple(
        summary.model_copy(update={"provenance": proof}) for summary in broker.account_snapshots
    )
    coordinator, _, _ = _coordinator(broker)
    result = coordinator.reconcile()
    assert result.status is ReconciliationStatus.RECONCILED
    assert result.snapshot is not None
    assert result.snapshot.account.provenance is proof
    snapshot = ReconciliationSnapshot.from_result(
        run_id="synthetic-proof",
        trigger="operator",
        portfolio=result,
        readiness_status=ReconciliationStatus.RECONCILED,
        readiness_reason="synthetic",
        generation=0,
        started_at=NOW,
        completed_at=NOW,
        account_fingerprint="a" * 64,
    )
    assert snapshot.account is not None
    assert snapshot.account.provenance is proof
    restored = ReconciliationSnapshot.model_validate_json(snapshot.model_dump_json())
    assert restored.account is not None
    assert restored.account.provenance is proof
    assert PAPER_ACCOUNT not in snapshot.model_dump_json()


async def test_official_direct_response_is_verified_but_pure_rows_are_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chronos.broker.official_ibkr import account_summary_from_rows
    from tests.support import fake_ibapi
    from tests.unit.test_fake_ibapi import _broker

    rows = [
        (PAPER_ACCOUNT, tag, value, "USD")
        for tag, value in (
            ("NetLiquidation", "100000"),
            ("TotalCashValue", "80000"),
            ("BuyingPower", "160000"),
        )
    ]
    unverified = account_summary_from_rows(rows, expected_account=PAPER_ACCOUNT, as_of=FIXED_NOW)
    assert unverified.provenance is AccountSummaryProvenance.UNKNOWN
    gateway = fake_ibapi.FakeGateway.seeded()
    fake_ibapi.install(monkeypatch, gateway)
    broker = _broker()
    await broker.connect()
    try:
        summary = await broker.account_summary()
    finally:
        await broker.disconnect()
    assert summary.provenance is AccountSummaryProvenance.VERIFIED_RESPONSE
    assert summary.total_cash == Decimal("99499.50")


async def test_sim_response_keeps_its_controlled_balances_usable() -> None:
    from tests.support.sim_broker import SimBroker

    broker = SimBroker(account_id=PAPER_ACCOUNT, clock=lambda: FIXED_NOW, symbols={"AAPL": 1})
    await broker.connect()
    try:
        summary = await broker.account_summary()
    finally:
        await broker.disconnect()
    assert summary.provenance is AccountSummaryProvenance.VERIFIED_RESPONSE
    assert summary.total_cash == Decimal("100000")
