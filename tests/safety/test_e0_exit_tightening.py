"""E-0 pins for exit-risk evidence and reconciliation readiness."""

from __future__ import annotations

from decimal import Decimal

from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.enums import OrderIntent, ReconciliationStatus, RiskCheckStatus
from chronos.domain.models import BrokerPosition
from chronos.orders.evidence import BrokerRiskEvidenceProvider
from chronos.orders.intent import build_stock_intent
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.risk import OrderRiskEngine, RiskEvidence
from tests.support.order_fakes import (
    FIXED_NOW,
    PAPER_ACCOUNT,
    FakeBroker,
    paper_settings,
    stock_contract,
)
from tests.support.persistence_keys import TEST_PERSISTENCE_KEY


def _stock_close():
    return build_stock_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.CLOSE_LONG_STOCK,
        contract=stock_contract(),
        quantity=1,
        limit_price=Decimal("100"),
    )


def _manual_review(readiness: ReconciliationReadiness) -> None:
    snapshot = readiness.snapshot()
    assert readiness.complete(
        expected_generation=snapshot.generation,
        status=ReconciliationStatus.MANUAL_REVIEW,
        reason="portfolio reconciliation requires manual review",
    )


def _reconciled_proven_state_detail(evidence: RiskEvidence) -> str:
    decision = OrderRiskEngine(paper_settings(), persistence_key=TEST_PERSISTENCE_KEY).evaluate(
        _stock_close(), evidence, now=FIXED_NOW
    )
    check = next(check for check in decision.checks if check.name == "reconciled_proven_state")
    assert check.status is RiskCheckStatus.FAIL
    return check.detail


def test_manual_review_survives_a_consistent_gather_bracket() -> None:
    readiness = ReconciliationReadiness()
    broker = FakeBroker()
    connection = BrokerConnectionManager(broker, readiness=readiness)
    connection.start()
    try:
        _manual_review(readiness)
        evidence = BrokerRiskEvidenceProvider(
            connection=connection,
            settings=paper_settings(),
            reconciliation_readiness=readiness,
        ).gather(_stock_close(), now=FIXED_NOW)

        assert evidence.reconciliation_status is ReconciliationStatus.MANUAL_REVIEW
        assert evidence.reconciliation_generation is None
        assert evidence.reconciliation_session_id is None
        assert (
            _reconciled_proven_state_detail(evidence)
            == "reconciliation is MANUAL_REVIEW, not RECONCILED"
        )
    finally:
        connection.close()


def test_manual_review_republished_in_a_new_generation_is_not_bracketed() -> None:
    readiness = ReconciliationReadiness()
    broker = FakeBroker()
    connection = BrokerConnectionManager(broker, readiness=readiness)
    connection.start()
    try:
        _manual_review(readiness)
        original_positions = broker.positions

        async def positions_after_a_new_manual_review_generation() -> tuple[BrokerPosition, ...]:
            generation = readiness.begin_reconciliation("connection changed during gather")
            assert readiness.complete(
                expected_generation=generation,
                status=ReconciliationStatus.MANUAL_REVIEW,
                reason="portfolio reconciliation requires manual review",
            )
            return await original_positions()

        broker.positions = positions_after_a_new_manual_review_generation  # type: ignore[method-assign]
        evidence = BrokerRiskEvidenceProvider(
            connection=connection,
            settings=paper_settings(),
            reconciliation_readiness=readiness,
        ).gather(_stock_close(), now=FIXED_NOW)

        assert readiness.snapshot().status is ReconciliationStatus.MANUAL_REVIEW
        assert evidence.reconciliation_status is ReconciliationStatus.PENDING
        assert evidence.reconciliation_generation is None
        assert evidence.reconciliation_session_id is None
    finally:
        connection.close()
