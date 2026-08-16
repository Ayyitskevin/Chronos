"""Closed-bar port of Pine 32 tail-risk moments.  Same-symbol OHLCV only."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from chronos.marketdata.bars import Bar
from chronos.research.features.models import (
    FeatureFamily,
    FeatureInputError,
    FeaturePolicy,
    FeatureSnapshot,
    TailState,
)
from chronos.research.five_tool.alignment import source_bar_id
from chronos.research.five_tool.indicators import pine_atr


@dataclass(frozen=True, slots=True)
class TailRiskObservation:
    snapshot: FeatureSnapshot
    state: TailState | None


def _moments(
    returns: Sequence[float],
) -> tuple[float, float, float, float, float, float] | None:
    count = len(returns)
    if count == 0:
        return None
    mean = math.fsum(returns) / count
    second = 0.0
    third = 0.0
    fourth = 0.0
    downside = 0.0
    for item in returns:
        delta = item - mean
        square = delta * delta
        second += square
        third += square * delta
        fourth += square * square
        if delta < 0.0:
            downside += square
    stdev = math.sqrt(second / count)
    if stdev <= 0.0:
        return None
    skew = (third / count) / (stdev**3)
    excess_kurtosis = (fourth / count) / (stdev**4) - 3.0
    semidev_ratio = math.sqrt(downside / count) / stdev
    return mean, stdev, skew, excess_kurtosis, semidev_ratio, min(returns)


def evaluate_tail_risk(
    bars: Sequence[Bar],
    policy: FeaturePolicy | None = None,
) -> tuple[TailRiskObservation, ...]:
    """Causal tail-risk snapshots.  Warm until ``tail_window`` completed returns."""

    settings = policy or FeaturePolicy()
    if not bars:
        raise FeatureInputError("tail-risk evaluation requires a non-empty primary series")
    highs = tuple(bar.high for bar in bars)
    lows = tuple(bar.low for bar in bars)
    closes = tuple(bar.close for bar in bars)
    atr_series = pine_atr(highs, lows, closes, settings.tail_atr_length)
    returns: deque[float] = deque()
    changes: deque[float] = deque()
    observations: list[TailRiskObservation] = []
    previous_close: float | None = None
    for index, bar in enumerate(bars):
        if bar.status.value != "CLOSED":
            raise FeatureInputError("tail-risk consumes closed bars only")
        if previous_close is not None and bar.close > 0.0 and previous_close > 0.0:
            returns.append(math.log(bar.close / previous_close))
            changes.append(bar.close - previous_close)
            if len(returns) > settings.tail_window:
                returns.popleft()
                changes.popleft()
        warmed = len(returns) >= settings.tail_window
        computed = _moments(tuple(returns)) if warmed else None
        atr = atr_series[index]
        worst_change = min(changes) if changes else None
        worst_atr = (
            worst_change / atr
            if computed is not None and worst_change is not None and atr is not None and atr > 0.0
            else None
        )
        if computed is None:
            mean = stdev = skew = kurtosis = semidev = None
            state: TailState | None = None
        else:
            mean, stdev, skew, kurtosis, semidev, _ = computed
            if kurtosis > settings.tail_kurtosis_fat or skew < settings.tail_skew_fat:
                state = TailState.FAT_TAILED
            elif kurtosis > settings.tail_kurtosis_elevated:
                state = TailState.ELEVATED
            else:
                state = TailState.ORDINARY
        snapshot = FeatureSnapshot(
            family=FeatureFamily.TAIL_RISK,
            timestamp_utc=bar.timestamp_utc,
            primary_sequence_id=source_bar_id(bar),
            warmup=not warmed or state is None,
            values=(
                ("TR_STATE", None if state is None else state.value),
                ("TR_KURT", kurtosis),
                ("TR_SKEW", skew),
                ("TR_SEMIDEV_RATIO", semidev),
                ("TR_WORST_ATR", worst_atr),
                ("TR_MEAN", mean),
                ("TR_SD", stdev),
                ("TR_N", len(returns)),
            ),
        )
        observations.append(TailRiskObservation(snapshot=snapshot, state=state))
        previous_close = bar.close
    return tuple(observations)
