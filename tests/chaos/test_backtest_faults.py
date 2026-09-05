"""Fault injection through the full backtest engine.

Intent ids are deterministic (UUIDv5 of economic content), and until a fill
happens equity and cash stay exactly at the initial value, so every entry
intent a clean run would submit can be reconstructed offline. The fault runs
key their FaultPlan by those harvested ids.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from chronos.backtest.engine import BacktestConfig, BacktestResult, run_backtest
from chronos.control.halt import HaltStore
from chronos.execution.brokers.simulated import FaultPlan
from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.portfolio.sizer import PortfolioPolicy, convert_proposal
from chronos.risk.policy import RiskPolicy
from chronos.strategies.base import (
    PositionSide,
    ProposalDirection,
    StrategyContext,
    StrategyProposal,
)

INITIAL_CASH = 3000.0
N_BARS = 30


def make_series() -> BarSeries:
    """Small synthetic uptrend: close climbs 0.5/day, lows dip 1.0 below."""

    session = date(2024, 1, 1)  # a Monday
    bars: list[Bar] = []
    prev_close = 100.0
    for i in range(N_BARS):
        while session.weekday() >= 5:
            session += timedelta(days=1)
        close = 100.0 + 0.5 * i
        open_ = prev_close
        high = max(open_, close) + 1.0
        low = min(open_, close) - 1.0
        bars.append(
            Bar(
                symbol="SPY",
                source="synthetic",
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
        )
        prev_close = close
        session += timedelta(days=1)
    return BarSeries(symbol="SPY", interval=BarInterval.DAY_1, bars=tuple(bars))


class AlwaysEnterStrategy:
    """Proposes a long entry on every bar while flat; holds forever after."""

    strategy_id = "strat_a"
    version = "1"
    warmup_bars = 1

    def on_bar(self, context: StrategyContext) -> StrategyProposal | None:
        if context.position.side is not PositionSide.FLAT:
            return None
        return entry_proposal_for(context.bars[-1])


def entry_proposal_for(bar: Bar) -> StrategyProposal:
    return StrategyProposal(
        strategy_id=AlwaysEnterStrategy.strategy_id,
        strategy_version=AlwaysEnterStrategy.version,
        timestamp_utc=bar.timestamp_utc,
        symbol=bar.symbol,
        direction=ProposalDirection.ENTER_LONG,
        desired_exposure_fraction=0.5,
        stop_fraction=0.05,
        source_bar_ids=(bar.sequence_id,),
        reason="always enter",
    )


EXIT_ON = date(2024, 1, 10)


class EnterThenExitOnDateStrategy:
    """Enters once while flat, then exits on a fixed session date.

    The exit is keyed to a DATE rather than to ``bars_held`` on purpose. A
    partial fill changes how long the engine thinks the position has been held,
    so a hold-count exit would move the exit bar as well as the entry basis and
    the two effects could not be read apart.
    """

    strategy_id = AlwaysEnterStrategy.strategy_id
    version = AlwaysEnterStrategy.version
    warmup_bars = 1

    def on_bar(self, context: StrategyContext) -> StrategyProposal | None:
        bar = context.bars[-1]
        if context.position.side is PositionSide.FLAT:
            if bar.session_date >= EXIT_ON:
                return None
            return entry_proposal_for(bar)
        if bar.session_date < EXIT_ON:
            return None
        return StrategyProposal(
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            timestamp_utc=bar.timestamp_utc,
            symbol=bar.symbol,
            direction=ProposalDirection.EXIT_LONG,
            desired_exposure_fraction=0.0,
            stop_fraction=None,
            source_bar_ids=(bar.sequence_id,),
            reason="date exit",
        )


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


def config() -> BacktestConfig:
    return BacktestConfig(initial_cash_usd=INITIAL_CASH, slippage_bps_per_side=0.0)


def harvest_flat_entry_intent_ids(series: BarSeries) -> list[str]:
    """Entry intent ids the engine would submit while never having filled.

    While no fill has happened, equity == cash == initial exactly, and the
    reference price is the bar close, so the conversion the backtest performs
    is reproducible bar by bar.
    """

    policy = PortfolioPolicy(max_fraction_per_position=config().max_fraction_per_position)
    ids: list[str] = []
    for bar in series:
        conversion = convert_proposal(
            entry_proposal_for(bar),
            policy=policy,
            equity_usd=Decimal(str(INITIAL_CASH)),
            cash_usd=Decimal(str(INITIAL_CASH)),
            held_shares=0,
            reference_price=Decimal(str(bar.close)),
        )
        assert conversion.intent is not None
        ids.append(conversion.intent.intent_id)
    return ids


def run(
    tmp_path: Path,
    name: str,
    faults: FaultPlan | None,
    strategy: object | None = None,
) -> BacktestResult:
    halt_store = HaltStore(tmp_path / f"halt-{name}.json")
    halt_store.rearm("armed for backtest chaos")
    return run_backtest(
        series=make_series(),
        strategy=strategy if strategy is not None else AlwaysEnterStrategy(),
        risk_policy=permissive_policy(),
        config=config(),
        halt_store=halt_store,
        faults=faults,
    )


class TestRejectedEntries:
    def test_rejecting_every_entry_leaves_equity_untouched(self, tmp_path: Path) -> None:
        series = make_series()
        all_entry_ids = frozenset(harvest_flat_entry_intent_ids(series))
        faults = FaultPlan(reject_intent_ids=all_entry_ids)

        clean = run(tmp_path, "clean", None)
        faulted = run(tmp_path, "reject", faults)

        # The clean run actually trades (sanity that the faults matter)...
        assert any(value != INITIAL_CASH for value in clean.equity_curve)
        # ...while the rejected run never gains a position: no phantom shares,
        # no cash movement, equity pinned at the initial value on every bar.
        assert faulted.trades == ()
        assert faulted.equity_curve == (INITIAL_CASH,) * N_BARS
        assert faulted.final_equity == INITIAL_CASH
        # Broker rejection is not a risk rejection.
        assert faulted.risk_rejections == 0
        assert not faulted.data_quality_blocking

    def test_fault_runs_are_deterministic(self, tmp_path: Path) -> None:
        series = make_series()
        faults = FaultPlan(reject_intent_ids=frozenset(harvest_flat_entry_intent_ids(series)))
        first = run(tmp_path, "det-a", faults)
        second = run(tmp_path, "det-b", faults)
        assert first == second


class TestPartialFillEntries:
    def test_partial_fill_of_first_entry_completes_and_is_deterministic(
        self, tmp_path: Path
    ) -> None:
        series = make_series()
        first_entry_id = harvest_flat_entry_intent_ids(series)[0]
        faults = FaultPlan(partial_fill_intent_ids=frozenset({first_entry_id}))

        clean = run(tmp_path, "clean", None)
        faulted_one = run(tmp_path, "partial-a", faults)
        faulted_two = run(tmp_path, "partial-b", faults)

        # The run completes and stays deterministic under injected partials.
        assert faulted_one == faulted_two
        assert len(faulted_one.equity_curve) == N_BARS

        # The partial delays full deployment: on the first fill bar the
        # faulted run holds fewer shares than the clean run, so the curves
        # must diverge there while both end fully invested.
        assert faulted_one.equity_curve != clean.equity_curve
        assert faulted_one.equity_curve[-1] != INITIAL_CASH


class TestMultiFillEntryBasis:
    """A position filled in two parts is valued at the quantity-weighted average.

    Issue #187: the engine accumulated ``quantity`` from ``cumulative_filled``
    while overwriting ``entry_price`` with the newest fill, so a position built
    from more than one BUY fill was valued at its LAST fill rather than its
    average — and ``entry_date``/``bars_held`` likewise recorded the completing
    fill instead of the opening one.

    The arithmetic this pins, with slippage at zero so every figure is exact.
    The first entry intent is 14 shares at a limit of 100.10; the injected
    partial splits it 1/2:

        PARTIAL_FILL  2024-01-02   7 shares @ 100.00   commission 1.00
        FILLED        2024-01-03   7 shares @ 100.10   commission 1.00

        weighted entry = (7 * 100.00 + 7 * 100.10) / 14
                       = 1400.70 / 14
                       = 100.05          <- what the engine must record
                         100.10          <- what it recorded before the fix

    Exiting all 14 on 2024-01-11 at 103.50, with 1.00 exit commission against
    2.00 of accumulated entry commission:

        corrected   (103.50 - 100.05) * 14 - 1.00 - 2.00 = 45.30
        before fix  (103.50 - 100.10) * 14 - 1.00 - 2.00 = 44.60

    The 0.70 gap is ``(last_fill_price - weighted_average) * quantity``. Its
    sign follows the direction between the fills, so the defect is a bias and
    not a constant: this series rises, so the dearer fill was last, the basis
    was overstated, and the trade read worse than it was.

    This is not only a reporting error. ``net_pnl`` is written into
    ``realized_by_day``/``realized_by_week``, which feed ``AccountView`` and so
    the risk engine's daily and weekly loss limits and ``consecutive_losses`` —
    a mis-valued trade can change a later risk decision, and thus which trades
    happen at all.
    """

    def test_two_fill_entry_is_valued_at_the_weighted_average(self, tmp_path: Path) -> None:
        series = make_series()
        first_entry_id = harvest_flat_entry_intent_ids(series)[0]
        faults = FaultPlan(partial_fill_intent_ids=frozenset({first_entry_id}))

        result = run(tmp_path, "legged", faults, EnterThenExitOnDateStrategy())

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.quantity == 14

        # The basis is the weighted average of the two fills, not the last one.
        assert trade.entry_price == pytest.approx(100.05)
        assert trade.entry_price != pytest.approx(100.10)

        # The position opened on the first fill, not on the one that completed it.
        assert trade.entry_date == date(2024, 1, 2)
        assert trade.exit_date == date(2024, 1, 11)
        assert trade.exit_price == pytest.approx(103.50)

        # Commissions accumulate across both entry fills (this was already right).
        assert trade.entry_commission == pytest.approx(2.00)
        assert trade.exit_commission == pytest.approx(1.00)

        # ...so the realized PnL follows from the weighted basis.
        assert trade.net_pnl == pytest.approx(45.30)
        assert trade.net_pnl != pytest.approx(44.60)

        # bars_held counts from the opening fill and is not reset by the second.
        assert trade.bars_held == 7

    def test_single_fill_entry_is_unchanged_by_the_weighting(self, tmp_path: Path) -> None:
        """The control: with one fill, the weighted average IS that fill.

        Every strategy in the corpus takes exactly one BUY fill per position
        (``convert_proposal`` refuses ENTER_LONG while held, and no research
        call site injects partial fills), so this is the path the committed
        research results actually exercise. It must not move.
        """

        clean = run(tmp_path, "clean-basis", None, EnterThenExitOnDateStrategy())

        assert len(clean.trades) == 1
        trade = clean.trades[0]
        assert trade.entry_price == pytest.approx(100.00)
        assert trade.entry_date == date(2024, 1, 2)
        assert trade.bars_held == 7
        # One fill, so one entry commission (the legged run pays two) and the
        # basis is that fill: (103.50 - 100.00) * 14 - 1.00 - 1.00 = 47.00.
        assert trade.entry_commission == pytest.approx(1.00)
        assert trade.net_pnl == pytest.approx(47.00)
