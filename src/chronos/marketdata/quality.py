"""Fail-closed data-quality validation for bar series.

A failed check yields machine-readable issues. Callers in the execution plane
must treat any ``blocking`` issue as an order-generation lock; research callers
record the report next to their results.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from chronos.marketdata.bars import BarSeries, BarStatus


class IssueKind(StrEnum):
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    NON_FINITE_VALUE = "NON_FINITE_VALUE"
    IMPOSSIBLE_OHLC = "IMPOSSIBLE_OHLC"
    NEGATIVE_VOLUME = "NEGATIVE_VOLUME"
    DUPLICATE_BAR = "DUPLICATE_BAR"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    WEEKEND_BAR = "WEEKEND_BAR"
    LARGE_GAP = "LARGE_GAP"
    EXTREME_RETURN = "EXTREME_RETURN"
    UNCLOSED_BAR = "UNCLOSED_BAR"
    EMPTY_SERIES = "EMPTY_SERIES"


@dataclass(frozen=True, slots=True)
class DataQualityIssue:
    kind: IssueKind
    blocking: bool
    detail: str


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    symbol: str
    bar_count: int
    issues: tuple[DataQualityIssue, ...]

    @property
    def blocking(self) -> bool:
        return any(issue.blocking for issue in self.issues)

    @property
    def ok(self) -> bool:
        return not self.issues


_MAX_CALENDAR_GAP = timedelta(days=7)
_EXTREME_DAILY_RETURN = 0.35


def validate_series(series: BarSeries) -> DataQualityReport:
    """Validate one bar series. Ordering/uniqueness is enforced by BarSeries itself."""

    issues: list[DataQualityIssue] = []
    if len(series) == 0:
        issues.append(
            DataQualityIssue(IssueKind.EMPTY_SERIES, blocking=True, detail="series has no bars")
        )
        return DataQualityReport(symbol=series.symbol, bar_count=0, issues=tuple(issues))

    seen_ids: set[str] = set()
    previous_close: float | None = None
    previous_ts = None
    for bar in series:
        label = f"{bar.symbol} {bar.session_date.isoformat()}"
        if bar.status is not BarStatus.CLOSED:
            issues.append(DataQualityIssue(IssueKind.UNCLOSED_BAR, blocking=True, detail=label))
        if bar.sequence_id in seen_ids:
            issues.append(DataQualityIssue(IssueKind.DUPLICATE_BAR, blocking=True, detail=label))
        seen_ids.add(bar.sequence_id)
        # inf passes both the positivity and OHLC-range comparisons, and NaN
        # volume fails no ordered comparison — check finiteness explicitly
        # (independent review M5: silent-pass corruption classes).
        if not all(
            math.isfinite(value) for value in (bar.open, bar.high, bar.low, bar.close, bar.volume)
        ):
            issues.append(DataQualityIssue(IssueKind.NON_FINITE_VALUE, blocking=True, detail=label))
        elif min(bar.open, bar.high, bar.low, bar.close) <= 0:
            issues.append(
                DataQualityIssue(IssueKind.NON_POSITIVE_PRICE, blocking=True, detail=label)
            )
        elif not (bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high):
            issues.append(DataQualityIssue(IssueKind.IMPOSSIBLE_OHLC, blocking=True, detail=label))
        if bar.volume < 0:
            issues.append(DataQualityIssue(IssueKind.NEGATIVE_VOLUME, blocking=True, detail=label))
        if bar.session_date.weekday() >= 5:
            issues.append(DataQualityIssue(IssueKind.WEEKEND_BAR, blocking=False, detail=label))
        if previous_ts is not None and bar.timestamp_utc - previous_ts > _MAX_CALENDAR_GAP:
            issues.append(
                DataQualityIssue(
                    IssueKind.LARGE_GAP,
                    blocking=False,
                    detail=f"{label}: gap {(bar.timestamp_utc - previous_ts).days} days",
                )
            )
        if previous_close is not None and previous_close > 0:
            move = abs(bar.close / previous_close - 1.0)
            if move > _EXTREME_DAILY_RETURN:
                issues.append(
                    DataQualityIssue(
                        IssueKind.EXTREME_RETURN,
                        blocking=False,
                        detail=f"{label}: close moved {move:.1%} vs prior close",
                    )
                )
        previous_close = bar.close
        previous_ts = bar.timestamp_utc

    return DataQualityReport(symbol=series.symbol, bar_count=len(series), issues=tuple(issues))
