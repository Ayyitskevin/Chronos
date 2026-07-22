from decimal import Decimal

from tests.support.order_fakes import (
    FIXED_NOW,
    PAPER_ACCOUNT,
    FakeBroker,
    option_contract,
    paper_settings,
)

from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.enums import OrderIntent, ReconciliationStatus
from chronos.orders.evidence import BrokerRiskEvidenceProvider
from chronos.orders.intent import build_option_intent
from chronos.orders.reconciliation_readiness import ReconciliationReadiness


def test_provider_reads_shared_reconciliation_readiness_each_time() -> None:
    readiness = ReconciliationReadiness()
    broker = FakeBroker()
    connection = BrokerConnectionManager(broker, readiness=readiness)
    connection.start()
    provider = BrokerRiskEvidenceProvider(
        connection=connection,
        settings=paper_settings(),
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
        pending = provider.gather(intent, now=FIXED_NOW)
        assert pending.reconciliation_status is ReconciliationStatus.PENDING

        assert readiness.complete(
            expected_generation=readiness.snapshot().generation,
            status=ReconciliationStatus.RECONCILED,
            reason="test broker/local parity",
            reconciled_at=FIXED_NOW,
        )
        reconciled = provider.gather(intent, now=FIXED_NOW)
        assert reconciled.reconciliation_status is ReconciliationStatus.RECONCILED
        reconciled_snapshot = readiness.snapshot()
        assert reconciled.reconciliation_generation == reconciled_snapshot.generation
        assert reconciled.reconciliation_session_id == reconciled_snapshot.session_id

        async def positions_with_invalidation() -> tuple[object, ...]:
            readiness.invalidate("connection changed during evidence gathering")
            return ()

        broker.positions = positions_with_invalidation  # type: ignore[method-assign]
        raced = provider.gather(intent, now=FIXED_NOW)
        assert raced.reconciliation_status is ReconciliationStatus.PENDING
        assert raced.reconciliation_generation is None
        assert raced.reconciliation_session_id is None
    finally:
        connection.close()
