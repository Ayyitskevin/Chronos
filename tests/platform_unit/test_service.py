"""Service-plane startup gate and decision cycle."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from chronos.auditlog.log import AuditLog, ChainState, verify_chain
from chronos.control.halt import HaltReason, HaltStore
from chronos.control.modes import TradingMode, resolve_mode_lock
from chronos.execution.engine import ExecutionEngine
from chronos.execution.intents import IntentStatus
from chronos.execution.ledger import MemoryLedger
from chronos.execution.reconciliation import OrderStateView
from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.notifications.notifier import SafeNotifierFanout
from chronos.portfolio.sizer import PortfolioPolicy
from chronos.risk.engine import RiskEngine
from chronos.risk.policy import RiskPolicy
from chronos.service.cycle import ServiceCycle
from chronos.service.ports import (
    BrokerEvidence,
    NullBrokerEvidence,
    NullExecutionBroker,
    ReplayDataSource,
)
from chronos.service.runtime import ServiceComponents
from chronos.service.startup import run_startup
from chronos.strategies.base import (
    PositionSide,
    ProposalDirection,
    StrategyContext,
    StrategyProposal,
)

NOW = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)


def make_series(symbol: str, n: int = 60) -> BarSeries:
    bars = []
    session = date(2020, 1, 6)
    price = 100.0
    for _i in range(n):
        while session.weekday() >= 5:
            session += timedelta(days=1)
        price += 0.25
        bars.append(
            Bar(
                symbol=symbol,
                source="synthetic",
                exchange="TEST",
                interval=BarInterval.DAY_1,
                session_date=session,
                timestamp_utc=datetime.combine(session, time(21, 0), tzinfo=UTC),
                open=price,
                high=price + 0.5,
                low=price - 0.5,
                close=price,
                volume=1000.0,
            )
        )
        session += timedelta(days=1)
    return BarSeries(symbol=symbol, interval=BarInterval.DAY_1, bars=tuple(bars))


class AlwaysEnter:
    strategy_id = "always_enter"
    version = "1"
    warmup_bars = 1

    def on_bar(self, context: StrategyContext) -> StrategyProposal | None:
        if context.position.side is not PositionSide.FLAT:
            return None
        bar = context.decision_bar
        return StrategyProposal(
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            timestamp_utc=bar.timestamp_utc,
            symbol=bar.symbol,
            direction=ProposalDirection.ENTER_LONG,
            desired_exposure_fraction=0.9,
            stop_fraction=0.05,
            source_bar_ids=(bar.sequence_id,),
            reason="always",
        )


def shadow_policy() -> RiskPolicy:
    return RiskPolicy.model_validate(
        {
            "policy_version": "svc-test",
            "allowed_symbols": ("SPY",),
            "allowed_strategy_ids": ("always_enter",),
            "allow_long_entries": True,
            "max_bot_capital_usd": 3000,
            "max_position_notional_usd": 3000,
            "max_aggregate_exposure_usd": 3000,
            "max_symbol_exposure_fraction": 1.0,
            "max_risk_per_trade_fraction": 0.5,
            "max_simultaneous_positions": 1,
            "max_open_orders": 1,
            "max_daily_loss_usd": 3000,
            "max_weekly_loss_usd": 3000,
            "max_drawdown_fraction": 0.9,
            "max_consecutive_losses": 10,
            "max_quote_age_seconds": 10 * 86400,
            "max_bar_age_seconds": 10 * 86400,
            "max_price_deviation_fraction": 0.05,
            "max_order_rejections_per_day": 10,
            "allow_overnight_positions": True,
        }
    )


def build_components(
    tmp_path: Path,
    *,
    mode: TradingMode = TradingMode.SHADOW,
    evidence: object | None = None,
    ledger: MemoryLedger | None = None,
) -> ServiceComponents:
    mode_lock = resolve_mode_lock(
        requested_mode=mode,
        paper_account_allowlist=(),
        broker_reported_account_id=None,
        broker_reported_environment_is_paper=None,
        order_transmission_enabled=False,
    )
    risk_engine = RiskEngine(shadow_policy())
    used_ledger = ledger if ledger is not None else MemoryLedger()
    store = HaltStore(tmp_path / "halt.json")
    store.rearm("armed for service test")
    engine = ExecutionEngine(
        broker=NullExecutionBroker(),
        risk_engine=risk_engine,
        ledger=used_ledger,
        halt_store=store,
        mode_lock=mode_lock,
        reconciliation_passed=False,
    )
    data = ReplayDataSource()
    data.add(make_series("SPY"))
    return ServiceComponents(
        mode_lock=mode_lock,
        risk_engine=risk_engine,
        engine=engine,
        ledger=used_ledger,
        halt_store=store,
        audit_log=AuditLog(tmp_path / "audit.jsonl"),
        notifier=SafeNotifierFanout(notifiers=()),
        data_source=data,
        evidence_source=evidence if evidence is not None else NullBrokerEvidence(),
        portfolio_policy=PortfolioPolicy(max_fraction_per_position=0.95),
        strategies={"always_enter": AlwaysEnter()},
        symbols=("SPY",),
        equity_usd=3000.0,
    )


class TestStartup:
    def test_clean_startup_permits_orders(self, tmp_path: Path) -> None:
        c = build_components(tmp_path)
        report = run_startup(c, now_utc=NOW)
        assert not report.halted
        assert report.reconciliation_passed
        assert c.engine.reconciliation_passed

    def test_startup_while_halted_stays_halted(self, tmp_path: Path) -> None:
        c = build_components(tmp_path)
        c.halt_store.halt(HaltReason.OPERATOR_REQUEST, "manual")
        report = run_startup(c, now_utc=NOW)
        assert report.halted
        assert not report.reconciliation_passed
        assert not c.engine.reconciliation_passed

    def test_disconnected_broker_halts(self, tmp_path: Path) -> None:
        class Disconnected:
            def gather(self) -> BrokerEvidence:
                return BrokerEvidence(False, (), {}, {})

        c = build_components(tmp_path, evidence=Disconnected())
        report = run_startup(c, now_utc=NOW)
        assert report.halted
        assert c.halt_store.read().reason is HaltReason.CONNECTION_UNSTABLE

    def test_state_mismatch_halts_and_blocks(self, tmp_path: Path) -> None:
        ledger = MemoryLedger()
        # Ledger believes an order is partially filled at 3.
        from tests.platform_unit.test_hydration import make_intent

        intent = make_intent()
        ledger.record_intent(intent, IntentStatus.PENDING_SUBMISSION)
        ledger.record_transition(intent.intent_id, IntentStatus.SUBMITTED, NOW, "s")
        ledger.record_transition(intent.intent_id, IntentStatus.PARTIALLY_FILLED, NOW, "pf")
        ledger.record_fill(intent.intent_id, 3, Decimal("500"), Decimal("1"), NOW)

        class BrokerSaysFilled:
            def gather(self) -> BrokerEvidence:
                return BrokerEvidence(
                    connected=True,
                    open_order_refs=(intent.intent_id,),
                    positions={},
                    order_states={intent.intent_id: OrderStateView(IntentStatus.FILLED, 5)},
                )

        c = build_components(tmp_path, evidence=BrokerSaysFilled(), ledger=ledger)
        report = run_startup(c, now_utc=NOW)
        assert report.halted
        assert not c.engine.reconciliation_passed
        assert c.halt_store.read().reason is HaltReason.RECONCILIATION_MISMATCH


class TestCycleShadow:
    def test_shadow_produces_intents_but_cannot_submit(self, tmp_path: Path) -> None:
        c = build_components(tmp_path)
        run_startup(c, now_utc=NOW)
        report = ServiceCycle(c).run_once(now_utc=NOW)
        assert report.ran
        spy = [d for d in report.decisions if d.symbol == "SPY"]
        assert spy and spy[0].proposal == ProposalDirection.ENTER_LONG.value
        assert "SELL" not in spy[0].intent  # it's a BUY intent
        # SHADOW: mode forbids orders, so risk denies and nothing submits.
        assert spy[0].risk_approved is False
        assert "MODE_FORBIDS_ORDERS" in spy[0].risk_codes
        assert c.engine.working_intent_ids() == ()  # nothing submitted

    def test_shadow_never_calls_broker_submit(self, tmp_path: Path) -> None:
        # NullExecutionBroker.submit raises if ever called.
        c = build_components(tmp_path)
        run_startup(c, now_utc=NOW)
        ServiceCycle(c).run_once(now_utc=NOW)  # must not raise

    def test_cycle_blocked_when_halted(self, tmp_path: Path) -> None:
        c = build_components(tmp_path)
        run_startup(c, now_utc=NOW)
        c.halt_store.halt(HaltReason.DAILY_LOSS_LIMIT, "limit")
        report = ServiceCycle(c).run_once(now_utc=NOW)
        assert not report.ran
        assert report.halted

    def test_cycle_deterministic(self, tmp_path: Path) -> None:
        c1 = build_components(tmp_path / "a")
        run_startup(c1, now_utc=NOW)
        r1 = ServiceCycle(c1).run_once(now_utc=NOW)
        c2 = build_components(tmp_path / "b")
        run_startup(c2, now_utc=NOW)
        r2 = ServiceCycle(c2).run_once(now_utc=NOW)
        assert r1.decisions == r2.decisions

    def test_audit_chain_intact_after_cycle(self, tmp_path: Path) -> None:
        c = build_components(tmp_path)
        run_startup(c, now_utc=NOW)
        ServiceCycle(c).run_once(now_utc=NOW)
        assert verify_chain(tmp_path / "audit.jsonl").state is ChainState.VALID
