"""Daily ETF-ratio ALIGN port of Pine 09.  Internals are optional and never required."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from chronos.marketdata.bars import Bar, BarSeries
from chronos.research.features.alignment import align_companions
from chronos.research.features.models import (
    FeatureFamily,
    FeatureInputError,
    FeaturePolicy,
    FeatureSnapshot,
)
from chronos.research.five_tool.alignment import source_bar_id


@dataclass(frozen=True, slots=True)
class BreadthObservation:
    snapshot: FeatureSnapshot
    align: int | None
    breadth_direction: int | None


def _ratio(numerator: Bar | None, denominator: Bar | None) -> float | None:
    if numerator is None or denominator is None or denominator.close == 0.0:
        return None
    return numerator.close / denominator.close


def _slope(history: Sequence[float | None], lookback: int) -> float | None:
    if len(history) <= lookback:
        return None
    current = history[-1]
    lagged = history[-1 - lookback]
    if current is None or lagged is None:
        return None
    return current - lagged


def _direction(breadth_slope: float | None, risk_slope: float | None) -> int | None:
    if breadth_slope is None or risk_slope is None:
        return None
    if breadth_slope > 0.0 and risk_slope > 0.0:
        return 1
    if breadth_slope < 0.0 and risk_slope < 0.0:
        return -1
    return 0


def _align(regime: int | None, breadth_direction: int | None) -> int | None:
    if regime not in {-1, 0, 1} or breadth_direction is None:
        return None
    if regime == 0 or breadth_direction == 0:
        return 0
    if regime == breadth_direction:
        return 1
    return -1


def evaluate_breadth(
    primary: Sequence[Bar],
    *,
    spy: BarSeries | None,
    rsp: BarSeries | None,
    qqq: BarSeries | None,
    regime: Sequence[int | None],
    tick: BarSeries | None = None,
    add: BarSeries | None = None,
    vold: BarSeries | None = None,
    policy: FeaturePolicy | None = None,
) -> tuple[BreadthObservation, ...]:
    """Daily ALIGN from RSP/SPY and QQQ/SPY slopes versus the Five-Tool regime."""

    settings = policy or FeaturePolicy()
    if not primary:
        raise FeatureInputError("breadth evaluation requires a non-empty primary series")
    if len(regime) != len(primary):
        raise FeatureInputError("breadth regime series must match the primary bar count")
    aligned = align_companions(
        primary,
        {"spy": spy, "rsp": rsp, "qqq": qqq, "tick": tick, "add": add, "vold": vold},
        allow_equal={
            "spy": True,
            "rsp": True,
            "qqq": True,
            "tick": True,
            "add": True,
            "vold": True,
        },
    )
    breadth_history: list[float | None] = []
    risk_history: list[float | None] = []
    observations: list[BreadthObservation] = []
    for bar, companions, regime_value in zip(primary, aligned, regime, strict=True):
        missing = tuple(name for name in ("spy", "rsp", "qqq") if companions[name] is None)
        breadth_ratio = _ratio(companions["rsp"], companions["spy"])
        risk_ratio = _ratio(companions["qqq"], companions["spy"])
        breadth_history.append(breadth_ratio)
        risk_history.append(risk_ratio)
        breadth_slope = _slope(breadth_history, settings.breadth_slope_lookback)
        risk_slope = _slope(risk_history, settings.breadth_slope_lookback)
        direction = _direction(breadth_slope, risk_slope)
        align = None if missing else _align(regime_value, direction)
        snapshot = FeatureSnapshot(
            family=FeatureFamily.BREADTH,
            timestamp_utc=bar.timestamp_utc,
            primary_sequence_id=source_bar_id(bar),
            warmup=not missing and (breadth_slope is None or risk_slope is None),
            missing_required=missing,
            values=(
                ("ALIGN", align),
                ("BREADTH_SLOPE", breadth_slope),
                ("RISK_SLOPE", risk_slope),
                ("BREADTH_DIRECTION", direction),
                ("TICK", None if companions["tick"] is None else companions["tick"].close),
                ("ADD", None if companions["add"] is None else companions["add"].close),
                ("VOLD", None if companions["vold"] is None else companions["vold"].close),
                ("REGIME", regime_value),
            ),
        )
        observations.append(
            BreadthObservation(snapshot=snapshot, align=align, breadth_direction=direction)
        )
    return tuple(observations)
