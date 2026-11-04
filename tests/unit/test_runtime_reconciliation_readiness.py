import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

from tests.support.order_fakes import FIXED_NOW, PAPER_ACCOUNT, FakeBroker

from chronos.api.reconciliation_loop import reconcile_once
from chronos.api.routes.orders import ReconciliationResponse
from chronos.domain.enums import ConnectionState, OrderLifecycle, ReconciliationStatus
from chronos.domain.models import BrokerPosition, LocalReconciliationEvidence, UnderlyingContract
from chronos.operations.health import OperationalFacts, evaluate_operational_health
from chronos.orders.reconciliation_readiness import ReadinessBlocker, ReconciliationReadiness
from chronos.orders.reconciliation_recovery import (
    OrderRestartReconciliationReport,
    RemainingActiveIntent,
    RestartOrderObservation,
)
from chronos.runtime import AppRuntime, _reconciliation_run_snapshot
from chronos.services.reconciliation import ReconciliationCoordinator, ReconciliationResult


class _SyncConnection:
    def __init__(self, broker: FakeBroker) -> None:
        self.broker = broker
        self._broker = broker

    def run(self, coroutine: object, *, timeout: float | None = None) -> object:
        del timeout
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


class _FixedPortfolioReconciliation:
    def __init__(self, result: ReconciliationResult) -> None:
        self.result = result

    def reconcile(self) -> ReconciliationResult:
        return self.result


class _CompleteLocalReader:
    def read(self, current_account_id: str) -> LocalReconciliationEvidence:
        assert current_account_id == PAPER_ACCOUNT
        return LocalReconciliationEvidence(complete=True)


def _restart_report(
    *,
    unresolved: tuple[RestartOrderObservation, ...] = (),
    remaining_active: tuple[RemainingActiveIntent, ...] = (),
) -> OrderRestartReconciliationReport:
    return OrderRestartReconciliationReport(
        proven=(),
        unresolved=unresolved,
        remaining_active=remaining_active,
        applied_updates=(),
    )


def _portfolio_result(
    *,
    status: ReconciliationStatus,
    snapshot: object | None,
    symbols: tuple[object, ...] = (),
) -> ReconciliationResult:
    return ReconciliationResult.model_construct(
        status=status,
        snapshot=snapshot,
        symbols=symbols,
        opening_actions_locked=True,
        reasons=(),
    )


def _runtime_for(
    restart: OrderRestartReconciliationReport,
    portfolio: ReconciliationResult,
) -> AppRuntime:
    runtime = AppRuntime.__new__(AppRuntime)
    runtime.reconciliation_readiness = ReconciliationReadiness()
    runtime.order_management = cast(Any, _RestartService(restart))
    runtime.reconciliation = cast(Any, _FixedPortfolioReconciliation(portfolio))
    broker = FakeBroker()
    runtime.broker = cast(Any, broker)
    runtime.connection = cast(Any, _SyncConnection(broker))
    return runtime


def _unresolved_restart_observation() -> RestartOrderObservation:
    return RestartOrderObservation(
        intent_id="intent-unresolved",
        local_status=OrderLifecycle.SUBMISSION_UNKNOWN,
        broker_status=None,
        transition_applied=False,
        reason="broker evidence is unresolved",
    )


def _manual_review_portfolio(
    *,
    open_orders: tuple[object, ...] = (),
    snapshot: object | None = None,
    symbols: tuple[object, ...] = (),
) -> ReconciliationResult:
    if snapshot is None and open_orders:
        snapshot = SimpleNamespace(open_orders=open_orders)
    elif snapshot is None:
        snapshot = SimpleNamespace(open_orders=())
    return _portfolio_result(
        status=ReconciliationStatus.MANUAL_REVIEW,
        snapshot=snapshot,
        symbols=symbols,
    )


def test_manual_review_union_keeps_all_restart_blockers_and_base_reason() -> None:
    restart = _restart_report(
        unresolved=(_unresolved_restart_observation(),),
        remaining_active=(
            RemainingActiveIntent(intent_id="intent-active", status=OrderLifecycle.SUBMITTED),
        ),
    )
    runtime = _runtime_for(restart, _manual_review_portfolio())

    report = runtime.reconcile_submission_readiness(now=FIXED_NOW)

    assert report.readiness.status is ReconciliationStatus.MANUAL_REVIEW
    assert report.readiness.reason == "portfolio reconciliation requires manual review"
    assert report.readiness.blockers == frozenset(
        {
            ReadinessBlocker.RESTART_SENT_ACTIVE,
            ReadinessBlocker.RESTART_BROKER_EVIDENCE_INCOMPLETE,
        }
    )


def test_manual_review_union_includes_open_orders_without_short_circuiting() -> None:
    restart = _restart_report(
        unresolved=(_unresolved_restart_observation(),),
        remaining_active=(
            RemainingActiveIntent(intent_id="intent-active", status=OrderLifecycle.SUBMITTED),
        ),
    )
    runtime = _runtime_for(
        restart,
        _manual_review_portfolio(open_orders=(object(),)),
    )

    report = runtime.reconcile_submission_readiness(now=FIXED_NOW)

    assert report.readiness.status is ReconciliationStatus.MANUAL_REVIEW
    assert report.readiness.reason == "portfolio reconciliation requires manual review"
    assert report.readiness.blockers == frozenset(
        {
            ReadinessBlocker.OPEN_ORDERS,
            ReadinessBlocker.RESTART_SENT_ACTIVE,
            ReadinessBlocker.RESTART_BROKER_EVIDENCE_INCOMPLETE,
        }
    )


def test_manual_review_union_reports_only_incomplete_restart_evidence() -> None:
    runtime = _runtime_for(
        _restart_report(unresolved=(_unresolved_restart_observation(),)),
        _manual_review_portfolio(),
    )

    report = runtime.reconcile_submission_readiness(now=FIXED_NOW)

    assert report.readiness.status is ReconciliationStatus.MANUAL_REVIEW
    assert report.readiness.reason == "portfolio reconciliation requires manual review"
    assert report.readiness.blockers == frozenset(
        {ReadinessBlocker.RESTART_BROKER_EVIDENCE_INCOMPLETE}
    )


def test_manual_review_without_snapshot_is_pass_incomplete_not_open_orders() -> None:
    runtime = _runtime_for(
        _restart_report(),
        _portfolio_result(status=ReconciliationStatus.MANUAL_REVIEW, snapshot=None),
    )

    report = runtime.reconcile_submission_readiness(now=FIXED_NOW)

    assert report.readiness.status is ReconciliationStatus.MANUAL_REVIEW
    assert report.readiness.reason == "portfolio reconciliation requires manual review"
    assert report.readiness.blockers == frozenset({ReadinessBlocker.PASS_INCOMPLETE})
    assert ReadinessBlocker.OPEN_ORDERS not in report.readiness.blockers


def test_reconciled_without_snapshot_is_pass_incomplete() -> None:
    runtime = _runtime_for(
        _restart_report(),
        _portfolio_result(status=ReconciliationStatus.RECONCILED, snapshot=None),
    )

    report = runtime.reconcile_submission_readiness(now=FIXED_NOW)

    assert report.readiness.status is ReconciliationStatus.PENDING
    assert report.readiness.reason == "portfolio reconciliation did not produce complete evidence"
    assert report.readiness.blockers == frozenset({ReadinessBlocker.PASS_INCOMPLETE})


def test_scope_change_is_a_typed_blocker() -> None:
    portfolio = _portfolio_result(
        status=ReconciliationStatus.RECONCILED,
        snapshot=SimpleNamespace(open_orders=()),
    )
    runtime = _runtime_for(_restart_report(), portfolio)

    class _DisconnectedConnection(_SyncConnection):
        def connection_status(self) -> object:
            return SimpleNamespace(
                connected=False,
                state=ConnectionState.DISCONNECTED,
                account_id=PAPER_ACCOUNT,
            )

    broker = FakeBroker()
    runtime.connection = cast(Any, _DisconnectedConnection(broker))

    report = runtime.reconcile_submission_readiness(now=FIXED_NOW)

    assert report.readiness.status is ReconciliationStatus.PENDING
    assert report.readiness.reason == (
        "broker connection/account scope changed during reconciliation"
    )
    assert report.readiness.blockers == frozenset({ReadinessBlocker.SCOPE_CHANGED})


class _RaisingBlockerSymbol:
    @property
    def blocker_codes(self) -> tuple[str, ...]:
        raise RuntimeError("sentinel blocker read failure")


@contextmanager
def _runtime_logs() -> Iterator[list[logging.LogRecord]]:
    """Capture the runtime's own logger, whatever an earlier test did to logging.

    ``caplog`` handles the root logger, and ``configure_logging`` sets ``chronos.propagate =
    False`` process-wide (utils/logging.py), so once any earlier test has configured logging a
    root-level capture sees no chronos record. pytest 9.1 happens to attach its capture handler
    to non-propagating loggers, which hid this on the locked version; an older pytest in a stale
    venv (8.4.2) does not, and the same test failed there in the full run. A handler on the
    logger itself works on both.
    """

    collected: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            collected.append(record)

    logger = logging.getLogger("chronos.runtime")
    handler = _Collector(level=logging.DEBUG)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield collected
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def test_blocker_union_failure_is_guarded_and_reconcile_once_succeeds() -> None:
    portfolio = _manual_review_portfolio(symbols=(_RaisingBlockerSymbol(),))
    runtime = _runtime_for(_restart_report(), portfolio)

    with _runtime_logs() as records:
        assert reconcile_once(runtime) == (True, False)
    snapshot = runtime.reconciliation_readiness.snapshot()

    assert snapshot.status is ReconciliationStatus.MANUAL_REVIEW
    assert snapshot.reason == "portfolio reconciliation requires manual review"
    assert snapshot.blockers == frozenset({ReadinessBlocker.BLOCKERS_UNDETERMINED})
    assert any(
        getattr(record, "event", None) == "readiness_blockers_undetermined" for record in records
    )


def test_writer_uses_symbol_codes_without_matching_reason_text() -> None:
    reason_with_code_words = "A nonzero broker position lacks complete cycle provenance."
    for reasons, codes, expected in (
        ((reason_with_code_words,), (), frozenset()),
        (
            (),
            (ReadinessBlocker.HELD_POSITION_UNPROVEN.value,),
            frozenset({ReadinessBlocker.HELD_POSITION_UNPROVEN}),
        ),
    ):
        symbol = SimpleNamespace(reasons=reasons, blocker_codes=codes)
        portfolio = _manual_review_portfolio(
            snapshot=SimpleNamespace(open_orders=()),
            symbols=(symbol,),
        )
        runtime = _runtime_for(_restart_report(), portfolio)

        report = runtime.reconcile_submission_readiness(now=FIXED_NOW)

        assert report.readiness.blockers == expected


def test_real_reconciler_held_position_code_reaches_readiness_writer() -> None:
    broker = FakeBroker(
        positions=(
            BrokerPosition(
                account_id=PAPER_ACCOUNT,
                contract=UnderlyingContract(con_id=1001, symbol="AAPL"),
                quantity=Decimal("100"),
                average_cost=Decimal("10"),
                market_price=Decimal("10"),
            ),
        )
    )
    connection = _SyncConnection(broker)
    coordinator = ReconciliationCoordinator(
        connection,
        _CompleteLocalReader(),
        ("AAPL",),
    )
    runtime = _runtime_for(
        _restart_report(),
        _portfolio_result(
            status=ReconciliationStatus.RECONCILED,
            snapshot=SimpleNamespace(open_orders=()),
        ),
    )
    runtime.reconciliation = cast(Any, coordinator)
    runtime.broker = cast(Any, broker)
    runtime.connection = cast(Any, connection)

    report = runtime.reconcile_submission_readiness(now=FIXED_NOW)

    assert report.portfolio.status is ReconciliationStatus.MANUAL_REVIEW
    assert ReadinessBlocker.HELD_POSITION_UNPROVEN in report.readiness.blockers
    assert report.readiness.status is ReconciliationStatus.MANUAL_REVIEW


def test_symbol_blocker_codes_default_to_empty_on_clean_reconciliation() -> None:
    runtime = _runtime_for(
        _restart_report(),
        _portfolio_result(
            status=ReconciliationStatus.RECONCILED,
            snapshot=SimpleNamespace(open_orders=()),
            symbols=(SimpleNamespace(reasons=(), blocker_codes=()),),
        ),
    )

    report = runtime.reconcile_submission_readiness(now=FIXED_NOW)

    assert report.readiness.status is ReconciliationStatus.RECONCILED
    assert report.readiness.blockers == frozenset()


def test_blocker_metadata_does_not_change_persisted_or_served_key_sets() -> None:
    runtime = _runtime_for(
        _restart_report(),
        _portfolio_result(status=ReconciliationStatus.MANUAL_REVIEW, snapshot=None),
    )
    report = runtime.reconcile_submission_readiness(now=FIXED_NOW)
    persisted = _reconciliation_run_snapshot(
        trigger="periodic",
        report=report,
        generation=report.readiness.generation,
        started_at=FIXED_NOW,
        account_fingerprint="fingerprint-for-shape-test",
    ).model_dump(mode="json")
    api_payload = ReconciliationResponse(
        status=report.readiness.status.value,
        generation=report.readiness.generation,
        applied_count=0,
        proven_count=0,
        unresolved_count=0,
        remaining_active_count=0,
    ).model_dump(mode="json")
    health_payload = evaluate_operational_health(OperationalFacts(), now=FIXED_NOW).model_dump(
        mode="json"
    )

    assert set(persisted) == {
        "run_id",
        "trigger",
        "status",
        "started_at",
        "completed_at",
        "account",
        "positions",
        "open_orders",
        "evidence_age_seconds",
        "execution_count",
        "decisions",
        "reasons",
        "readiness_status",
        "readiness_reason",
        "generation",
    }
    assert set(api_payload) == {
        "status",
        "generation",
        "applied_count",
        "proven_count",
        "unresolved_count",
        "remaining_active_count",
    }
    assert set(health_payload) == {
        "assessed_at",
        "liveness",
        "service_readiness",
        "trading_capability",
        "observations",
    }
    assert set(health_payload["observations"]) == {
        "writer_role",
        "store_readable",
        "startup_faults",
        "tasks",
        "broker_loop_running",
        "broker",
        "reconciliation",
        "clock",
        "clock_evidence",
        "slo",
    }


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
