"""Causal companion alignment for pairing features.

Benchmark-style joins use the latest companion close at or before the primary
close.  Prior-completed joins (Pine ``close[1]`` + ``lookahead_on``) require a
strictly earlier companion timestamp.  Future values are refused.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from chronos.marketdata.bars import Bar, BarSeries, BarStatus
from chronos.research.features.models import FeatureInputError
from chronos.research.five_tool.alignment import source_bar_id


def require_closed(bar: Bar, label: str) -> None:
    if bar.status is not BarStatus.CLOSED:
        raise FeatureInputError(f"{label} must be a closed bar")


def _series_feed_identity(series: BarSeries, label: str) -> tuple[str, str] | None:
    if not series.bars:
        return None
    expected = (series.bars[0].source, series.bars[0].exchange)
    if any((bar.source, bar.exchange) != expected for bar in series.bars[1:]):
        raise FeatureInputError(f"{label} source/exchange identity changed within the series")
    return expected


def latest_companion(
    primary: Bar,
    series: BarSeries | None,
    *,
    label: str,
    allow_equal: bool,
) -> Bar | None:
    """Return the latest causal companion bar, or ``None`` when none is eligible."""

    require_closed(primary, "primary")
    if series is None or not series.bars:
        return None
    _series_feed_identity(series, label)
    chosen: Bar | None = None
    for bar in series.bars:
        require_closed(bar, label)
        if bar.timestamp_utc > primary.timestamp_utc:
            continue
        if bar.timestamp_utc == primary.timestamp_utc and not allow_equal:
            continue
        chosen = bar
    return chosen


def align_companions(
    primary_bars: Sequence[Bar],
    companions: Mapping[str, BarSeries | None],
    *,
    allow_equal: Mapping[str, bool] | None = None,
) -> tuple[dict[str, Bar | None], ...]:
    """Align every named companion to each primary close without backfill."""

    if not primary_bars:
        raise FeatureInputError("alignment requires a non-empty primary series")
    equal_policy = allow_equal or {}
    aligned: list[dict[str, Bar | None]] = []
    previous: Bar | None = None
    for primary in primary_bars:
        require_closed(primary, "primary")
        if previous is not None and primary.timestamp_utc <= previous.timestamp_utc:
            raise FeatureInputError("primary closes must be strictly increasing")
        if previous is not None and (
            primary.symbol != previous.symbol
            or primary.source != previous.source
            or primary.exchange != previous.exchange
            or primary.interval is not previous.interval
        ):
            raise FeatureInputError("primary symbol/source/exchange/interval changed mid-stream")
        row: dict[str, Bar | None] = {}
        for name, series in companions.items():
            row[name] = latest_companion(
                primary,
                series,
                label=name,
                allow_equal=equal_policy.get(name, True),
            )
        aligned.append(row)
        previous = primary
    return tuple(aligned)


def companion_source_id(bar: Bar | None) -> str | None:
    return None if bar is None else source_bar_id(bar)
