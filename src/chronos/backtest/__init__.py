"""Deterministic backtesting and replay (Phase 7 research plane).

The backtest engine drives the same portfolio, risk, execution, and simulated
broker components used by replay and (with a different adapter) paper mode.
Repeated runs over identical inputs produce byte-identical results; the replay
test suite asserts this.
"""

from chronos.backtest.engine import BacktestConfig, BacktestResult, run_backtest
from chronos.backtest.metrics import PerformanceMetrics, compute_metrics

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "PerformanceMetrics",
    "compute_metrics",
    "run_backtest",
]
