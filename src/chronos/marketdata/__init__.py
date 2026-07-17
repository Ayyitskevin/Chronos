"""Market-data plane for the deterministic strategy platform.

This package is independent of the wheel-dashboard broker adapters. It defines
the bar vocabulary, data-provider ports, and fail-closed data-quality checks
used by research, backtesting, replay, shadow, and paper execution.
"""

from chronos.marketdata.bars import Bar, BarInterval, BarSeries, BarStatus
from chronos.marketdata.quality import DataQualityIssue, DataQualityReport, validate_series

__all__ = [
    "Bar",
    "BarInterval",
    "BarSeries",
    "BarStatus",
    "DataQualityIssue",
    "DataQualityReport",
    "validate_series",
]
