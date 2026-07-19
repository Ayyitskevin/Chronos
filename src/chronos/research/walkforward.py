"""Rolling walk-forward + sample-honest verdict (AI Quant plan C3, ADR-0014 §2).

Rolls a **fixed** out-of-sample window across a deterministic backtest, pools the OOS
per-bar returns and trades, and returns a verdict whose **blocking default is
INSUFFICIENT_EVIDENCE**. It registers **one trial per configuration** in the C2 registry,
so the deflated Sharpe's multiple-testing N is honest rather than self-reported.

For the current fixed-rule strategies a "fold" is a causal replay over the OOS segment
after a warm-up prefix (nothing is re-fit; purged CV does not bind — `purged_cv.py`). The
per-window Sharpes give the cross-fold variance the DSR needs.

Research-plane and read-only: it drives the deterministic backtest (which uses the
*simulated* execution broker) and places no real order — it imports no `chronos.orders`
or `chronos.broker` module (enforced by `tests/safety/test_research_isolation.py`).
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from chronos.backtest.engine import BacktestConfig, ClosedTrade, run_backtest
from chronos.control.halt import HaltStore
from chronos.marketdata.bars import BarSeries
from chronos.registry import RegistryLedger, RunStage, register_run, trial_count
from chronos.research.stats import (
    block_bootstrap_ci,
    deflated_sharpe,
    moments,
    probabilistic_sharpe,
    sharpe,
    total_return,
)
from chronos.risk.policy import RiskPolicy
from chronos.strategies.base import Strategy

_DSR_PASS_THRESHOLD = 0.95


class WalkForwardVerdict(StrEnum):
    PASS = "pass"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class WindowResult:
    start: date
    end: date
    bars: int
    trades: int
    oos_return: float
    sharpe: float | None


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    strategy_id: str
    symbol: str
    seed: int
    windows: tuple[WindowResult, ...]
    pooled_bars: int
    pooled_trades: int
    pooled_sharpe: float | None
    sharpe_ci: tuple[float, float] | None
    psr: float | None
    deflated_sharpe: float | None
    trial_count: int
    purged_cv: str
    verdict: WalkForwardVerdict
    reason: str


def walk_forward(
    series: BarSeries,
    strategy_factory: Callable[[], Strategy],
    risk_policy: RiskPolicy,
    config: BacktestConfig,
    *,
    halt_store: HaltStore,
    ledger: RegistryLedger,
    strategy_id: str,
    config_hash: str,
    code_commit: str,
    data_hashes: dict[str, object],
    criteria_ref: str,
    test_window: int,
    warmup: int,
    min_trades: int,
    seed: int,
    block_size: int = 20,
    n_resamples: int = 1000,
) -> WalkForwardReport:
    """Run the fixed-config walk-forward and return its sample-honest report + verdict."""

    result = run_backtest(
        series=series,
        strategy=strategy_factory(),
        risk_policy=risk_policy,
        config=config,
        halt_store=halt_store,
    )
    curve = result.equity_curve
    dates = result.equity_curve_dates
    returns = [_bar_return(curve[i - 1], curve[i]) for i in range(1, len(curve))]
    oos_returns = returns[warmup:]
    oos_dates = dates[warmup + 1 :]  # returns[i] is aligned to dates[i + 1]

    windows = _windows(result.trades, oos_returns, oos_dates, test_window)
    pooled_trades = (
        sum(1 for t in result.trades if oos_dates and t.entry_date >= oos_dates[0])
        if oos_dates
        else 0
    )

    m = moments(oos_returns)
    pooled_sharpe = sharpe(oos_returns)

    # Register exactly one trial for this configuration; then read the honest cumulative N.
    register_run(
        ledger,
        stage=RunStage.VALIDATION,
        strategy_id=strategy_id,
        config_hash=config_hash,
        code_commit=code_commit,
        data_hashes=data_hashes,
        criteria_ref=criteria_ref,
        touched_data=True,
    )
    n_trials = trial_count(ledger, strategy_id=strategy_id)
    window_sharpes = [w.sharpe for w in windows if w.sharpe is not None]
    trial_variance = statistics.pvariance(window_sharpes) if len(window_sharpes) >= 2 else 0.0

    ci = (
        block_bootstrap_ci(
            oos_returns, sharpe, block_size=block_size, n_resamples=n_resamples, seed=seed
        )
        if len(oos_returns) >= 2
        else None
    )
    psr = (
        probabilistic_sharpe(pooled_sharpe, len(oos_returns), skew=m.skew, kurtosis=m.kurtosis)
        if pooled_sharpe is not None and m is not None
        else None
    )
    dsr = (
        deflated_sharpe(
            pooled_sharpe,
            len(oos_returns),
            trial_count=n_trials,
            trial_sharpe_variance=trial_variance,
            skew=m.skew,
            kurtosis=m.kurtosis,
        )
        if pooled_sharpe is not None and m is not None
        else None
    )

    verdict, reason = _verdict(pooled_trades, ci, dsr, min_trades)
    return WalkForwardReport(
        strategy_id=strategy_id,
        symbol=series.symbol,
        seed=seed,
        windows=windows,
        pooled_bars=len(oos_returns),
        pooled_trades=pooled_trades,
        pooled_sharpe=pooled_sharpe,
        sharpe_ci=ci,
        psr=psr,
        deflated_sharpe=dsr,
        trial_count=n_trials,
        purged_cv="not applicable (fixed-rule replay)",
        verdict=verdict,
        reason=reason,
    )


def _bar_return(prev_equity: float, equity: float) -> float:
    if prev_equity <= 0.0:
        return 0.0
    return equity / prev_equity - 1.0


def _windows(
    closed: tuple[ClosedTrade, ...],
    oos_returns: list[float],
    oos_dates: tuple[date, ...],
    test_window: int,
) -> tuple[WindowResult, ...]:
    windows: list[WindowResult] = []
    for start in range(0, len(oos_returns), test_window):
        seg = oos_returns[start : start + test_window]
        seg_dates = oos_dates[start : start + test_window]
        if len(seg) < 2:
            continue
        window_start, window_end = seg_dates[0], seg_dates[-1]
        seg_trades = sum(1 for t in closed if window_start <= t.entry_date <= window_end)
        windows.append(
            WindowResult(
                start=window_start,
                end=window_end,
                bars=len(seg),
                trades=seg_trades,
                oos_return=total_return(seg),
                sharpe=sharpe(seg),
            )
        )
    return tuple(windows)


def _verdict(
    pooled_trades: int,
    ci: tuple[float, float] | None,
    dsr: float | None,
    min_trades: int,
) -> tuple[WalkForwardVerdict, str]:
    if pooled_trades < min_trades:
        return (
            WalkForwardVerdict.INSUFFICIENT_EVIDENCE,
            f"only {pooled_trades} OOS trades (< {min_trades} floor)",
        )
    if ci is None or dsr is None:
        return WalkForwardVerdict.INSUFFICIENT_EVIDENCE, "statistics undefined (too few OOS bars)"
    if ci[1] <= 0.0:
        return WalkForwardVerdict.FAIL, f"Sharpe bootstrap CI strictly <= 0 {ci}"
    if ci[0] <= 0.0:
        return WalkForwardVerdict.INSUFFICIENT_EVIDENCE, f"Sharpe bootstrap CI includes 0 {ci}"
    if dsr < _DSR_PASS_THRESHOLD:
        return (
            WalkForwardVerdict.INSUFFICIENT_EVIDENCE,
            f"deflated Sharpe {dsr:.3f} < {_DSR_PASS_THRESHOLD}",
        )
    return WalkForwardVerdict.PASS, "Sharpe CI > 0 and deflated Sharpe >= threshold"
