import asyncio
from types import SimpleNamespace
from typing import Any, cast

from tests.support.order_fakes import FIXED_NOW, PAPER_ACCOUNT, FakeBroker

from chronos.domain.enums import OrderLifecycle, ReconciliationStatus
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.reconciliation_recovery import (
    OrderRestartReconciliationReport,
    RemainingActiveIntent,
)
from chronos.runtime import AppRuntime
from chronos.services.reconciliation import ReconciliationResult


class _SyncConnection:
    def __init__(self, broker: FakeBroker) -> None:
        self._broker = broker

    def run(self, coroutine: object) -> object:
        return asyncio.run(cast(Any, coroutine))

    def connection_status(self) -> object:
        return asyncio.run(self._broker.connection_status())


class _RestartService:
    account_id = PAPER_ACCOUNT

    def __init__(self, report: OrderRestartReconciliationReport) -> None:
        self.report = report

    def reconcile_on_restart_report(
        self, *, now: object = None
    ) -> OrderRestartReconciliationReport:
        del now
        return self.report


class _PortfolioReconciliation:
    def __init__(self, *, has_open_orders: bool = False) -> None:
        self.has_open_orders = has_open_orders

    def reconcile(self) -> ReconciliationResult:
        snapshot = SimpleNamespace(
            open_orders=(object(),) if self.has_open_orders else (),
        )
        return ReconciliationResult.model_construct(
            status=ReconciliationStatus.RECONCILED,
            snapshot=snapshot,
            symbols=(),
            opening_actions_locked=True,
            reasons=(),
        )


def test_sent_active_intent_keeps_composite_readiness_pending() -> None:
    restart = OrderRestartReconciliationReport(
        proven=(),
        unresolved=(),
        remaining_active=(
            RemainingActiveIntent(
                intent_id="intent-1",
                status=OrderLifecycle.SUBMITTED,
            ),
        ),
        applied_updates=(),
    )
    runtime = AppRuntime.__new__(AppRuntime)
    runtime.reconciliation_readiness = ReconciliationReadiness()
    runtime.order_management = cast(Any, _RestartService(restart))
    runtime.reconciliation = cast(Any, _PortfolioReconciliation())
    broker = FakeBroker()
    runtime.broker = cast(Any, broker)
    runtime.connection = cast(Any, _SyncConnection(broker))

    report = runtime.reconcile_submission_readiness(now=FIXED_NOW)

    assert report.readiness.status is ReconciliationStatus.PENDING
    assert report.readiness.ready is False
    assert "sent-active" in report.readiness.reason


def test_broker_open_order_keeps_composite_readiness_pending() -> None:
    restart = OrderRestartReconciliationReport(
        proven=(),
        unresolved=(),
        remaining_active=(),
        applied_updates=(),
    )
    runtime = AppRuntime.__new__(AppRuntime)
    runtime.reconciliation_readiness = ReconciliationReadiness()
    runtime.order_management = cast(Any, _RestartService(restart))
    runtime.reconciliation = cast(Any, _PortfolioReconciliation(has_open_orders=True))
    broker = FakeBroker()
    runtime.broker = cast(Any, broker)
    runtime.connection = cast(Any, _SyncConnection(broker))

    report = runtime.reconcile_submission_readiness(now=FIXED_NOW)

    assert report.readiness.status is ReconciliationStatus.PENDING
    assert report.readiness.ready is False
    assert "broker open orders" in report.readiness.reason
