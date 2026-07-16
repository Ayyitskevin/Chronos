import asyncio
import logging
from collections.abc import Coroutine
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar, cast

import pytest

from chronos.broker.base import Broker
from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    DisplayEnvironment,
    OptionRight,
    OrderLifecycle,
    OrderSide,
    ReconciliationStatus,
    WheelStage,
)
from chronos.domain.models import (
    AccountSummary,
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    ConnectionStatus,
    OptionContract,
    UnderlyingContract,
)
from chronos.services.reconciliation import (
    LocalReconciliationEvidence,
    ReconciliationCoordinator,
    ReconciliationResult,
)

_T = TypeVar("_T")

ACCOUNT_ID = "DU1234567"
OTHER_ACCOUNT_ID = "DU7654321"
NOW = datetime(2026, 1, 15, 15, 30, tzinfo=UTC)


class _ScriptedBroker:
    def __init__(
        self,
        *,
        positions: tuple[
            tuple[BrokerPosition, ...],
            tuple[BrokerPosition, ...],
        ] = ((), ()),
        orders: tuple[tuple[BrokerOrder, ...], tuple[BrokerOrder, ...]] = ((), ()),
        executions: tuple[
            tuple[BrokerExecution, ...],
            tuple[BrokerExecution, ...],
        ] = ((), ()),
        accounts: tuple[AccountSummary, AccountSummary] | None = None,
        status_end_account_id: str = ACCOUNT_ID,
        server_time_failure: Exception | None = None,
    ) -> None:
        account = AccountSummary(
            account_id=ACCOUNT_ID,
            net_liquidation=Decimal("250000"),
            total_cash=Decimal("125000"),
            buying_power=Decimal("240000"),
            as_of=NOW,
        )
        self.account_snapshots = accounts or (account, account)
        self.statuses = (
            _status(ACCOUNT_ID),
            _status(status_end_account_id),
        )
        self.server_times = (NOW, NOW + timedelta(seconds=1))
        self.position_snapshots = positions
        self.order_snapshots = orders
        self.execution_snapshots = executions
        self.server_time_failure = server_time_failure
        self.calls = {
            "status": 0,
            "time": 0,
            "account": 0,
            "positions": 0,
            "orders": 0,
            "executions": 0,
        }

    async def connection_status(self) -> ConnectionStatus:
        index = self.calls["status"]
        self.calls["status"] += 1
        return self.statuses[index]

    async def server_time(self) -> datetime:
        index = self.calls["time"]
        self.calls["time"] += 1
        if index == 0 and self.server_time_failure is not None:
            raise self.server_time_failure
        return self.server_times[index]

    async def account_summary(self) -> AccountSummary:
        index = self.calls["account"]
        self.calls["account"] += 1
        return self.account_snapshots[index]

    async def positions(self) -> tuple[BrokerPosition, ...]:
        index = self.calls["positions"]
        self.calls["positions"] += 1
        return self.position_snapshots[index]

    async def open_orders(self) -> tuple[BrokerOrder, ...]:
        index = self.calls["orders"]
        self.calls["orders"] += 1
        return self.order_snapshots[index]

    async def executions(self, since: datetime | None = None) -> tuple[BrokerExecution, ...]:
        assert since is None
        index = self.calls["executions"]
        self.calls["executions"] += 1
        return self.execution_snapshots[index]


class _CountingRunner:
    def __init__(self, broker: _ScriptedBroker) -> None:
        self.broker = cast(Broker, broker)
        self.run_calls = 0
        self.timeouts: list[float | None] = []
        self.inside_run = False
        self.failure: Exception | None = None

    def run(
        self,
        coroutine: Coroutine[Any, Any, _T],
        *,
        timeout: float | None = None,
    ) -> _T:
        self.run_calls += 1
        self.timeouts.append(timeout)
        if self.failure is not None:
            coroutine.close()
            raise self.failure
        self.inside_run = True
        try:
            return asyncio.run(coroutine)
        finally:
            self.inside_run = False


class _LocalReader:
    def __init__(
        self,
        evidence: LocalReconciliationEvidence,
        *,
        runner: _CountingRunner | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.evidence = evidence
        self.runner = runner
        self.failure = failure
        self.calls: list[str] = []

    def read(self, current_account_id: str) -> LocalReconciliationEvidence:
        if self.runner is not None:
            assert self.runner.inside_run is False
        self.calls.append(current_account_id)
        if self.failure is not None:
            raise self.failure
        return self.evidence


def _status(account_id: str) -> ConnectionStatus:
    return ConnectionStatus(
        state=ConnectionState.CONNECTED,
        environment=DisplayEnvironment.PAPER,
        connected=True,
        account_id=account_id,
        data_quality=DataQuality.LIVE,
        last_successful_sync=NOW,
    )


def _stock(
    *,
    account_id: str = ACCOUNT_ID,
    quantity: str = "100",
    market_price: str = "190",
) -> BrokerPosition:
    return BrokerPosition(
        account_id=account_id,
        contract=UnderlyingContract(
            con_id=1001,
            symbol="AAPL",
            primary_exchange="NASDAQ",
        ),
        quantity=Decimal(quantity),
        average_cost=Decimal("180"),
        market_price=Decimal(market_price),
        unrealized_pnl=(Decimal(market_price) - Decimal("180")) * Decimal(quantity),
    )


def _put_contract(*, right: OptionRight = OptionRight.PUT) -> OptionContract:
    right_code = "P" if right is OptionRight.PUT else "C"
    return OptionContract(
        con_id=2001,
        symbol="AAPL",
        underlying_con_id=1001,
        expiration=date(2026, 2, 20),
        strike=Decimal("180"),
        right=right,
        multiplier=Decimal("100"),
        trading_class="AAPL",
        local_symbol=f"AAPL 260220{right_code}00180000",
        deliverable_shares=Decimal("100"),
        deliverable_verified=True,
    )


def _put_order(
    *,
    right: OptionRight = OptionRight.PUT,
    side: OrderSide = OrderSide.SELL,
    filled_quantity: str = "0",
    lifecycle: OrderLifecycle = OrderLifecycle.SUBMITTED,
) -> BrokerOrder:
    filled = Decimal(filled_quantity)
    return BrokerOrder(
        broker_order_id=7001,
        permanent_id=9001,
        client_id=17,
        account_id=ACCOUNT_ID,
        order_ref="CHR-AAPL-PUT",
        contract=_put_contract(right=right),
        side=side,
        quantity=Decimal("1"),
        filled_quantity=filled,
        remaining_quantity=Decimal("1") - filled,
        limit_price=Decimal("2.50"),
        lifecycle=lifecycle,
    )


def _execution() -> BrokerExecution:
    return BrokerExecution(
        execution_id="EXEC-1",
        account_id=ACCOUNT_ID,
        broker_order_id=7001,
        permanent_id=9001,
        client_id=17,
        order_ref="CHR-AAPL-PUT",
        contract=_put_contract(),
        side=OrderSide.SELL,
        quantity=Decimal("1"),
        price=Decimal("2.50"),
        timestamp=NOW,
        commission=Decimal("0.65"),
        commission_currency="USD",
    )


def _coordinator(
    broker: _ScriptedBroker,
    evidence: LocalReconciliationEvidence | None = None,
    *,
    allowlisted_symbols: tuple[str, ...] = ("AAPL",),
    failure: Exception | None = None,
    monotonic: Any | None = None,
    max_observation_seconds: float = 30.0,
) -> tuple[ReconciliationCoordinator, _CountingRunner, _LocalReader]:
    runner = _CountingRunner(broker)
    reader = _LocalReader(
        evidence or LocalReconciliationEvidence(complete=True),
        runner=runner,
        failure=failure,
    )
    return (
        ReconciliationCoordinator(
            runner,
            reader,
            allowlisted_symbols,
            max_observation_seconds=max_observation_seconds,
            **({"monotonic": monotonic} if monotonic is not None else {}),
        ),
        runner,
        reader,
    )


def test_flat_reconciliation_uses_one_manager_call_and_double_reads() -> None:
    broker = _ScriptedBroker()
    coordinator, runner, reader = _coordinator(broker)

    result = coordinator.reconcile()

    assert runner.run_calls == 1
    assert runner.timeouts == [30.0]
    assert reader.calls == [ACCOUNT_ID]
    assert broker.calls == {
        "status": 2,
        "time": 2,
        "account": 2,
        "positions": 2,
        "orders": 2,
        "executions": 2,
    }
    assert result.status is ReconciliationStatus.RECONCILED
    assert result.opening_actions_locked is True
    assert len(result.symbols) == 1
    assert result.symbols[0].stage is WheelStage.FLAT
    assert result.symbols[0].opening_actions_locked is True
    assert result.symbols[0].manual_review_required is False
    assert result.snapshot is not None
    assert result.snapshot.account.masked_account_id != ACCOUNT_ID
    assert ACCOUNT_ID not in result.model_dump_json()


@pytest.mark.parametrize(
    (
        "case",
        "expected_status",
        "expected_snapshot",
        "expected_symbol_count",
        "expected_pending",
        "expected_manual_review",
        "expected_reason_count",
        "expected_local_reads",
    ),
    (
        ("reconciled", ReconciliationStatus.RECONCILED, True, 1, 0, 0, 0, 1),
        ("manual_review", ReconciliationStatus.MANUAL_REVIEW, True, 2, 0, 1, 1, 1),
        ("broker_failure", ReconciliationStatus.PENDING, False, 1, 1, 0, 1, 0),
        ("broker_evidence_rejected", ReconciliationStatus.PENDING, False, 1, 1, 0, 1, 0),
        ("local_failure", ReconciliationStatus.PENDING, True, 1, 1, 0, 1, 1),
        ("elapsed_window", ReconciliationStatus.PENDING, False, 1, 1, 0, 1, 1),
    ),
)
def test_each_reconciliation_exit_emits_one_aggregate_terminal_event(
    case: str,
    expected_status: ReconciliationStatus,
    expected_snapshot: bool,
    expected_symbol_count: int,
    expected_pending: int,
    expected_manual_review: int,
    expected_reason_count: int,
    expected_local_reads: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    broker = _ScriptedBroker()
    evidence: LocalReconciliationEvidence | None = None
    failure: Exception | None = None
    monotonic: Any | None = None
    allowlisted_symbols = ("AAPL",)
    if case == "manual_review":
        allowlisted_symbols = ("AAPL", "MSFT")
        positions = (_stock(),)
        broker = _ScriptedBroker(positions=(positions, positions))
        evidence = LocalReconciliationEvidence(
            complete=True,
            unresolved_symbols=frozenset({"AAPL"}),
        )
    elif case == "broker_failure":
        broker = _ScriptedBroker(
            server_time_failure=RuntimeError(f"disconnected from {ACCOUNT_ID}"),
        )
    elif case == "broker_evidence_rejected":
        broker = _ScriptedBroker(positions=((), (_stock(),)))
    elif case == "local_failure":
        failure = RuntimeError(f"database failed for {ACCOUNT_ID}")
    elif case == "elapsed_window":
        monotonic_values = iter((100.0, 101.0, 131.0))

        def elapsed_monotonic() -> float:
            return next(monotonic_values)

        monotonic = elapsed_monotonic

    coordinator, _, reader = _coordinator(
        broker,
        evidence,
        allowlisted_symbols=allowlisted_symbols,
        failure=failure,
        monotonic=monotonic,
        max_observation_seconds=30.0,
    )
    reconciliation_logger = logging.getLogger("chronos.services.reconciliation")
    previous_propagate = reconciliation_logger.propagate
    reconciliation_logger.propagate = False
    reconciliation_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="chronos.services.reconciliation"):
            result = coordinator.reconcile()
    finally:
        reconciliation_logger.removeHandler(caplog.handler)
        reconciliation_logger.propagate = previous_propagate

    terminal_records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "reconciliation_completed"
    ]
    assert len(terminal_records) == 1
    terminal = terminal_records[0]
    assert terminal.levelno == logging.INFO
    assert terminal.reconciliation_status == expected_status.value
    assert terminal.snapshot_published is expected_snapshot
    assert terminal.symbol_count == expected_symbol_count
    assert terminal.pending_symbol_count == expected_pending
    assert terminal.manual_review_symbol_count == expected_manual_review
    assert terminal.result_reason_count == expected_reason_count
    assert result.status is expected_status
    assert (result.snapshot is not None) is expected_snapshot
    assert len(reader.calls) == expected_local_reads
    assert ACCOUNT_ID not in terminal.getMessage()
    assert all(reason not in terminal.getMessage() for reason in result.reasons)
    for forbidden_field in (
        "account_id",
        "net_liquidation",
        "total_cash",
        "buying_power",
        "reasons",
        "symbols",
    ):
        assert not hasattr(terminal, forbidden_field)


def test_terminal_diagnostic_failure_returns_the_locked_result_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RaisingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.emit_calls = 0

        def emit(self, record: logging.LogRecord) -> None:
            self.emit_calls += 1
            raise RuntimeError("diagnostic sink unavailable")

    broker = _ScriptedBroker()
    coordinator, runner, reader = _coordinator(broker)
    original_derive = coordinator._derive_result
    derived_results: list[ReconciliationResult] = []

    def capture_derived_result(*args: Any, **kwargs: Any) -> ReconciliationResult:
        result = original_derive(*args, **kwargs)
        derived_results.append(result)
        return result

    monkeypatch.setattr(coordinator, "_derive_result", capture_derived_result)
    handler = RaisingHandler()
    reconciliation_logger = logging.getLogger("chronos.services.reconciliation")
    previous_level = reconciliation_logger.level
    previous_propagate = reconciliation_logger.propagate
    previous_disabled = reconciliation_logger.disabled
    previous_handlers = tuple(reconciliation_logger.handlers)
    for existing_handler in previous_handlers:
        reconciliation_logger.removeHandler(existing_handler)
    reconciliation_logger.setLevel(logging.INFO)
    reconciliation_logger.propagate = False
    reconciliation_logger.disabled = False
    reconciliation_logger.addHandler(handler)
    try:
        result = coordinator.reconcile()
    finally:
        reconciliation_logger.removeHandler(handler)
        for existing_handler in previous_handlers:
            reconciliation_logger.addHandler(existing_handler)
        reconciliation_logger.setLevel(previous_level)
        reconciliation_logger.propagate = previous_propagate
        reconciliation_logger.disabled = previous_disabled

    assert derived_results == [result]
    assert result is derived_results[0]
    assert result.status is ReconciliationStatus.RECONCILED
    assert handler.emit_calls == 1
    assert runner.run_calls == 1
    assert runner.timeouts == [30.0]
    assert reader.calls == [ACCOUNT_ID]
    assert broker.calls == {
        "status": 2,
        "time": 2,
        "account": 2,
        "positions": 2,
        "orders": 2,
        "executions": 2,
    }


@pytest.mark.parametrize(
    ("field", "changed_value"),
    (
        ("net_liquidation", Decimal("249999")),
        ("total_cash", Decimal("124999")),
        ("buying_power", Decimal("239999")),
        ("currency", "EUR"),
    ),
)
def test_changed_account_values_reject_otherwise_stable_broker_evidence(
    field: str,
    changed_value: object,
) -> None:
    account_first = AccountSummary(
        account_id=ACCOUNT_ID,
        net_liquidation=Decimal("250000"),
        total_cash=Decimal("125000"),
        buying_power=Decimal("240000"),
        as_of=NOW,
    )
    account_second = account_first.model_copy(
        update={field: changed_value, "as_of": NOW + timedelta(seconds=1)}
    )
    broker = _ScriptedBroker(accounts=(account_first, account_second))
    coordinator, _, reader = _coordinator(broker)

    result = coordinator.reconcile()

    assert reader.calls == []
    assert result.status is ReconciliationStatus.PENDING
    assert result.snapshot is None
    assert any("changed" in reason for reason in result.reasons)
    assert ACCOUNT_ID not in result.model_dump_json()


def test_account_observation_time_can_refresh_when_values_are_stable() -> None:
    account_first = AccountSummary(
        account_id=ACCOUNT_ID,
        net_liquidation=Decimal("250000"),
        total_cash=Decimal("125000"),
        buying_power=Decimal("240000"),
        as_of=NOW,
    )
    account_second = account_first.model_copy(update={"as_of": NOW + timedelta(seconds=1)})
    broker = _ScriptedBroker(accounts=(account_first, account_second))
    coordinator, _, _ = _coordinator(broker)

    result = coordinator.reconcile()

    assert result.status is ReconciliationStatus.RECONCILED
    assert result.snapshot is not None
    assert result.snapshot.account.as_of == account_second.as_of


def test_account_observation_time_cannot_move_backward() -> None:
    account_second = AccountSummary(
        account_id=ACCOUNT_ID,
        net_liquidation=Decimal("250000"),
        total_cash=Decimal("125000"),
        buying_power=Decimal("240000"),
        as_of=NOW - timedelta(seconds=1),
    )
    account_first = account_second.model_copy(update={"as_of": NOW})
    broker = _ScriptedBroker(accounts=(account_first, account_second))
    coordinator, _, reader = _coordinator(broker)

    result = coordinator.reconcile()

    assert reader.calls == []
    assert result.status is ReconciliationStatus.PENDING
    assert result.snapshot is None
    assert any("moved backward" in reason for reason in result.reasons)


def test_incomplete_local_evidence_keeps_portfolio_pending() -> None:
    broker = _ScriptedBroker()
    coordinator, _, _ = _coordinator(
        broker,
        LocalReconciliationEvidence(complete=False),
    )

    result = coordinator.reconcile()

    assert result.status is ReconciliationStatus.PENDING
    assert result.symbols[0].status is ReconciliationStatus.PENDING
    assert result.symbols[0].opening_actions_locked is True
    assert any("incomplete" in reason for reason in result.reasons)


def test_position_change_during_double_read_is_pending() -> None:
    broker = _ScriptedBroker(positions=((), (_stock(),)))
    coordinator, _, _ = _coordinator(broker)

    result = coordinator.reconcile()

    assert result.status is ReconciliationStatus.PENDING
    assert result.snapshot is None
    assert any("changed" in reason for reason in result.reasons)


def test_market_only_position_change_does_not_destabilize_state_evidence() -> None:
    first = _stock(market_price="190")
    second = _stock(market_price="191")
    broker = _ScriptedBroker(positions=((first,), (second,)))
    coordinator, _, _ = _coordinator(
        broker,
        LocalReconciliationEvidence(
            complete=True,
            unresolved_symbols=frozenset({"AAPL"}),
        ),
    )

    result = coordinator.reconcile()

    assert result.status is ReconciliationStatus.MANUAL_REVIEW
    assert not any("changed" in reason for reason in result.reasons)
    assert result.snapshot is not None
    assert result.snapshot.positions[0].market_price == Decimal("191")


def test_account_mismatch_is_pending_and_raw_ids_are_not_serialized() -> None:
    broker = _ScriptedBroker(status_end_account_id=OTHER_ACCOUNT_ID)
    coordinator, _, _ = _coordinator(
        broker,
        LocalReconciliationEvidence(
            complete=True,
            reasons=(f"diagnostic mentions {ACCOUNT_ID}",),
        ),
    )

    result = coordinator.reconcile()
    serialized = result.model_dump_json()

    assert result.status is ReconciliationStatus.PENDING
    assert result.snapshot is None
    assert any("account scope" in reason for reason in result.reasons)
    assert ACCOUNT_ID not in serialized
    assert OTHER_ACCOUNT_ID not in serialized


def test_duplicate_position_evidence_is_pending() -> None:
    duplicate_positions = (_stock(), _stock())
    broker = _ScriptedBroker(positions=(duplicate_positions, duplicate_positions))
    coordinator, _, _ = _coordinator(broker)

    result = coordinator.reconcile()

    assert result.status is ReconciliationStatus.PENDING
    assert result.snapshot is None
    assert any("duplicate" in reason for reason in result.reasons)


def test_nonzero_position_requires_manual_provenance_review() -> None:
    positions = (_stock(),)
    broker = _ScriptedBroker(positions=(positions, positions))
    coordinator, _, _ = _coordinator(
        broker,
        LocalReconciliationEvidence(
            complete=True,
            unresolved_symbols=frozenset({"AAPL"}),
        ),
    )

    result = coordinator.reconcile()

    symbol = result.symbols[0]
    assert result.status is ReconciliationStatus.MANUAL_REVIEW
    assert symbol.status is ReconciliationStatus.MANUAL_REVIEW
    assert symbol.stage is WheelStage.MANUAL_REVIEW
    assert symbol.opening_actions_locked is True
    assert any("fill-allocation provenance" in reason for reason in symbol.reasons)


def test_exact_zero_fill_owned_put_can_reconcile_but_remains_action_locked() -> None:
    order = _put_order()
    orders = (order,)
    broker = _ScriptedBroker(orders=(orders, orders))
    coordinator, _, _ = _coordinator(
        broker,
        LocalReconciliationEvidence(
            active_order_identities=frozenset({order.identity}),
            complete=True,
        ),
    )

    result = coordinator.reconcile()

    symbol = result.symbols[0]
    assert result.status is ReconciliationStatus.RECONCILED
    assert symbol.status is ReconciliationStatus.RECONCILED
    assert symbol.stage is WheelStage.SHORT_PUT_PENDING
    assert symbol.pending_put_contracts == Decimal("1")
    assert symbol.opening_actions_locked is True
    assert result.opening_actions_locked is True
    assert ACCOUNT_ID not in result.model_dump_json()


def test_unresolved_owned_put_stays_manual_even_with_exact_broker_identity() -> None:
    order = _put_order()
    orders = (order,)
    broker = _ScriptedBroker(orders=(orders, orders))
    coordinator, _, _ = _coordinator(
        broker,
        LocalReconciliationEvidence(
            active_order_identities=frozenset({order.identity}),
            unresolved_symbols=frozenset({"AAPL"}),
            complete=True,
        ),
    )

    result = coordinator.reconcile()

    assert result.status is ReconciliationStatus.MANUAL_REVIEW
    assert result.symbols[0].stage is WheelStage.MANUAL_REVIEW
    assert any("not fully resolved" in reason for reason in result.symbols[0].reasons)


def test_execution_requires_manual_fill_allocation_review() -> None:
    execution = _execution()
    executions = (execution,)
    broker = _ScriptedBroker(executions=(executions, executions))
    coordinator, _, _ = _coordinator(
        broker,
        LocalReconciliationEvidence(
            complete=True,
            unresolved_symbols=frozenset({"AAPL"}),
        ),
    )

    result = coordinator.reconcile()

    assert result.status is ReconciliationStatus.MANUAL_REVIEW
    assert result.snapshot is not None
    assert result.snapshot.execution_count == 1
    assert any("fill allocation" in reason for reason in result.symbols[0].reasons)


def test_local_reader_failure_is_generic_pending_without_exception_disclosure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broker = _ScriptedBroker()
    coordinator, _, _ = _coordinator(
        broker,
        failure=RuntimeError(f"database failed for {ACCOUNT_ID}"),
    )

    with caplog.at_level(logging.WARNING, logger="chronos.services.reconciliation"):
        result = coordinator.reconcile()

    assert result.status is ReconciliationStatus.PENDING
    assert result.snapshot is not None
    assert ACCOUNT_ID not in result.model_dump_json()
    assert result.reasons == (
        "Local strategy evidence could not be read; reconciliation remains locked.",
    )
    assert ACCOUNT_ID not in caplog.text
    assert "database failed" not in caplog.text


def test_broker_read_failure_returns_generic_pending_without_snapshot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broker = _ScriptedBroker(
        server_time_failure=RuntimeError(f"disconnected from {ACCOUNT_ID}"),
    )
    coordinator, _, reader = _coordinator(broker)

    with caplog.at_level(logging.WARNING, logger="chronos.services.reconciliation"):
        result = coordinator.reconcile()

    assert result.status is ReconciliationStatus.PENDING
    assert result.snapshot is None
    assert reader.calls == []
    assert result.reasons == (
        "Broker evidence could not be captured; reconciliation remains locked.",
    )
    assert ACCOUNT_ID not in caplog.text
    assert "disconnected" not in caplog.text


def test_connection_manager_timeout_returns_typed_pending() -> None:
    broker = _ScriptedBroker()
    coordinator, runner, reader = _coordinator(broker)
    runner.failure = TimeoutError(f"timeout for {ACCOUNT_ID}")

    result = coordinator.reconcile()

    assert runner.run_calls == 1
    assert reader.calls == []
    assert result.status is ReconciliationStatus.PENDING
    assert result.snapshot is None
    assert ACCOUNT_ID not in result.model_dump_json()


def test_slow_real_elapsed_window_is_pending_even_with_bounded_server_time() -> None:
    broker = _ScriptedBroker()
    monotonic_values = iter((100.0, 131.0))
    coordinator, _, reader = _coordinator(
        broker,
        monotonic=lambda: next(monotonic_values),
        max_observation_seconds=30.0,
    )

    result = coordinator.reconcile()

    assert reader.calls == []
    assert result.status is ReconciliationStatus.PENDING
    assert result.snapshot is None
    assert any("real-time" in reason for reason in result.reasons)


@pytest.mark.parametrize("finished_at", (131.0, 99.0, float("nan"), float("inf")))
def test_invalid_end_to_end_evidence_window_is_pending(finished_at: float) -> None:
    broker = _ScriptedBroker()
    monotonic_values = iter((100.0, 101.0, finished_at))
    coordinator, _, reader = _coordinator(
        broker,
        monotonic=lambda: next(monotonic_values),
        max_observation_seconds=30.0,
    )

    result = coordinator.reconcile()

    assert reader.calls == [ACCOUNT_ID]
    assert result.status is ReconciliationStatus.PENDING
    assert result.snapshot is None
    assert any("real-time" in reason for reason in result.reasons)


def test_end_to_end_evidence_window_accepts_exact_boundary() -> None:
    broker = _ScriptedBroker()
    monotonic_values = iter((100.0, 101.0, 130.0))
    coordinator, _, _ = _coordinator(
        broker,
        monotonic=lambda: next(monotonic_values),
        max_observation_seconds=30.0,
    )

    result = coordinator.reconcile()

    assert result.status is ReconciliationStatus.RECONCILED
    assert result.snapshot is not None
    assert result.snapshot.window_seconds == 30.0


def test_slow_failed_local_read_cannot_publish_stale_broker_snapshot() -> None:
    broker = _ScriptedBroker()
    monotonic_values = iter((100.0, 101.0, 131.0))
    coordinator, _, reader = _coordinator(
        broker,
        failure=RuntimeError("local read failed"),
        monotonic=lambda: next(monotonic_values),
        max_observation_seconds=30.0,
    )

    result = coordinator.reconcile()

    assert reader.calls == [ACCOUNT_ID]
    assert result.status is ReconciliationStatus.PENDING
    assert result.snapshot is None
    assert any("real-time" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    ("status_start", "status_end"),
    (
        (
            _status(ACCOUNT_ID).model_copy(
                update={"state": ConnectionState.DISCONNECTED, "connected": False}
            ),
            _status(ACCOUNT_ID),
        ),
        (
            _status(ACCOUNT_ID),
            _status(ACCOUNT_ID).model_copy(update={"state": ConnectionState.DEGRADED}),
        ),
        (
            _status(ACCOUNT_ID),
            _status(ACCOUNT_ID).model_copy(update={"environment": DisplayEnvironment.LIVE}),
        ),
        (
            _status(ACCOUNT_ID),
            _status(ACCOUNT_ID).model_copy(update={"data_quality": DataQuality.DELAYED}),
        ),
    ),
)
def test_unhealthy_or_changed_connection_status_is_pending(
    status_start: ConnectionStatus,
    status_end: ConnectionStatus,
) -> None:
    broker = _ScriptedBroker()
    broker.statuses = (status_start, status_end)
    coordinator, _, reader = _coordinator(broker)

    result = coordinator.reconcile()

    assert reader.calls == []
    assert result.status is ReconciliationStatus.PENDING
    assert result.snapshot is None


@pytest.mark.parametrize(
    "server_times",
    (
        (NOW, NOW - timedelta(seconds=1)),
        (NOW, NOW + timedelta(seconds=31)),
        (NOW.replace(tzinfo=None), NOW),
    ),
)
def test_invalid_server_time_window_is_pending(
    server_times: tuple[datetime, datetime],
) -> None:
    broker = _ScriptedBroker()
    broker.server_times = server_times
    coordinator, _, reader = _coordinator(broker)

    result = coordinator.reconcile()

    assert reader.calls == []
    assert result.status is ReconciliationStatus.PENDING
    assert result.snapshot is None
    assert any("server time" in reason or "server-time" in reason for reason in result.reasons)


def test_wrong_account_position_evidence_is_pending() -> None:
    position = _stock(account_id=OTHER_ACCOUNT_ID)
    broker = _ScriptedBroker(positions=((position,), (position,)))
    coordinator, _, reader = _coordinator(broker)

    result = coordinator.reconcile()

    assert reader.calls == []
    assert result.status is ReconciliationStatus.PENDING
    assert result.snapshot is None
    assert any("consistent account scope" in reason for reason in result.reasons)


@pytest.mark.parametrize("evidence_kind", ("order", "execution"))
def test_order_or_execution_change_during_double_read_is_pending(
    evidence_kind: str,
) -> None:
    order = _put_order()
    execution = _execution()
    broker = _ScriptedBroker(
        orders=((), (order,)) if evidence_kind == "order" else ((), ()),
        executions=((), (execution,)) if evidence_kind == "execution" else ((), ()),
    )
    coordinator, _, reader = _coordinator(broker)

    result = coordinator.reconcile()

    assert reader.calls == []
    assert result.status is ReconciliationStatus.PENDING
    assert result.snapshot is None
    assert any("changed" in reason for reason in result.reasons)


@pytest.mark.parametrize("evidence_kind", ("order", "execution"))
def test_duplicate_order_or_execution_evidence_is_pending(evidence_kind: str) -> None:
    order = _put_order()
    execution = _execution()
    duplicate_orders = (order, order) if evidence_kind == "order" else ()
    duplicate_executions = (execution, execution) if evidence_kind == "execution" else ()
    broker = _ScriptedBroker(
        orders=(duplicate_orders, duplicate_orders),
        executions=(duplicate_executions, duplicate_executions),
    )
    coordinator, _, reader = _coordinator(broker)

    result = coordinator.reconcile()

    assert reader.calls == []
    assert result.status is ReconciliationStatus.PENDING
    assert result.snapshot is None
    assert any("duplicate" in reason for reason in result.reasons)


def test_conflicting_order_execution_contract_metadata_is_pending() -> None:
    order = _put_order()
    execution = _execution().model_copy(
        update={
            "contract": _put_contract().model_copy(update={"strike": Decimal("181")}),
        }
    )
    broker = _ScriptedBroker(
        orders=((order,), (order,)),
        executions=((execution,), (execution,)),
    )
    coordinator, _, reader = _coordinator(broker)

    result = coordinator.reconcile()

    assert reader.calls == []
    assert result.status is ReconciliationStatus.PENDING
    assert result.snapshot is None
    assert any("conflicting" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    ("order", "owned"),
    (
        (_put_order(), False),
        (
            _put_order(
                filled_quantity="0.5",
                lifecycle=OrderLifecycle.PARTIALLY_FILLED,
            ),
            True,
        ),
        (_put_order(right=OptionRight.CALL), True),
        (_put_order(side=OrderSide.BUY), True),
    ),
    ids=("unowned-put", "partial-fill", "opening-call", "buy-to-close"),
)
def test_non_exact_open_order_conditions_require_manual_review(
    order: BrokerOrder,
    owned: bool,
) -> None:
    orders = (order,)
    identities = frozenset({order.identity}) if owned else frozenset()
    broker = _ScriptedBroker(orders=(orders, orders))
    coordinator, _, _ = _coordinator(
        broker,
        LocalReconciliationEvidence(
            active_order_identities=identities,
            complete=True,
        ),
    )

    result = coordinator.reconcile()

    assert result.status is ReconciliationStatus.MANUAL_REVIEW
    assert result.snapshot is not None
    assert result.symbols[0].stage is WheelStage.MANUAL_REVIEW
    assert result.symbols[0].opening_actions_locked is True
