"""Deterministic single-symbol backtest engine (ADR-0005, ADR-0006 fill model).

Loop per closed bar t:

1. Working orders from bar t-1 are evaluated against bar t by the simulated
   broker (limit fills at min/max(open, limit)); resulting events update the
   account.
2. If a position carries a protective stop and bar t traded through it, an
   exit is generated through the normal proposal -> risk -> execution path at
   the stop price (modelling a resting stop-limit).
3. The strategy sees bars [0..t] and may emit a proposal; the portfolio layer
   sizes it; the risk engine validates it; the execution engine submits it.
   The order can only fill on bar t+1 or later. No same-bar entry+exit exists.

Costs: commission per share with a minimum per order (IBKR Pro fixed tier
assumption, ASSUMPTIONS.md A-20) plus configurable per-side slippage in basis
points applied to fill prices.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from chronos.control.halt import HaltStore
from chronos.control.modes import TradingMode, resolve_mode_lock
from chronos.domain.enums import OrderSide
from chronos.execution.brokers.port import BrokerEventKind
from chronos.execution.brokers.simulated import FaultPlan, SimulatedExecutionBroker
from chronos.execution.engine import ExecutionEngine
from chronos.execution.intents import OrderIntent
from chronos.execution.ledger import MemoryLedger
from chronos.marketdata.bars import BarSeries
from chronos.marketdata.quality import validate_series
from chronos.portfolio.sizer import PortfolioPolicy, convert_proposal
from chronos.risk.engine import AccountView, MarketViewEntry, RiskEngine
from chronos.risk.policy import RiskPolicy
from chronos.strategies.base import (
    PositionSide,
    PositionState,
    ProposalDirection,
    Strategy,
    StrategyContext,
    StrategyProposal,
)


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    symbol: str
    entry_date: date
    exit_date: date
    quantity: int
    entry_price: float
    exit_price: float
    entry_commission: float
    exit_commission: float
    net_pnl: float
    exit_reason: str
    bars_held: int


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_cash_usd: float
    slippage_bps_per_side: float
    max_fraction_per_position: float = 0.95
    commission_per_share: float = 0.005
    commission_minimum: float = 1.00
    use_adjusted_close: bool = False


@dataclass(frozen=True, slots=True)
class BacktestResult:
    strategy_id: str
    strategy_version: str
    symbol: str
    equity_curve_dates: tuple[date, ...]
    equity_curve: tuple[float, ...]
    trades: tuple[ClosedTrade, ...]
    risk_rejections: int
    skipped_conversions: int
    data_quality_blocking: bool
    final_equity: float


@dataclass
class _Position:
    quantity: int = 0
    entry_price: float = 0.0
    entry_commission: float = 0.0
    entry_date: date | None = None
    stop_price: float | None = None
    bars_held: int = 0


def run_backtest(
    *,
    series: BarSeries,
    strategy: Strategy,
    risk_policy: RiskPolicy,
    config: BacktestConfig,
    halt_store: HaltStore,
    faults: FaultPlan | None = None,
) -> BacktestResult:
    quality = validate_series(series)
    mode_lock = resolve_mode_lock(
        requested_mode=TradingMode.BACKTEST,
        paper_account_allowlist=(),
        broker_reported_account_id=None,
        broker_reported_environment_is_paper=None,
        order_transmission_enabled=False,
    )
    broker = SimulatedExecutionBroker(
        commission_per_share=Decimal(str(config.commission_per_share)),
        commission_minimum=Decimal(str(config.commission_minimum)),
        faults=faults or FaultPlan(),
    )
    ledger = MemoryLedger()
    risk_engine = RiskEngine(risk_policy)
    engine = ExecutionEngine(
        broker=broker,
        risk_engine=risk_engine,
        ledger=ledger,
        halt_store=halt_store,
        mode_lock=mode_lock,
        reconciliation_passed=True,  # simulated broker state is local by definition
    )
    portfolio_policy = PortfolioPolicy(max_fraction_per_position=config.max_fraction_per_position)

    cash = config.initial_cash_usd
    position = _Position()
    pending_side: dict[str, OrderSide] = {}
    pending_stop: dict[str, float | None] = {}
    pending_exit_reason: dict[str, str] = {}
    trades: list[ClosedTrade] = []
    equity_dates: list[date] = []
    equity_curve: list[float] = []
    realized_by_day: dict[date, float] = {}
    realized_by_week: dict[tuple[int, int], float] = {}
    peak_equity = config.initial_cash_usd
    consecutive_losses = 0
    risk_rejections = 0
    skipped = 0

    slip = config.slippage_bps_per_side / 10_000.0

    def price_of(index: int) -> float:
        bar = series[index]
        if config.use_adjusted_close and bar.adjusted_close is not None:
            return bar.adjusted_close
        return bar.close

    for index in range(len(series)):
        bar = series[index]
        now = bar.timestamp_utc

        # 1. Evaluate working orders against this bar.
        broker.advance_bar(bar)
        for event in engine.pump_events(now_utc=now):
            if event.kind not in (BrokerEventKind.FILLED, BrokerEventKind.PARTIAL_FILL):
                continue
            side = pending_side.get(event.intent_id)
            if side is None or event.average_fill_price is None:
                continue
            fill_price = float(event.average_fill_price)
            commission = float(event.commission_usd or 0)
            if side is OrderSide.BUY:
                effective = fill_price * (1.0 + slip)
                # The sim reports cumulative fills; incremental cash movement
                # is reconstructed from the position delta.
                increment = event.cumulative_filled - position.quantity
                cash -= effective * increment + commission
                if position.quantity == 0:
                    # Opening from flat: this fill is the whole basis.
                    position.entry_price = effective
                    position.entry_date = bar.session_date
                    position.bars_held = 0
                elif increment > 0:
                    # Adding to an open position (a partial fill completing, or
                    # a second leg): the basis is the quantity-weighted average,
                    # not the newest fill. `entry_date` keeps the date the
                    # position opened and `bars_held` keeps counting from it.
                    prior_notional = position.entry_price * position.quantity
                    position.entry_price = (prior_notional + effective * increment) / (
                        position.quantity + increment
                    )
                position.quantity = event.cumulative_filled
                position.entry_commission += commission
                position.stop_price = pending_stop.get(event.intent_id)
            else:
                effective = fill_price * (1.0 - slip)
                increment = min(event.cumulative_filled, position.quantity)
                cash += effective * increment - commission
                if event.kind is BrokerEventKind.FILLED and position.entry_date is not None:
                    pnl = (
                        (effective - position.entry_price) * increment
                        - commission
                        - position.entry_commission
                    )
                    trades.append(
                        ClosedTrade(
                            symbol=bar.symbol,
                            entry_date=position.entry_date,
                            exit_date=bar.session_date,
                            quantity=increment,
                            entry_price=position.entry_price,
                            exit_price=effective,
                            entry_commission=position.entry_commission,
                            exit_commission=commission,
                            net_pnl=pnl,
                            exit_reason=pending_exit_reason.get(event.intent_id, "signal"),
                            bars_held=position.bars_held,
                        )
                    )
                    realized_by_day[bar.session_date] = (
                        realized_by_day.get(bar.session_date, 0.0) + pnl
                    )
                    iso = bar.session_date.isocalendar()
                    week_key = (iso.year, iso.week)
                    realized_by_week[week_key] = realized_by_week.get(week_key, 0.0) + pnl
                    consecutive_losses = consecutive_losses + 1 if pnl < 0 else 0
                    position = _Position()

        # 2. Mark to market.
        mark = price_of(index)
        equity = cash + position.quantity * mark
        peak_equity = max(peak_equity, equity)
        equity_dates.append(bar.session_date)
        equity_curve.append(equity)
        if position.quantity > 0:
            position.bars_held += 1

        # 3. Protective stop: if this bar traded through the stop, exit next bar.
        stop_hit = (
            position.quantity > 0
            and position.stop_price is not None
            and bar.low <= position.stop_price
        )

        # 4. Strategy decision on the closed bar.
        context = StrategyContext(
            bars=series.slice_until(index + 1),
            position=(
                PositionState(
                    side=PositionSide.LONG if position.quantity > 0 else PositionSide.FLAT,
                    bars_held=position.bars_held,
                    entry_price=position.entry_price if position.quantity > 0 else None,
                )
            ),
        )
        proposal = strategy.on_bar(context)

        intent: OrderIntent | None = None
        exit_reason = "signal"
        if stop_hit and position.quantity > 0:
            # Stop exit takes precedence over strategy HOLD; modelled as a
            # marketable limit at the stop price.
            stop_proposal = StrategyProposal(
                strategy_id=strategy.strategy_id,
                strategy_version=strategy.version,
                timestamp_utc=now,
                symbol=bar.symbol,
                direction=ProposalDirection.EXIT_LONG,
                desired_exposure_fraction=0.0,
                stop_fraction=None,
                evidence={"stop_price": float(position.stop_price or 0.0)},
                source_bar_ids=(bar.sequence_id,),
                reason="protective stop traded through",
            )
            proposal = stop_proposal
            exit_reason = "stop"

        if proposal is not None:
            conversion = convert_proposal(
                proposal,
                policy=portfolio_policy,
                equity_usd=Decimal(str(equity)),
                cash_usd=Decimal(str(cash)),
                held_shares=position.quantity,
                reference_price=Decimal(str(mark)),
            )
            if conversion.intent is None:
                skipped += 1
            else:
                intent = conversion.intent

        if intent is not None:
            account_view = AccountView(
                account_equity_usd=equity,
                cash_usd=cash,
                position_shares={bar.symbol: position.quantity} if position.quantity else {},
                position_notional_usd=(
                    {bar.symbol: position.quantity * mark} if position.quantity else {}
                ),
                open_order_count=len(engine.working_intent_ids()),
                realized_pnl_today_usd=realized_by_day.get(bar.session_date, 0.0),
                realized_pnl_week_usd=realized_by_week.get(
                    (bar.session_date.isocalendar().year, bar.session_date.isocalendar().week),
                    0.0,
                ),
                peak_equity_usd=peak_equity,
                consecutive_losses=consecutive_losses,
                as_of_utc=now,
            )
            market_view = MarketViewEntry(last_price=mark, bar_close_utc=now, quote_utc=now)
            decision = risk_engine.validate(
                intent,
                account=account_view,
                market=market_view,
                halt=halt_store.read(),
                mode_lock=mode_lock,
                now_utc=now,
            )
            if not decision.approved:
                risk_rejections += 1
            else:
                result = engine.submit_approved(intent, decision.approval, now_utc=now)
                if result.submitted:
                    pending_side[intent.intent_id] = intent.side
                    pending_stop[intent.intent_id] = (
                        float(intent.stop_price) if intent.stop_price is not None else None
                    )
                    pending_exit_reason[intent.intent_id] = exit_reason

    return BacktestResult(
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version,
        symbol=series.symbol,
        equity_curve_dates=tuple(equity_dates),
        equity_curve=tuple(equity_curve),
        trades=tuple(trades),
        risk_rejections=risk_rejections,
        skipped_conversions=skipped,
        data_quality_blocking=quality.blocking,
        final_equity=equity_curve[-1] if equity_curve else config.initial_cash_usd,
    )
