"""Performance metrics computed from a backtest result (Phase 6).

All ratios are reported with their assumptions; nothing here annualizes from
fewer than 30 observations without flagging ``low_sample``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from chronos.backtest.engine import BacktestResult

_TRADING_DAYS = 252


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    trades: int
    win_rate: float | None
    profit_factor: float | None
    expectancy_usd: float | None
    average_winner_usd: float | None
    average_loser_usd: float | None
    payoff_ratio: float | None
    max_consecutive_losses: int
    total_return_fraction: float
    cagr_fraction: float | None
    annualized_volatility: float | None
    sharpe: float | None
    sortino: float | None
    calmar: float | None
    max_drawdown_fraction: float
    longest_underwater_days: int
    exposure_fraction: float
    turnover_usd: float
    net_pnl_usd: float
    gross_profit_usd: float
    gross_loss_usd: float
    commission_usd: float
    per_year_return_fraction: dict[int, float]
    low_sample: bool


def compute_metrics(result: BacktestResult) -> PerformanceMetrics:
    curve = list(result.equity_curve)
    dates = list(result.equity_curve_dates)
    trades = list(result.trades)

    daily_returns: list[float] = []
    for i in range(1, len(curve)):
        if curve[i - 1] > 0:
            daily_returns.append(curve[i] / curve[i - 1] - 1.0)

    total_return = curve[-1] / curve[0] - 1.0 if curve and curve[0] > 0 else 0.0
    years = _year_fraction(dates)
    cagr = (
        (curve[-1] / curve[0]) ** (1.0 / years) - 1.0
        if curve and curve[0] > 0 and years and years > 0.2
        else None
    )

    vol = _std(daily_returns)
    ann_vol = vol * math.sqrt(_TRADING_DAYS) if vol is not None else None
    mean_daily = _mean(daily_returns)
    sharpe = (
        (mean_daily * _TRADING_DAYS) / ann_vol
        if ann_vol is not None and ann_vol > 0 and mean_daily is not None
        else None
    )
    downside = _std([r for r in daily_returns if r < 0])
    sortino = (
        (mean_daily * _TRADING_DAYS) / (downside * math.sqrt(_TRADING_DAYS))
        if downside is not None and downside > 0 and mean_daily is not None
        else None
    )

    max_dd, underwater_days = _drawdown(curve, dates)
    calmar = cagr / max_dd if cagr is not None and max_dd > 0 else None

    wins = [t.net_pnl for t in trades if t.net_pnl > 0]
    losses = [t.net_pnl for t in trades if t.net_pnl <= 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    win_rate = len(wins) / len(trades) if trades else None
    expectancy = _mean([t.net_pnl for t in trades])
    avg_win = _mean(wins)
    avg_loss = _mean([-loss for loss in losses])
    payoff = avg_win / avg_loss if avg_win is not None and avg_loss and avg_loss > 0 else None

    max_consec = 0
    run = 0
    for trade in trades:
        run = run + 1 if trade.net_pnl < 0 else 0
        max_consec = max(max_consec, run)

    exposure_days = sum(t.bars_held for t in trades)
    exposure = exposure_days / len(curve) if curve else 0.0
    turnover = sum(t.quantity * (t.entry_price + t.exit_price) for t in trades)
    commission = sum(t.entry_commission + t.exit_commission for t in trades)

    per_year: dict[int, float] = {}
    year_start_equity: dict[int, float] = {}
    for i, session in enumerate(dates):
        year_start_equity.setdefault(session.year, curve[i - 1] if i > 0 else curve[0])
        per_year[session.year] = curve[i]
    per_year_return = {
        year: per_year[year] / start - 1.0 for year, start in year_start_equity.items() if start > 0
    }

    return PerformanceMetrics(
        trades=len(trades),
        win_rate=win_rate,
        profit_factor=profit_factor,
        expectancy_usd=expectancy,
        average_winner_usd=avg_win,
        average_loser_usd=avg_loss,
        payoff_ratio=payoff,
        max_consecutive_losses=max_consec,
        total_return_fraction=total_return,
        cagr_fraction=cagr,
        annualized_volatility=ann_vol,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        max_drawdown_fraction=max_dd,
        longest_underwater_days=underwater_days,
        exposure_fraction=exposure,
        turnover_usd=turnover,
        net_pnl_usd=sum(t.net_pnl for t in trades),
        gross_profit_usd=gross_profit,
        gross_loss_usd=gross_loss,
        commission_usd=commission,
        per_year_return_fraction=per_year_return,
        low_sample=len(trades) < 30,
    )


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _std(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _year_fraction(dates: list[date]) -> float | None:
    if len(dates) < 2:
        return None
    return (dates[-1] - dates[0]).days / 365.25


def _drawdown(curve: list[float], dates: list[date]) -> tuple[float, int]:
    peak = float("-inf")
    peak_date: date | None = None
    max_dd = 0.0
    longest_underwater = 0
    for i, value in enumerate(curve):
        if value > peak:
            peak = value
            peak_date = dates[i]
        elif peak > 0:
            max_dd = max(max_dd, 1.0 - value / peak)
            if peak_date is not None:
                longest_underwater = max(longest_underwater, (dates[i] - peak_date).days)
    return max_dd, longest_underwater
