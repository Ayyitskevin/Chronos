"""Fault injection through the real execution pipeline.

Each test wires the actual components — simulated broker, risk engine with a
permissive policy, memory ledger, persistent halt store, backtest mode lock,
execution engine — and injects one failure mode, asserting the pipeline ends
in the correct state (terminal order status, halt, refusal) without leaking
exceptions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

from chronos.control.halt import HaltReason, HaltStore
from chronos.control.modes import ModeLock, TradingMode, resolve_mode_lock
from chronos.domain.enums import OrderSide
from chronos.execution.brokers.simulated import FaultPlan, SimulatedExecutionBroker
from chronos.execution.engine import ExecutionEngine, SubmissionRefused, SubmissionResult
from chronos.execution.intents import IntentStatus, OrderIntent, TimeInForce
from chronos.execution.ledger import MemoryLedger
from chronos.marketdata.bars import Bar, BarInterval
from chronos.risk.engine import AccountView, MarketViewEntry, RiskEngine
from chronos.risk.policy import RiskPolicy

NOW = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)


def make_intent(**overrides: object) -> OrderIntent:
    payload: dict[str, object] = {
        "strategy_id": "strat_a",
        "strategy_version": "1",
        "symbol": "SPY",
        "side": OrderSide.BUY,
        "quantity": 5,
        "limit_price": Decimal("500.10"),
        "stop_price": Decimal("485.00"),
        "time_in_force": TimeInForce.DAY,
        "decision_timestamp_utc": NOW,
        "source_bar_sequence_id": "test:SPY:1d:2024-06-03",
        "proposal_reason": "test",
    }
    payload.update(overrides)
    return OrderIntent(**payload)  # type: ignore[arg-type]


def permissive_policy() -> RiskPolicy:
    return RiskPolicy.model_validate(
        {
            "policy_version": "test",
            "allowed_symbols": ("SPY",),
            "allowed_strategy_ids": ("strat_a",),
            "allow_long_entries": True,
            "max_bot_capital_usd": 3000,
            "max_position_notional_usd": 3000,
            "max_aggregate_exposure_usd": 3000,
            "max_symbol_exposure_fraction": 1.0,
            "max_risk_per_trade_fraction": 0.10,
            "max_simultaneous_positions": 1,
            "max_open_orders": 2,
            "max_daily_loss_usd": 300,
            "max_weekly_loss_usd": 600,
            "max_drawdown_fraction": 0.5,
            "max_consecutive_losses": 10,
            "max_quote_age_seconds": 300,
            "max_bar_age_seconds": 5 * 86400,
            "max_price_deviation_fraction": 0.05,
            "allow_overnight_positions": True,
        }
    )


def healthy_account() -> AccountView:
    return AccountView(
        account_equity_usd=3000.0,
        cash_usd=3000.0,
        position_shares={},
        position_notional_usd={},
        open_order_count=0,
        realized_pnl_today_usd=0.0,
        realized_pnl_week_usd=0.0,
        peak_equity_usd=3000.0,
        consecutive_losses=0,
        as_of_utc=NOW,
    )


def backtest_lock() -> ModeLock:
    return resolve_mode_lock(
        requested_mode=TradingMode.BACKTEST,
        paper_account_allowlist=(),
        broker_reported_account_id=None,
        broker_reported_environment_is_paper=None,
        order_transmission_enabled=False,
    )


def make_bar(
    session: date = date(2024, 6, 4),
    open_: float = 505.0,
    high: float = 506.0,
    low: float = 495.0,
    close: float = 500.0,
) -> Bar:
    return Bar(
        symbol="SPY",
        source="test",
        exchange="ARCA",
        interval=BarInterval.DAY_1,
        session_date=session,
        timestamp_utc=datetime.combine(session, time(21, 0), tzinfo=UTC),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1_000_000.0,
    )


class Pipeline:
    """One fully wired execution pipeline over the simulated broker."""

    def __init__(self, tmp_path: Path, faults: FaultPlan | None = None) -> None:
        self.broker = SimulatedExecutionBroker(faults=faults or FaultPlan())
        self.risk_engine = RiskEngine(permissive_policy())
        self.ledger = MemoryLedger()
        self.halt_path = tmp_path / "halt.json"
        self.halt_store = HaltStore(self.halt_path)
        self.halt_store.rearm("armed for chaos test")
        self.engine = ExecutionEngine(
            broker=self.broker,
            risk_engine=self.risk_engine,
            ledger=self.ledger,
            halt_store=self.halt_store,
            mode_lock=backtest_lock(),
            reconciliation_passed=True,
        )

    def approve(self, intent: OrderIntent) -> object:
        decision = self.risk_engine.validate(
            intent,
            account=healthy_account(),
            market=MarketViewEntry(last_price=500.0, bar_close_utc=NOW, quote_utc=NOW),
            halt=self.halt_store.read(),
            mode_lock=self.engine.mode_lock,
            now_utc=NOW,
        )
        assert decision.approved, decision.explanations
        return decision.approval

    def submit(self, intent: OrderIntent) -> SubmissionResult:
        return self.engine.submit_approved(intent, self.approve(intent), now_utc=NOW)  # type: ignore[arg-type]

    def pump(self) -> tuple[object, ...]:
        return self.engine.pump_events(now_utc=NOW)


class TestBrokerRejection:
    def test_injected_rejection_terminates_without_halt(self, tmp_path: Path) -> None:
        intent = make_intent()
        pipeline = Pipeline(
            tmp_path, faults=FaultPlan(reject_intent_ids=frozenset({intent.intent_id}))
        )
        result = pipeline.submit(intent)
        assert result.submitted
        pipeline.pump()

        assert pipeline.engine.order_status(intent.intent_id) is IntentStatus.REJECTED
        assert not pipeline.halt_store.read().halted
        # The terminal status reached the ledger.
        assert pipeline.ledger.statuses[intent.intent_id] is IntentStatus.REJECTED


class TestPartialFillProgression:
    def test_partial_then_full_fill_with_cumulative_quantities(self, tmp_path: Path) -> None:
        intent = make_intent(quantity=5)
        pipeline = Pipeline(
            tmp_path, faults=FaultPlan(partial_fill_intent_ids=frozenset({intent.intent_id}))
        )
        pipeline.submit(intent)
        pipeline.pump()  # apply the acknowledgement
        assert pipeline.engine.order_status(intent.intent_id) is IntentStatus.ACKNOWLEDGED

        pipeline.broker.advance_bar(make_bar(session=date(2024, 6, 4)))
        pipeline.pump()
        assert pipeline.engine.order_status(intent.intent_id) is IntentStatus.PARTIALLY_FILLED
        assert pipeline.engine._orders[intent.intent_id].filled_quantity == 2

        pipeline.broker.advance_bar(make_bar(session=date(2024, 6, 5)))
        pipeline.pump()
        assert pipeline.engine.order_status(intent.intent_id) is IntentStatus.FILLED
        assert pipeline.engine._orders[intent.intent_id].filled_quantity == 5

        # Ledger saw both fills with increasing cumulative quantity.
        assert [f.cumulative_quantity for f in pipeline.ledger.fills] == [2, 5]
        assert not pipeline.halt_store.read().halted


class TestDuplicateEvents:
    def test_duplicate_events_are_applied_once(self, tmp_path: Path) -> None:
        intent = make_intent(quantity=5)
        pipeline = Pipeline(
            tmp_path,
            faults=FaultPlan(
                duplicate_events_intent_ids=frozenset({intent.intent_id}),
                partial_fill_intent_ids=frozenset({intent.intent_id}),
            ),
        )
        pipeline.submit(intent)
        events = pipeline.pump()
        assert len(events) == 2  # duplicated ACK arrived twice...
        acked = [t for t in pipeline.ledger.transitions if t.status is IntentStatus.ACKNOWLEDGED]
        assert len(acked) == 1  # ...but was applied and recorded once

        pipeline.broker.advance_bar(make_bar(session=date(2024, 6, 4)))
        pipeline.pump()  # duplicated PARTIAL_FILL (same cumulative) applied once
        partials = [
            t for t in pipeline.ledger.transitions if t.status is IntentStatus.PARTIALLY_FILLED
        ]
        assert len(partials) == 1
        assert [f.cumulative_quantity for f in pipeline.ledger.fills] == [2]

        pipeline.broker.advance_bar(make_bar(session=date(2024, 6, 5)))
        pipeline.pump()  # duplicated FILLED applied once
        filled = [t for t in pipeline.ledger.transitions if t.status is IntentStatus.FILLED]
        assert len(filled) == 1
        assert [f.cumulative_quantity for f in pipeline.ledger.fills] == [2, 5]

        # Idempotent duplicates must not be treated as contradictions.
        assert not pipeline.halt_store.read().halted
        assert pipeline.engine.order_status(intent.intent_id) is IntentStatus.FILLED


class TestDroppedAcknowledgement:
    def test_fill_after_dropped_ack_is_legal(self, tmp_path: Path) -> None:
        intent = make_intent()
        pipeline = Pipeline(
            tmp_path, faults=FaultPlan(drop_ack_intent_ids=frozenset({intent.intent_id}))
        )
        pipeline.submit(intent)
        assert pipeline.pump() == ()  # silent acceptance: no events at all
        assert pipeline.engine.order_status(intent.intent_id) is IntentStatus.SUBMITTED

        pipeline.broker.advance_bar(make_bar())
        pipeline.pump()
        # SUBMITTED -> FILLED without an intervening ACKNOWLEDGED is legal.
        assert pipeline.engine.order_status(intent.intent_id) is IntentStatus.FILLED
        assert not pipeline.halt_store.read().halted


class TestLedgerFailureMidRun:
    def test_ledger_outage_refuses_and_halts_persistently(self, tmp_path: Path) -> None:
        pipeline = Pipeline(tmp_path)
        first = make_intent()
        # Different economic content (a later decision bar) -> different id.
        second = make_intent(source_bar_sequence_id="test:SPY:1d:2024-06-04")

        result_one = pipeline.submit(first)
        assert result_one.submitted

        # Approve BEFORE breaking the ledger: the risk decision is legitimate,
        # the outage strikes between approval and submission.
        approval = pipeline.approve(second)
        pipeline.ledger.fail_writes = True
        result_two = pipeline.engine.submit_approved(second, approval, now_utc=NOW)  # type: ignore[arg-type]
        assert not result_two.submitted
        assert result_two.refusal is SubmissionRefused.LEDGER_UNAVAILABLE

        # The halt is durable: a brand-new store on the same path sees it.
        state = HaltStore(pipeline.halt_path).read()
        assert state.halted
        assert state.reason is HaltReason.STORAGE_FAILURE


class TestUnknownOrderEvent:
    def test_event_for_unknown_intent_halts(self, tmp_path: Path) -> None:
        pipeline = Pipeline(tmp_path)
        later = make_intent(source_bar_sequence_id="test:SPY:1d:2024-06-05")
        approval = pipeline.approve(later)  # minted before the incident

        # Hand the broker an order the engine never submitted; its ACK is
        # evidence about an intent the engine cannot account for.
        rogue = make_intent(source_bar_sequence_id="test:SPY:1d:2024-06-04")
        pipeline.broker.submit(rogue)
        pipeline.pump()

        state = pipeline.halt_store.read()
        assert state.halted
        assert state.reason is HaltReason.UNKNOWN_ORDER
        # The halt blocks even a properly approved later submission.
        blocked = pipeline.engine.submit_approved(later, approval, now_utc=NOW)  # type: ignore[arg-type]
        assert not blocked.submitted
        assert blocked.refusal is SubmissionRefused.HALTED
