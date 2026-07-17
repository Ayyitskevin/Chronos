"""Hand-computed performance metrics on a tiny synthetic BacktestResult."""

from __future__ import annotations

from datetime import date

import pytest

from chronos.backtest.engine import BacktestResult, ClosedTrade
from chronos.backtest.metrics import compute_metrics

# Equity curve 100 -> 110 -> 99 -> 105 across a year boundary:
#   2024-12-30 (Mon), 2024-12-31 (Tue), 2025-01-02 (Thu), 2025-01-03 (Fri)
DATES = (date(2024, 12, 30), date(2024, 12, 31), date(2025, 1, 2), date(2025, 1, 3))
CURVE = (100.0, 110.0, 99.0, 105.0)


def trade(net_pnl: float, *, quantity: int, exit_price: float, bars_held: int) -> ClosedTrade:
    return ClosedTrade(
        symbol="SPY",
        entry_date=date(2024, 12, 30),
        exit_date=date(2025, 1, 3),
        quantity=quantity,
        entry_price=100.0,
        exit_price=exit_price,
        entry_commission=1.0,
        exit_commission=1.0,
        net_pnl=net_pnl,
        exit_reason="signal",
        bars_held=bars_held,
    )


TRADES = (
    trade(100.0, quantity=10, exit_price=110.0, bars_held=2),
    trade(50.0, quantity=5, exit_price=110.0, bars_held=1),
    trade(-50.0, quantity=5, exit_price=90.0, bars_held=1),
)


def make_result() -> BacktestResult:
    return BacktestResult(
        strategy_id="strat_a",
        strategy_version="1",
        symbol="SPY",
        equity_curve_dates=DATES,
        equity_curve=CURVE,
        trades=TRADES,
        risk_rejections=0,
        skipped_conversions=0,
        data_quality_blocking=False,
        final_equity=CURVE[-1],
    )


class TestReturnAndDrawdown:
    def test_total_return(self) -> None:
        metrics = compute_metrics(make_result())
        # 105 / 100 - 1 = 5%
        assert metrics.total_return_fraction == pytest.approx(0.05)

    def test_max_drawdown_from_known_curve(self) -> None:
        metrics = compute_metrics(make_result())
        # peak 110 -> trough 99: (110 - 99) / 110 = 10%
        assert metrics.max_drawdown_fraction == pytest.approx(0.10)

    def test_longest_underwater_days(self) -> None:
        metrics = compute_metrics(make_result())
        # peak on 2024-12-31; still under water on 2025-01-03 -> 3 calendar days
        assert metrics.longest_underwater_days == 3

    def test_cagr_suppressed_for_tiny_window(self) -> None:
        # 4 calendar days is far below the 0.2-year annualization floor.
        metrics = compute_metrics(make_result())
        assert metrics.cagr_fraction is None


class TestTradeStatistics:
    def test_profit_factor_and_win_rate(self) -> None:
        metrics = compute_metrics(make_result())
        # wins +100, +50 -> gross profit 150; losses -50 -> gross loss 50
        assert metrics.gross_profit_usd == pytest.approx(150.0)
        assert metrics.gross_loss_usd == pytest.approx(50.0)
        assert metrics.profit_factor == pytest.approx(3.0)
        assert metrics.win_rate == pytest.approx(2.0 / 3.0)
        assert metrics.trades == 3

    def test_expectancy_and_payoff(self) -> None:
        metrics = compute_metrics(make_result())
        # expectancy = (100 + 50 - 50) / 3
        assert metrics.expectancy_usd == pytest.approx(100.0 / 3.0)
        # avg winner 75, avg loser 50 -> payoff 1.5
        assert metrics.average_winner_usd == pytest.approx(75.0)
        assert metrics.average_loser_usd == pytest.approx(50.0)
        assert metrics.payoff_ratio == pytest.approx(1.5)
        assert metrics.net_pnl_usd == pytest.approx(100.0)

    def test_consecutive_losses_and_costs(self) -> None:
        metrics = compute_metrics(make_result())
        assert metrics.max_consecutive_losses == 1
        # 6 commissions of $1 each
        assert metrics.commission_usd == pytest.approx(6.0)
        # turnover = qty * (entry + exit): 10*210 + 5*210 + 5*190 = 4100
        assert metrics.turnover_usd == pytest.approx(4100.0)
        # exposure = total bars held / curve length = (2+1+1)/4
        assert metrics.exposure_fraction == pytest.approx(1.0)

    def test_low_sample_flag(self) -> None:
        metrics = compute_metrics(make_result())
        assert metrics.low_sample is True


class TestPerYearReturns:
    def test_per_year_returns_split_at_year_boundary(self) -> None:
        metrics = compute_metrics(make_result())
        per_year = metrics.per_year_return_fraction
        assert set(per_year) == {2024, 2025}
        # 2024: 100 -> 110 = +10%
        assert per_year[2024] == pytest.approx(0.10)
        # 2025 starts from the 2024 close of 110 and ends at 105
        assert per_year[2025] == pytest.approx(105.0 / 110.0 - 1.0)
