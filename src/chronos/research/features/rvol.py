"""Daily In-Play (Pine 04) and DAY_1-inert time-of-day RVOL (Pine 26)."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from chronos.marketdata.bars import Bar, BarInterval
from chronos.research.features.models import (
    FeatureFamily,
    FeatureInputError,
    FeaturePolicy,
    FeatureSnapshot,
)
from chronos.research.five_tool.alignment import source_bar_id
from chronos.research.five_tool.indicators import pine_atr, pine_sma

_INTRADAY = {BarInterval.MIN_1, BarInterval.MIN_5, BarInterval.HOUR_1}


@dataclass(frozen=True, slots=True)
class RvolObservation:
    snapshot: FeatureSnapshot
    in_play: bool | None
    tod_inert: bool


def _wilson_lower(rate: float | None, count: int) -> float | None:
    if count <= 0 or rate is None:
        return None
    z = 1.96
    z2 = z * z
    return (
        rate + z2 / (2 * count) - z * math.sqrt((rate * (1.0 - rate) + z2 / (4 * count)) / count)
    ) / (1.0 + z2 / count)


def evaluate_daily_rvol(
    bars: Sequence[Bar],
    policy: FeaturePolicy | None = None,
) -> tuple[RvolObservation, ...]:
    """Daily In-Play using prior SMA(volume) and ATR.  TOD exports stay inert on DAY_1."""

    settings = policy or FeaturePolicy()
    if not bars:
        raise FeatureInputError("RVOL evaluation requires a non-empty primary series")
    volumes = tuple(bar.volume for bar in bars)
    volume_sma = pine_sma(volumes, settings.rvol_lookback)
    atr_series = pine_atr(
        tuple(bar.high for bar in bars),
        tuple(bar.low for bar in bars),
        tuple(bar.close for bar in bars),
        14,
    )
    observations: list[RvolObservation] = []
    previous: Bar | None = None
    for index, bar in enumerate(bars):
        if bar.status.value != "CLOSED":
            raise FeatureInputError("RVOL consumes closed bars only")
        prior_sma = volume_sma[index - 1] if index > 0 else None
        prior_atr = atr_series[index - 1] if index > 0 else None
        daily_rvol = bar.volume / prior_sma if prior_sma is not None and prior_sma > 0.0 else None
        avg_dollar_vol_m = prior_sma * bar.close / 1_000_000.0 if prior_sma is not None else None
        gap_atr = None
        if previous is not None and prior_atr is not None and prior_atr > 0.0:
            gap_atr = (bar.open - previous.close) / prior_atr
        warmed = prior_sma is not None
        rvol_ok = daily_rvol is not None and daily_rvol >= settings.rvol_min_ratio
        liquidity_ok = (
            avg_dollar_vol_m is not None
            and avg_dollar_vol_m >= settings.rvol_min_avg_dollar_vol_millions
        )
        gap_ok = settings.rvol_min_gap_atr <= 0.0 or (
            gap_atr is not None and abs(gap_atr) >= settings.rvol_min_gap_atr
        )
        in_play = rvol_ok and liquidity_ok and gap_ok if warmed else None
        tod_inert = bar.interval is BarInterval.DAY_1
        snapshot = FeatureSnapshot(
            family=FeatureFamily.RVOL,
            timestamp_utc=bar.timestamp_utc,
            primary_sequence_id=source_bar_id(bar),
            warmup=not warmed,
            values=(
                ("IN_PLAY", None if in_play is None else in_play),
                ("RVOL_DAILY", daily_rvol),
                ("AVG_DOLLARVOL_M", avg_dollar_vol_m),
                ("GAP_ATR", gap_atr),
                ("TOD_INERT", tod_inert),
                ("RVOL_TOD", None),
                ("TRF_ELEVATED", None),
                ("TRF_BULL_TILT", None),
                ("TRF_BEAR_TILT", None),
                ("TRF_WARM", False),
            ),
        )
        observations.append(
            RvolObservation(snapshot=snapshot, in_play=in_play, tod_inert=tod_inert)
        )
        previous = bar
    return tuple(observations)


def evaluate_tod_rvol(
    bars: Sequence[Bar],
    *,
    regime: Sequence[int | None],
    policy: FeaturePolicy | None = None,
) -> tuple[RvolObservation, ...]:
    """Pine 26 TOD matrix.  Daily bars are inert; only intraday intervals count."""

    settings = policy or FeaturePolicy()
    if len(bars) != len(regime):
        raise FeatureInputError("TOD RVOL regime series must match the bar count")
    if not bars:
        raise FeatureInputError("TOD RVOL evaluation requires a non-empty series")
    days = min(settings.rvol_tod_days, 60)
    max_bars = min(settings.rvol_tod_max_bars, 500)
    matrix: dict[int, dict[int, float]] = defaultdict(dict)
    row = 0
    day_bar = -1
    cum = 0.0
    previous_session = None
    bull_e_n = bull_e_h = bull_b_n = bull_b_h = 0
    bear_e_n = bear_e_h = bear_b_n = bear_b_h = 0
    prior_warm = False
    prior_rvol: float | None = None
    prior_regime: int | None = None
    observations: list[RvolObservation] = []
    previous_close: float | None = None
    for index, bar in enumerate(bars):
        if bar.status.value != "CLOSED":
            raise FeatureInputError("TOD RVOL consumes closed bars only")
        inert = bar.interval not in _INTRADAY
        is_new_day = previous_session is None or bar.session_date != previous_session
        if is_new_day:
            day_bar = 0
            cum = bar.volume
            row = (row + 1) % days if previous_session is not None else 0
            matrix[row] = {}
        else:
            day_bar += 1
            cum += bar.volume
        if 0 <= day_bar < max_bars:
            matrix[row][day_bar] = cum
        tod_sum = 0.0
        tod_n = 0
        if 0 <= day_bar < max_bars:
            for other_row, slots in matrix.items():
                if other_row == row:
                    continue
                value = slots.get(day_bar)
                if value is not None:
                    tod_sum += value
                    tod_n += 1
        rvol_tod = None if inert or tod_n <= 0 or tod_sum <= 0.0 else cum / (tod_sum / tod_n)
        tod_warm = (not inert) and tod_n >= 5
        elevated = tod_warm and rvol_tod is not None and rvol_tod >= settings.rvol_tod_elevated
        if (
            not inert
            and not is_new_day
            and previous_close is not None
            and prior_warm
            and prior_rvol is not None
            and prior_regime in {-1, 1}
        ):
            continued = (
                bar.close > previous_close if prior_regime == 1 else bar.close < previous_close
            )
            elevated_prior = prior_rvol >= settings.rvol_tod_elevated
            if prior_regime == 1:
                if elevated_prior:
                    bull_e_n += 1
                    bull_e_h += int(continued)
                else:
                    bull_b_n += 1
                    bull_b_h += int(continued)
            elif elevated_prior:
                bear_e_n += 1
                bear_e_h += int(continued)
            else:
                bear_b_n += 1
                bear_b_h += int(continued)
        bull_e_r = bull_e_h / bull_e_n if bull_e_n else None
        bull_b_r = bull_b_h / bull_b_n if bull_b_n else None
        bear_e_r = bear_e_h / bear_e_n if bear_e_n else None
        bear_b_r = bear_b_h / bear_b_n if bear_b_n else None
        bull_wlo = _wilson_lower(bull_e_r, bull_e_n)
        bear_wlo = _wilson_lower(bear_e_r, bear_e_n)
        bull_tilt = (
            None
            if bull_e_n < settings.rvol_tod_min_side_n or bull_b_r is None or bull_wlo is None
            else 1.0
            if bull_wlo > bull_b_r + settings.rvol_tod_tilt_margin
            else 0.0
        )
        bear_tilt = (
            None
            if bear_e_n < settings.rvol_tod_min_side_n or bear_b_r is None or bear_wlo is None
            else 1.0
            if bear_wlo > bear_b_r + settings.rvol_tod_tilt_margin
            else 0.0
        )
        snapshot = FeatureSnapshot(
            family=FeatureFamily.RVOL,
            timestamp_utc=bar.timestamp_utc,
            primary_sequence_id=source_bar_id(bar),
            warmup=inert or not tod_warm,
            values=(
                ("IN_PLAY", None),
                ("RVOL_DAILY", None),
                ("AVG_DOLLARVOL_M", None),
                ("GAP_ATR", None),
                ("TOD_INERT", inert),
                ("RVOL_TOD", rvol_tod),
                ("TRF_ELEVATED", None if inert else elevated),
                ("TRF_BULL_TILT", bull_tilt),
                ("TRF_BEAR_TILT", bear_tilt),
                ("TRF_WARM", tod_warm),
            ),
        )
        observations.append(RvolObservation(snapshot=snapshot, in_play=None, tod_inert=inert))
        prior_warm = tod_warm
        prior_rvol = rvol_tod
        prior_regime = regime[index]
        previous_close = bar.close
        previous_session = bar.session_date
    return tuple(observations)


def merge_rvol_snapshots(
    daily: Sequence[RvolObservation],
    tod: Sequence[RvolObservation] | None = None,
) -> tuple[FeatureSnapshot, ...]:
    """Keep daily In-Play as the veto surface; attach TOD exports when present."""

    if tod is not None and len(tod) != len(daily):
        raise FeatureInputError("daily and TOD RVOL series must have equal length")
    merged: list[FeatureSnapshot] = []
    for index, item in enumerate(daily):
        values = dict(item.snapshot.values)
        if tod is not None:
            tod_values = dict(tod[index].snapshot.values)
            for key in (
                "RVOL_TOD",
                "TRF_ELEVATED",
                "TRF_BULL_TILT",
                "TRF_BEAR_TILT",
                "TRF_WARM",
                "TOD_INERT",
            ):
                values[key] = tod_values[key]
        merged.append(
            FeatureSnapshot(
                family=FeatureFamily.RVOL,
                timestamp_utc=item.snapshot.timestamp_utc,
                primary_sequence_id=item.snapshot.primary_sequence_id,
                warmup=item.snapshot.warmup,
                values=tuple(values.items()),
            )
        )
    return tuple(merged)
