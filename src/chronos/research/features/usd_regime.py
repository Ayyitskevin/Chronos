"""Rising-USD headwind for GLD. UUP fixtures or owner bytes; never a download.

This is the H-PAIR-GLD-USD treatment engine. Default FeaturePolicy keeps
``enable_usd_regime=False``, so the locked gold shadow path is unchanged.
Missing UUP fails closed. Chronos does not fetch UUP or DXY.
"""

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
    UsdState,
)
from chronos.research.five_tool.alignment import source_bar_id


@dataclass(frozen=True, slots=True)
class UsdObservation:
    snapshot: FeatureSnapshot
    state: UsdState | None
    slope: float | None


def require_certified_uup() -> None:
    """Refuse every attempt to treat UUP as a certified companion."""

    raise FeatureInputError(
        "UUP stays pending_certified_dataset; this slice does not download "
        "or open a USD series"
    )


def _slope(history: Sequence[float | None], lookback: int) -> float | None:
    if len(history) <= lookback:
        return None
    current = history[-1]
    lagged = history[-1 - lookback]
    if current is None or lagged is None:
        return None
    return current - lagged


def _state_from_slope(slope: float | None) -> UsdState | None:
    if slope is None:
        return None
    if slope > 0.0:
        return UsdState.RISING
    if slope < 0.0:
        return UsdState.FALLING
    return UsdState.FLAT


def evaluate_usd_regime(
    primary: Sequence[Bar],
    uup: BarSeries | None,
    policy: FeaturePolicy | None = None,
) -> tuple[UsdObservation, ...]:
    """UUP close slope versus a frozen lookback. Rising dollar vetoes GLD ENTER."""

    settings = policy or FeaturePolicy()
    if not primary:
        raise FeatureInputError("USD evaluation requires a non-empty primary series")
    aligned = align_companions(primary, {"uup": uup}, allow_equal={"uup": True})
    closes: list[float | None] = []
    observations: list[UsdObservation] = []
    for bar, companions in zip(primary, aligned, strict=True):
        uup_bar = companions["uup"]
        missing = () if uup_bar is not None else ("uup",)
        closes.append(None if uup_bar is None else uup_bar.close)
        slope = _slope(closes, settings.usd_slope_lookback)
        state = None if missing else _state_from_slope(slope)
        snapshot = FeatureSnapshot(
            family=FeatureFamily.USD_REGIME,
            timestamp_utc=bar.timestamp_utc,
            primary_sequence_id=source_bar_id(bar),
            warmup=not missing and slope is None,
            missing_required=missing,
            values=(
                ("USD_STATE", None if state is None else state.value),
                ("USD_SLOPE", slope),
                ("UUP_CLOSE", None if uup_bar is None else uup_bar.close),
            ),
        )
        observations.append(UsdObservation(snapshot=snapshot, state=state, slope=slope))
    return tuple(observations)
