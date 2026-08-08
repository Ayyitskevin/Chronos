"""Deterministic, research-only Five-Tool Confluence v3.6 signal engine.

The implementation has one causal transition kernel.  Batch evaluation, one-bar
streaming, and checkpoint replay all call :meth:`FiveToolEngine.step`; there is no
second vectorized decision path that can silently drift.  The module imports no
broker, order writer, mandate, or production strategy registry.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, replace
from datetime import datetime
from itertools import pairwise

from chronos.marketdata.bars import BarInterval
from chronos.research.five_tool.indicators import (
    confirmed_pivot,
    pine_atr,
    pine_dmi,
    pine_ema,
    pine_mfi,
    pine_percentrank,
    pine_rma,
    pine_rsi,
    pine_sma,
    pine_stdev,
    rolling_extreme,
)
from chronos.research.five_tool.models import (
    FiveToolBarInput,
    FiveToolInputError,
    FiveToolSettings,
    FiveToolState,
    FiveToolTrace,
    SetupFamily,
    Side,
    SignalEvent,
    SignalIntent,
    TraceValue,
)


def _last[T](values: tuple[T, ...] | list[T]) -> T | None:
    return values[-1] if values else None


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if not _finite(numerator) or not _finite(denominator) or denominator == 0.0:
        return None
    assert numerator is not None and denominator is not None
    return numerator / denominator


def _quality_gate(mode: str, adx_ok: bool, er_ok: bool) -> bool:
    return {
        "Off": True,
        "ADX": adx_ok,
        "Efficiency Ratio": er_ok,
        "ADX or ER": adx_ok or er_ok,
        "ADX and ER": adx_ok and er_ok,
    }.get(mode, True)


def _markov_index(regime: int | None) -> int | None:
    return None if regime is None else {1: 0, 0: 1, -1: 2}.get(regime)


def _wilson_lower(successes: int, sample_size: int) -> float | None:
    if sample_size <= 0:
        return None
    probability = successes / sample_size
    z_value = 1.96
    denominator = 1.0 + z_value**2 / sample_size
    center = (probability + z_value**2 / (2.0 * sample_size)) / denominator
    half = (
        z_value
        * math.sqrt(
            (probability * (1.0 - probability) + z_value**2 / (4.0 * sample_size)) / sample_size
        )
        / denominator
    )
    return max(0.0, center - half)


def _dwell_percentile(history: tuple[int, ...], current: int) -> float | None:
    if len(history) < 5:
        return None
    return 100.0 * sum(value <= current for value in history) / len(history)


def _previous_extreme(values: tuple[float | None, ...], length: int, kind: str) -> float | None:
    if len(values) <= 1:
        return None
    series = rolling_extreme(values[:-1], length, "highest" if kind == "high" else "lowest")
    return _last(series)


def _current_extreme(values: tuple[float, ...], length: int, kind: str) -> float | None:
    if len(values) < length:
        return None
    window = values[-length:]
    return max(window) if kind == "high" else min(window)


def _parse_pine_timestamp(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%d %b %Y %H:%M %z")
    except ValueError as exc:
        raise FiveToolInputError(f"unsupported Pine timestamp literal: {value!r}") from exc


def _stable_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _profile(settings: FiveToolSettings, interval: BarInterval) -> tuple[int, float, float, int]:
    requested = settings.text("preset_input")
    if requested == "Auto":
        requested = (
            "4H"
            if interval in {BarInterval.MIN_1, BarInterval.MIN_5, BarInterval.HOUR_1}
            else "Daily"
        )
    if requested == "Custom":
        return (
            settings.integer("lookback_custom"),
            settings.number("enter_z_custom"),
            settings.number("exit_z_custom"),
            settings.integer("confirm_custom"),
        )
    if requested == "4H":
        return 30, 1.10, 0.70, 3
    if requested == "Weekly":
        return 20, 0.70, 0.45, 2
    return 20, 0.85, 0.55, 2


def _select_setup(
    *,
    flip: bool,
    hidden: bool,
    regular: bool,
    reclaim: bool,
    long_v2: tuple[bool, bool, bool, bool] | None = None,
    short_v2: tuple[bool, bool, bool] | None = None,
) -> SetupFamily:
    if long_v2 is not None:
        pullback, retest, avwap_reclaim, breakout = long_v2
        if pullback:
            return SetupFamily.LEADER_PULLBACK
        if retest:
            return SetupFamily.BULL_RETEST
        if avwap_reclaim:
            return SetupFamily.LEGACY_AVWAP_RECLAIM
        if breakout:
            return SetupFamily.BASE_BREAKOUT
        return SetupFamily.NONE
    if short_v2 is not None:
        failed_reclaim, breakdown, retest = short_v2
        if failed_reclaim:
            return SetupFamily.FAILED_AVWAP_RECLAIM
        if breakdown:
            return SetupFamily.BEAR_FLAG_BREAKDOWN
        if retest:
            return SetupFamily.BEAR_RETEST
        return SetupFamily.NONE
    if flip:
        return SetupFamily.LEGACY_FLIP
    if hidden:
        return SetupFamily.LEGACY_HIDDEN_DIVERGENCE
    if regular:
        return SetupFamily.LEGACY_REGULAR_DIVERGENCE
    if reclaim:
        return SetupFamily.LEGACY_AVWAP_RECLAIM
    return SetupFamily.NONE


class FiveToolEngine:
    """One stateful façade around the immutable transition state."""

    def __init__(self, settings: FiveToolSettings, *, state: FiveToolState | None = None) -> None:
        self.settings = settings
        self.state = state or FiveToolState.initial(settings)
        if self.state.settings_digest != settings.digest:
            raise FiveToolInputError("checkpoint settings digest does not match")
        if self.state.history_start_utc != settings.history_start_utc:
            raise FiveToolInputError("checkpoint history start does not match")

    def checkpoint(self) -> FiveToolState:
        return self.state

    def step(self, item: FiveToolBarInput) -> FiveToolTrace:
        """Consume one new, strictly increasing, closed primary bar."""

        state = self.state
        timestamp = item.primary.timestamp_utc
        if not state.observations and timestamp != self.settings.history_start_utc:
            raise FiveToolInputError(
                "first bar must exactly equal the pinned history_start_utc; "
                f"got {timestamp.isoformat()}"
            )
        if state.observations and timestamp <= state.observations[-1].primary.timestamp_utc:
            raise FiveToolInputError("primary bars must be strictly increasing without rewind")
        observations = (*state.observations, item)
        index = len(observations) - 1
        settings = self.settings

        opens = tuple(observation.primary.open for observation in observations)
        highs = tuple(observation.primary.high for observation in observations)
        lows = tuple(observation.primary.low for observation in observations)
        closes = tuple(observation.primary.close for observation in observations)
        volumes = tuple(observation.primary.volume for observation in observations)
        benchmark_closes = tuple(
            observation.benchmark.value if observation.benchmark is not None else None
            for observation in observations
        )
        lookback, enter_base, exit_base, confirmation_bars = _profile(
            settings, item.primary.interval
        )

        one_bar_returns: list[float | None] = [None]
        for previous, current in pairwise(closes):
            one_bar_returns.append(
                math.log(current / previous) if current > 0.0 and previous > 0.0 else None
            )
        window_return = (
            math.log(closes[-1] / closes[-1 - lookback])
            if len(closes) > lookback and closes[-1] > 0.0 and closes[-1 - lookback] > 0.0
            else None
        )
        realized_vol_series = tuple(
            None if value is None else value * math.sqrt(lookback)
            for value in pine_stdev(one_bar_returns, lookback)
        )
        squared_returns = tuple(None if value is None else value**2 for value in one_bar_returns)
        ewma_vol_series = tuple(
            None if value is None else math.sqrt(max(value * lookback, 0.0))
            for value in pine_rma(squared_returns, lookback)
        )
        atr_window_series = pine_atr(highs, lows, closes, lookback)
        atr_pct_vol_series = tuple(
            None if value is None or close <= 0.0 else value / close * math.sqrt(lookback)
            for value, close in zip(atr_window_series, closes, strict=True)
        )
        selected_vol_series = {
            "EWMA": ewma_vol_series,
            "ATR%": atr_pct_vol_series,
        }.get(settings.text("vol_model"), realized_vol_series)
        selected_vol = _last(selected_vol_series)
        vol_percentile = _last(
            pine_percentrank(selected_vol_series, settings.integer("vol_percentile_len"))
        )
        vol_factor = 1.0
        if settings.boolean("use_vol_percentile_adjustment") and vol_percentile is not None:
            vol_factor = max(
                0.50,
                1.0 + ((vol_percentile - 50.0) / 100.0) * settings.number("percentile_sensitivity"),
            )
        enter_z = max(0.10, enter_base * vol_factor)
        exit_z = max(0.05, min(exit_base * vol_factor, enter_z - 0.05))
        regime_z = (
            window_return / selected_vol
            if window_return is not None and selected_vol is not None and selected_vol > 0.0
            else None
        )

        ema_filter = _last(pine_ema(closes, settings.integer("ema_filter_len")))
        bull_trend_ok = not settings.boolean("use_ema_filter") or (
            ema_filter is not None and closes[-1] > ema_filter
        )
        bear_trend_ok = not settings.boolean("use_ema_filter") or (
            ema_filter is not None and closes[-1] < ema_filter
        )
        _, _, adx_series = pine_dmi(
            highs,
            lows,
            closes,
            settings.integer("adx_len"),
            settings.integer("adx_smoothing"),
        )
        adx_value = _last(adx_series)
        absolute_changes: tuple[float | None, ...] = (
            None,
            *(abs(current - previous) for previous, current in pairwise(closes)),
        )
        er_length = settings.integer("er_len")
        er_den_sma = _last(pine_sma(absolute_changes, er_length))
        er_numerator = abs(closes[-1] - closes[-1 - er_length]) if len(closes) > er_length else None
        er_denominator = er_den_sma * er_length if er_den_sma is not None else None
        efficiency_ratio = _safe_ratio(er_numerator, er_denominator)
        adx_ok = adx_value is not None and adx_value >= settings.number("adx_threshold")
        er_ok = efficiency_ratio is not None and efficiency_ratio >= settings.number("er_threshold")
        trend_quality_ok = _quality_gate(settings.text("strength_filter"), adx_ok, er_ok)

        previous_core = state.confirmed_core if state.confirmed_core is not None else 0
        raw_enter = 0
        if regime_z is not None and regime_z > enter_z and bull_trend_ok:
            raw_enter = 1
        elif regime_z is not None and regime_z < -enter_z and bear_trend_ok:
            raw_enter = 2
        raw_hysteresis = raw_enter
        if settings.boolean("use_hysteresis"):
            if previous_core == 1 and regime_z is not None and regime_z > exit_z and bull_trend_ok:
                raw_hysteresis = 1
            elif (
                previous_core == 2 and regime_z is not None and regime_z < -exit_z and bear_trend_ok
            ):
                raw_hysteresis = 2
        candidate: int | None = raw_hysteresis if regime_z is not None else None
        if candidate not in {None, 0} and not trend_quality_ok:
            candidate = 0
        gap_atr = _last(pine_atr(highs, lows, closes, settings.integer("gap_atr_len")))
        gap_absolute = abs(opens[-1] - closes[-2]) if len(closes) > 1 else None
        gap_shock = (
            gap_absolute is not None
            and gap_atr is not None
            and gap_absolute > settings.number("gap_atr_mult") * gap_atr
        )
        gap_mode = settings.text("gap_guard_mode")
        if (
            gap_mode != "Off"
            and gap_shock
            and candidate is not None
            and (
                gap_mode == "Force Neutral"
                or (gap_mode == "Neutralize new flips" and candidate != previous_core)
            )
        ):
            candidate = 0
        candidates = (*state.candidate_regimes, candidate)
        candidate_held = (
            candidate is not None
            and len(candidates) >= confirmation_bars
            and all(value == candidate for value in candidates[-confirmation_bars:])
        )
        confirmed_core = candidate if candidate_held else state.confirmed_core
        internal_regime = (
            None
            if confirmed_core is None
            else 1
            if confirmed_core == 1
            else -1
            if confirmed_core == 2
            else 0
        )
        internal_strength = (
            min(100.0, abs(regime_z) / max(enter_z * 1.5, 0.0001) * 100.0)
            if regime_z is not None
            else None
        )

        external_in_set = item.external_regime in {-1.0, 0.0, 1.0}
        external_ok_run = min(state.external_ok_run + 1, 10_000) if external_in_set else 0
        external_bad_run = 0 if external_in_set else state.external_bad_run + 1
        external_latched = state.external_latched
        if settings.boolean("use_external") and external_ok_run >= 20:
            external_latched = True
        if external_bad_run > 5:
            external_latched = False
        external_last = (
            int(item.external_regime)
            if external_in_set and item.external_regime is not None
            else state.external_last
        )
        use_external = settings.boolean("use_external")
        external_live = use_external and external_latched
        regime = external_last if external_live else None if use_external else internal_regime
        regime_first_seen = not state.have_regime and regime is not None
        regime_flip = state.have_regime and regime is not None and regime != state.last_regime
        regime_anchor = regime_first_seen or regime_flip
        flip_to_bull = regime_flip and regime == 1
        flip_to_bear = regime_flip and regime == -1
        last_regime = regime if regime is not None else state.last_regime
        have_regime = state.have_regime or regime is not None

        dwell_bull = state.dwell_bull
        dwell_neutral = state.dwell_neutral
        dwell_bear = state.dwell_bear
        if (
            settings.boolean("use_dwell_tracking")
            and regime_flip
            and state.active_bars_in_regime > 0
            and state.previous_selected_regime is not None
        ):
            prior_index = _markov_index(state.previous_selected_regime)
            if prior_index == 0:
                dwell_bull = (*dwell_bull, state.active_bars_in_regime)[-120:]
            elif prior_index == 1:
                dwell_neutral = (*dwell_neutral, state.active_bars_in_regime)[-120:]
            elif prior_index == 2:
                dwell_bear = (*dwell_bear, state.active_bars_in_regime)[-120:]
        active_age = (
            0 if regime is None else 1 if regime_anchor else state.active_bars_in_regime + 1
        )

        markov_counts = list(state.markov_counts)
        markov_rows = list(state.markov_rows)
        markov_last_regime = state.markov_last_regime
        markov_last_bar = state.markov_last_bar_index
        markov_stride = (
            lookback if settings.text("markov_sampling") == "Non-overlap lookback" else 1
        )
        if regime is None:
            markov_last_regime = None
            markov_last_bar = None
        elif markov_last_bar is None or index - markov_last_bar >= markov_stride:
            if markov_last_regime is not None:
                from_index = _markov_index(markov_last_regime)
                to_index = _markov_index(regime)
                if from_index is not None and to_index is not None:
                    markov_counts[from_index * 3 + to_index] += 1
                    markov_rows[from_index] += 1
            markov_last_regime = regime
            markov_last_bar = index
        current_markov_index = _markov_index(regime)
        markov_n = markov_rows[current_markov_index] if current_markov_index is not None else 0
        stay_count = (
            markov_counts[current_markov_index * 3 + current_markov_index]
            if current_markov_index is not None
            else 0
        )
        markov_p_stay = stay_count / markov_n if markov_n > 0 else None
        mean_dwell_samples = (
            1.0 / max(1.0 - markov_p_stay, 0.0001)
            if markov_p_stay is not None and markov_p_stay < 0.999
            else None
        )
        mean_dwell_bars = (
            mean_dwell_samples * markov_stride if mean_dwell_samples is not None else None
        )
        markov_maturity = (
            active_age / mean_dwell_bars
            if mean_dwell_bars is not None and mean_dwell_bars > 0.0
            else None
        )
        estimator = settings.text("mk_estimator")
        if estimator == "Wilson lower bound":
            markov_gate_probability = _wilson_lower(stay_count, markov_n)
        elif estimator == "Laplace smoothed":
            alpha = settings.number("mk_laplace_alpha")
            markov_gate_probability = (stay_count + alpha) / (markov_n + 3.0 * alpha)
        else:
            markov_gate_probability = markov_p_stay
        dwell_history = (
            dwell_bull
            if current_markov_index == 0
            else dwell_neutral
            if current_markov_index == 1
            else dwell_bear
        )
        dwell_percentile = (
            _dwell_percentile(dwell_history, active_age)
            if current_markov_index is not None and settings.boolean("use_dwell_tracking")
            else None
        )

        external_strength_valid = (
            settings.boolean("use_ext_strength")
            and external_live
            and item.external_strength is not None
            and 0.0 <= item.external_strength <= 100.0
        )
        if use_external:
            if settings.boolean("use_ext_strength"):
                strength = item.external_strength if external_strength_valid else None
            else:
                strength = internal_strength if internal_regime == regime else None
        else:
            strength = internal_strength
        playbook_quality_ok = _quality_gate(settings.text("playbook_quality_filter"), adx_ok, er_ok)
        playbook_strength_ok = strength is not None and strength >= settings.number(
            "min_strength_for_bias"
        )
        playbook_stability_ok = active_age >= settings.integer("min_bars_for_bias")
        playbook_gap_ok = not settings.boolean("block_entries_on_gap") or not gap_shock
        fresh_extension_block = (
            settings.boolean("use_fresh_overextension_guard")
            and strength is not None
            and strength > settings.number("max_strength_for_fresh_bias")
            and active_age <= settings.integer("fresh_bias_guard_bars")
        )
        entry_gates_ok = (
            playbook_quality_ok
            and playbook_strength_ok
            and playbook_stability_ok
            and playbook_gap_ok
            and not fresh_extension_block
        )
        extension_risk = (
            confirmed_core not in {None, 0}
            and internal_strength is not None
            and internal_strength >= settings.number("extension_strength_threshold")
            and active_age > lookback
        )
        extension_active = extension_risk and (
            not use_external or (internal_regime is not None and internal_regime == regime)
        )
        chop_risk = 0 if adx_ok and er_ok else 1 if adx_ok or er_ok else 2
        risk_scale = (1.0 if chop_risk == 0 else 0.75 if chop_risk == 1 else 0.45) * (
            0.70 if extension_active else 1.0
        )

        rs_ratio_series: tuple[float | None, ...] = tuple(
            close / benchmark if benchmark is not None and benchmark > 0.0 and close > 0.0 else None
            for close, benchmark in zip(closes, benchmark_closes, strict=True)
        )
        rs_ma_series = pine_sma(rs_ratio_series, settings.integer("mans_len"))
        mans_series: tuple[float | None, ...] = tuple(
            (ratio / average - 1.0) * 100.0
            if ratio is not None and average is not None and average != 0.0
            else None
            for ratio, average in zip(rs_ratio_series, rs_ma_series, strict=True)
        )
        mansfield = _last(mans_series)
        slope_length = settings.integer("rs_slope_len")
        mans_rising = (
            mansfield is not None
            and len(mans_series) > slope_length
            and mans_series[-1 - slope_length] is not None
            and mansfield > mans_series[-1 - slope_length]  # type: ignore[operator]
        )
        rs_leader = mansfield is not None and mansfield > 0.0 and mans_rising
        rs_laggard = mansfield is not None and mansfield < 0.0 and not mans_rising
        rs_ratio = _last(rs_ratio_series)
        rs_high_previous = _previous_extreme(rs_ratio_series, settings.integer("nh_len"), "high")
        rs_low_previous = _previous_extreme(rs_ratio_series, settings.integer("nh_len"), "low")
        rs_new_high = (
            rs_ratio is not None and rs_high_previous is not None and rs_ratio > rs_high_previous
        )
        rs_new_low = (
            rs_ratio is not None and rs_low_previous is not None and rs_ratio < rs_low_previous
        )
        rs_mode = settings.text("rs_mode")
        rs_long_ok = (
            True
            if rs_mode == "Off"
            else mansfield is not None and mansfield > 0.0
            if rs_mode == "Above zero"
            else rs_leader
        )
        rs_short_ok = (
            True
            if rs_mode == "Off"
            else mansfield is not None and mansfield < 0.0
            if rs_mode == "Above zero"
            else rs_laggard
        )
        if settings.boolean("veto_laggard"):
            rs_long_ok = rs_long_ok and not rs_laggard
            rs_short_ok = rs_short_ok and not rs_leader

        benchmark_ema = _last(pine_ema(benchmark_closes, settings.integer("benchmark_filter_len")))
        benchmark_close = _last(benchmark_closes)
        benchmark_long_ok = not settings.boolean("use_benchmark_filter") or (
            benchmark_close is not None
            and benchmark_ema is not None
            and benchmark_close > benchmark_ema
        )
        benchmark_short_ok = not settings.boolean("use_benchmark_filter") or (
            benchmark_close is not None
            and benchmark_ema is not None
            and benchmark_close < benchmark_ema
        )
        htf_long_ok = not settings.boolean("use_htf_filter") or (
            item.htf_close is not None
            and item.htf_ema is not None
            and item.htf_close.value > item.htf_ema.value
        )
        htf_short_ok = not settings.boolean("use_htf_filter") or (
            item.htf_close is not None
            and item.htf_ema is not None
            and item.htf_close.value < item.htf_ema.value
        )

        oscillator_series = (
            pine_mfi(highs, lows, closes, volumes, settings.integer("osc_len"))
            if settings.text("osc_type") == "MFI"
            else pine_rsi(closes, settings.integer("osc_len"))
        )
        pivot_left = settings.integer("piv_l")
        pivot_right = settings.integer("piv_r")
        low_pivot = confirmed_pivot(
            oscillator_series, left=pivot_left, right=pivot_right, kind="low"
        )
        high_pivot = confirmed_pivot(
            oscillator_series, left=pivot_left, right=pivot_right, kind="high"
        )
        regular_bull = hidden_bull = regular_bear = hidden_bear = False
        previous_pivot_low = state.previous_pivot_low
        previous_pivot_high = state.previous_pivot_high
        if low_pivot is not None:
            pivot_index, oscillator_value = low_pivot
            price_value = lows[pivot_index]
            if previous_pivot_low is not None:
                previous_index, previous_oscillator, previous_price = previous_pivot_low
                gap = pivot_index - previous_index
                in_range = settings.integer("min_gap") <= gap <= settings.integer("max_gap")
                regular_bull = (
                    in_range
                    and price_value < previous_price
                    and oscillator_value > previous_oscillator
                )
                hidden_bull = (
                    in_range
                    and price_value > previous_price
                    and oscillator_value < previous_oscillator
                )
            previous_pivot_low = (pivot_index, oscillator_value, price_value)
        if high_pivot is not None:
            pivot_index, oscillator_value = high_pivot
            price_value = highs[pivot_index]
            if previous_pivot_high is not None:
                previous_index, previous_oscillator, previous_price = previous_pivot_high
                gap = pivot_index - previous_index
                in_range = settings.integer("min_gap") <= gap <= settings.integer("max_gap")
                regular_bear = (
                    in_range
                    and price_value > previous_price
                    and oscillator_value < previous_oscillator
                )
                hidden_bear = (
                    in_range
                    and price_value < previous_price
                    and oscillator_value > previous_oscillator
                )
            previous_pivot_high = (pivot_index, oscillator_value, price_value)

        atr_values = pine_atr(highs, lows, closes, settings.integer("atr_len"))
        atr_value = _last(atr_values)
        vwap_source_name = settings.text("vwap_src")
        if vwap_source_name == "hlc3":
            vwap_source = (highs[-1] + lows[-1] + closes[-1]) / 3.0
        elif vwap_source_name == "close":
            vwap_source = closes[-1]
        else:
            raise FiveToolInputError(
                "external input.source AVWAP values require an explicit supplied series"
            )
        previous_vwap = (
            state.avwap_pv / state.avwap_weight
            if state.avwap_on
            and state.avwap_pv is not None
            and state.avwap_weight is not None
            and state.avwap_weight > 0.0
            else None
        )
        stale_reset = (
            settings.boolean("use_avwap_stale_guard")
            and state.avwap_on
            and state.avwap_age >= settings.integer("avwap_stale_min_bars")
            and previous_vwap is not None
            and atr_value is not None
            and atr_value > 0.0
            and abs(vwap_source - previous_vwap)
            > settings.number("avwap_stale_atr_mult") * atr_value
        )
        avwap_reset = regime_anchor or stale_reset
        avwap_pv = 0.0 if avwap_reset else state.avwap_pv
        avwap_weight = 0.0 if avwap_reset else state.avwap_weight
        avwap_p2v = 0.0 if avwap_reset else state.avwap_p2v
        avwap_on = state.avwap_on or avwap_reset
        if avwap_on:
            weight = (
                volumes[-1]
                if settings.text("vwap_weighting") == "Volume" and volumes[-1] > 0.0
                else 0.0
                if settings.text("vwap_weighting") == "Volume"
                else 1.0
            )
            if weight > 0.0:
                avwap_pv = (avwap_pv or 0.0) + vwap_source * weight
                avwap_weight = (avwap_weight or 0.0) + weight
                avwap_p2v = (avwap_p2v or 0.0) + vwap_source**2 * weight
        flip_vwap = (
            avwap_pv / avwap_weight
            if avwap_on and avwap_pv is not None and avwap_weight is not None and avwap_weight > 0.0
            else None
        )
        avwap_variance = (
            max(avwap_p2v / avwap_weight - flip_vwap**2, 0.0)
            if flip_vwap is not None
            and avwap_p2v is not None
            and avwap_weight is not None
            and avwap_weight > 0.0
            else None
        )
        flip_sd = math.sqrt(avwap_variance) if avwap_variance is not None else None
        avwap_dead = avwap_on and settings.text("vwap_weighting") == "Volume" and flip_vwap is None
        avwap_entries_ok = not settings.boolean("block_entries_on_avwap_dead") or not avwap_dead
        above_avwap = flip_vwap is not None and closes[-1] > flip_vwap
        below_avwap = flip_vwap is not None and closes[-1] < flip_vwap
        value_zone_long = (
            flip_vwap is not None
            and flip_sd is not None
            and flip_vwap - flip_sd <= closes[-1] <= flip_vwap
        )
        value_zone_short = (
            flip_vwap is not None
            and flip_sd is not None
            and flip_vwap <= closes[-1] <= flip_vwap + flip_sd
        )
        reclaim_up = (
            flip_vwap is not None
            and previous_vwap is not None
            and len(closes) > 1
            and closes[-1] > flip_vwap
            and closes[-2] <= previous_vwap
        )
        reclaim_down = (
            flip_vwap is not None
            and previous_vwap is not None
            and len(closes) > 1
            and closes[-1] < flip_vwap
            and closes[-2] >= previous_vwap
        )
        avwap_age = 0 if not avwap_on else 1 if avwap_reset else state.avwap_age + 1

        long_flip_trigger = settings.boolean("trig_flip") and flip_to_bull
        long_hidden_trigger = settings.boolean("trig_hidden") and hidden_bull and above_avwap
        long_regular_trigger = (
            settings.boolean("trig_regular")
            and regular_bull
            and (not settings.boolean("use_value_zone") or value_zone_long)
        )
        long_reclaim_trigger = settings.boolean("trig_reclaim") and reclaim_up and rs_leader
        short_flip_trigger = settings.boolean("trig_flip") and flip_to_bear
        short_hidden_trigger = settings.boolean("trig_hidden") and hidden_bear and below_avwap
        short_regular_trigger = (
            settings.boolean("trig_regular")
            and regular_bear
            and (not settings.boolean("use_value_zone") or value_zone_short)
        )
        short_reclaim_trigger = settings.boolean("trig_reclaim") and reclaim_down and rs_laggard
        legacy_long_trigger = any(
            (long_flip_trigger, long_hidden_trigger, long_regular_trigger, long_reclaim_trigger)
        )
        legacy_short_trigger = any(
            (
                short_flip_trigger,
                short_hidden_trigger,
                short_regular_trigger,
                short_reclaim_trigger,
            )
        )

        long_trigger_points = (
            25.0
            if long_flip_trigger or long_hidden_trigger
            else 20.0
            if long_regular_trigger
            else 15.0
            if long_reclaim_trigger
            else 0.0
        )
        short_trigger_points = (
            25.0
            if short_flip_trigger or short_hidden_trigger
            else 20.0
            if short_regular_trigger
            else 15.0
            if short_reclaim_trigger
            else 0.0
        )
        strength_points = (
            10.0
            if strength is not None and strength >= settings.number("min_strength_for_bias")
            else 0.0
        )
        long_score = min(
            100.0,
            long_trigger_points
            + (
                25.0
                if rs_leader
                else 15.0
                if mansfield is not None and mansfield > 0
                else 10.0
                if mans_rising
                else 0.0
            )
            + (15.0 if above_avwap else 10.0 if value_zone_long else 0.0)
            + (10.0 if rs_new_high else 0.0)
            + strength_points,
        )
        short_score = min(
            100.0,
            short_trigger_points
            + (25.0 if rs_laggard else 15.0 if mansfield is not None and mansfield < 0 else 0.0)
            + (15.0 if below_avwap else 10.0 if value_zone_short else 0.0)
            + (10.0 if rs_new_low else 0.0)
            + strength_points,
        )

        atr_percent = (
            atr_value / closes[-1] * 100.0 if atr_value is not None and closes[-1] > 0.0 else None
        )
        atr_percent_ok = not settings.boolean("use_atr_pct_filter") or (
            atr_percent is not None
            and settings.number("min_atr_pct") <= atr_percent <= settings.number("max_atr_pct")
        )
        dollar_volume_series: tuple[float | None, ...] = tuple(
            volume * close * settings.point_value if close > 0.0 else None
            for volume, close in zip(volumes, closes, strict=True)
        )
        dollar_volume_average = _last(pine_sma(dollar_volume_series, settings.integer("liq_len")))
        liquidity_ok = not settings.boolean("use_liquidity_filter") or (
            dollar_volume_average is not None
            and dollar_volume_average >= settings.number("min_dollar_volume")
        )

        equity = item.account.equity
        equity_history = (*state.equity_history, equity)
        equity_peak = equity if state.equity_peak is None else max(state.equity_peak, equity)
        peak_window = settings.integer("equity_peak_window")
        rolling_peak = max(equity_history[-peak_window:])
        halt_peak = (
            rolling_peak if settings.text("equity_halt_rearm") == "Rolling peak" else equity_peak
        )
        equity_drawdown = (halt_peak - equity) / halt_peak * 100.0 if halt_peak > 0.0 else 0.0
        equity_halt = settings.boolean("use_equity_dd_halt") and (
            equity_drawdown >= settings.number("max_equity_dd_for_entries")
        )
        new_day = state.day_session is None or item.primary.session_date != state.day_session
        day_start_equity = equity if new_day else state.day_start_equity
        daily_halt_latched = False if new_day else state.daily_halt_latched
        daily_drawdown = (
            (day_start_equity - equity) / day_start_equity * 100.0
            if day_start_equity is not None and day_start_equity > 0.0
            else 0.0
        )
        if settings.boolean("use_daily_loss_halt") and daily_drawdown >= settings.number(
            "daily_loss_halt_pct"
        ):
            daily_halt_latched = True

        transitioned_to_flat = (
            state.previous_position is not Side.FLAT and item.account.position is Side.FLAT
        )
        last_exit_index = index if transitioned_to_flat else state.last_exit_bar_index
        cooldown_ok = (
            settings.integer("cooldown_bars_after_trade") <= 0
            or last_exit_index is None
            or index - last_exit_index >= settings.integer("cooldown_bars_after_trade")
        )
        risk_halt = equity_halt or daily_halt_latched

        long_risk_halt = risk_halt
        short_risk_halt = risk_halt
        long_equity_history = state.long_equity_history
        short_equity_history = state.short_equity_history
        long_equity_peak = state.long_equity_peak
        short_equity_peak = state.short_equity_peak
        long_day_start = state.long_day_start_equity
        short_day_start = state.short_day_start_equity
        long_daily_latched = state.long_daily_halt_latched
        short_daily_latched = state.short_daily_halt_latched
        if settings.boolean("use_blended_capital_split"):
            if (
                item.account.long_virtual_equity is None
                or item.account.short_virtual_equity is None
            ):
                raise FiveToolInputError(
                    "blended capital requires explicit fill-attributed side equities"
                )
            long_equity = item.account.long_virtual_equity
            short_equity = item.account.short_virtual_equity
            long_equity_history = (*long_equity_history, long_equity)
            short_equity_history = (*short_equity_history, short_equity)
            long_equity_peak = (
                long_equity if long_equity_peak is None else max(long_equity_peak, long_equity)
            )
            short_equity_peak = (
                short_equity if short_equity_peak is None else max(short_equity_peak, short_equity)
            )
            long_reference = (
                max(long_equity_history[-peak_window:])
                if settings.text("equity_halt_rearm") == "Rolling peak"
                else long_equity_peak
            )
            short_reference = (
                max(short_equity_history[-peak_window:])
                if settings.text("equity_halt_rearm") == "Rolling peak"
                else short_equity_peak
            )
            long_drawdown = (
                (long_reference - long_equity) / long_reference * 100.0
                if long_reference > 0.0
                else 0.0
            )
            short_drawdown = (
                (short_reference - short_equity) / short_reference * 100.0
                if short_reference > 0.0
                else 0.0
            )
            if new_day:
                long_day_start = long_equity
                short_day_start = short_equity
                long_daily_latched = False
                short_daily_latched = False
            long_daily_drawdown = (
                (long_day_start - long_equity) / long_day_start * 100.0
                if long_day_start is not None and long_day_start > 0.0
                else 0.0
            )
            short_daily_drawdown = (
                (short_day_start - short_equity) / short_day_start * 100.0
                if short_day_start is not None and short_day_start > 0.0
                else 0.0
            )
            if settings.boolean("use_daily_loss_halt"):
                threshold = settings.number("daily_loss_halt_pct")
                long_daily_latched = long_daily_latched or long_daily_drawdown >= threshold
                short_daily_latched = short_daily_latched or short_daily_drawdown >= threshold
            long_risk_halt = (
                settings.boolean("use_equity_dd_halt")
                and long_drawdown >= settings.number("max_equity_dd_for_entries")
            ) or long_daily_latched
            short_risk_halt = (
                settings.boolean("use_equity_dd_halt")
                and short_drawdown >= settings.number("max_equity_dd_for_entries")
            ) or short_daily_latched

        common_pretrade = atr_percent_ok and liquidity_ok and avwap_entries_ok
        long_environment_ok = (
            not long_risk_halt
            and cooldown_ok
            and common_pretrade
            and benchmark_long_ok
            and htf_long_ok
        )
        short_environment_ok = (
            not short_risk_halt
            and cooldown_ok
            and common_pretrade
            and benchmark_short_ok
            and htf_short_ok
        )

        # A queued retest is consumed only when a position is actually observed,
        # not merely because the setup alerted.  This is the research-side repair
        # for Pine's order-disabled repeated-alert behavior.
        long_retest_seen = state.long_retest_seen
        short_retest_seen = state.short_retest_seen
        long_retest_taken = state.long_retest_taken
        short_retest_taken = state.short_retest_taken
        pending_side = state.pending_entry_side
        pending_setup = state.pending_entry_setup
        if state.previous_position is Side.FLAT and item.account.position is Side.LONG:
            if pending_side is Side.LONG and pending_setup is SetupFamily.BULL_RETEST:
                long_retest_taken = True
            pending_side = Side.FLAT
            pending_setup = SetupFamily.NONE
        elif state.previous_position is Side.FLAT and item.account.position is Side.SHORT:
            if pending_side is Side.SHORT and pending_setup is SetupFamily.BEAR_RETEST:
                short_retest_taken = True
            pending_side = Side.FLAT
            pending_setup = SetupFamily.NONE
        elif transitioned_to_flat:
            pending_side = Side.FLAT
            pending_setup = SetupFamily.NONE

        rw_below_zero = mansfield is not None and mansfield < 0.0
        rw_deteriorating = mansfield is not None and not mans_rising
        rw_leader_short = rw_below_zero and rw_deteriorating
        rw_new_low = rs_new_low and rw_below_zero
        short_rw_mode = settings.text("short_rw_mode")
        short_rw_ok_v2 = (
            rw_below_zero
            if short_rw_mode == "Mansfield below zero"
            else rw_new_low
            if short_rw_mode == "New RS low"
            else rw_leader_short
        ) and not rs_leader
        short_range = highs[-1] - lows[-1]
        short_upper_wick = highs[-1] - max(opens[-1], closes[-1])
        short_rejection = closes[-1] < opens[-1] or (
            short_range > 0.0
            and short_upper_wick / short_range >= settings.number("short_rejection_wick_frac")
        )
        short_near_supply = (
            flip_vwap is not None
            and atr_value is not None
            and atr_value > 0.0
            and highs[-1] >= flip_vwap - atr_value * settings.number("short_retest_tolerance_atr")
            and closes[-1] < flip_vwap
        )
        short_failed_reclaim = below_avwap and short_near_supply and short_rejection
        if flip_to_bear:
            short_retest_seen = False
            short_retest_taken = False
        elif (
            regime == -1
            and not short_retest_taken
            and (
                above_avwap
                or (
                    flip_vwap is not None
                    and atr_value is not None
                    and atr_value > 0.0
                    and abs(closes[-1] - flip_vwap)
                    <= settings.number("short_retest_tolerance_atr") * atr_value
                )
            )
        ):
            short_retest_seen = True
        short_bear_retest = (
            settings.boolean("short_plus_allow_bear_retest")
            and regime == -1
            and short_retest_seen
            and not short_retest_taken
            and below_avwap
            and short_near_supply
            and short_rejection
            and short_rw_ok_v2
        )
        short_flag_floor = _previous_extreme(
            tuple(float(value) for value in lows), settings.integer("short_flag_len"), "low"
        )
        short_breakdown = (
            settings.boolean("allow_short_breakdown_trigger")
            and below_avwap
            and short_flag_floor is not None
            and closes[-1] < short_flag_floor
            and short_rw_ok_v2
        )
        short_trigger_v2_base = (
            short_failed_reclaim or short_breakdown
            if settings.boolean("require_failed_reclaim_short")
            else short_failed_reclaim or short_breakdown or short_flip_trigger
        )
        short_trigger_v2 = short_trigger_v2_base or (
            settings.boolean("short_plus_enabled") and short_bear_retest
        )
        short_rsi = _last(pine_rsi(closes, settings.integer("short_rsi_len")))
        short_distance_avwap = (
            max(0.0, (flip_vwap - closes[-1]) / atr_value)
            if flip_vwap is not None and atr_value is not None and atr_value > 0.0
            else 0.0
        )
        down_count = sum(
            closes[position] < closes[position - 1]
            for position in range(
                max(1, len(closes) - settings.integer("short_down_bars_len")), len(closes)
            )
        )
        short_shelf_high = _current_extreme(highs, settings.integer("short_flag_len"), "high")
        short_structural_reference = (
            flip_vwap
            if flip_vwap is not None
            and short_shelf_high is not None
            and flip_vwap > short_shelf_high
            else short_shelf_high
        )
        short_structural_stop = (
            short_structural_reference + settings.number("short_stop_buffer_atr") * atr_value
            if short_structural_reference is not None and atr_value is not None
            else None
        )
        short_structural_distance = (
            short_structural_stop - closes[-1]
            if short_structural_stop is not None and short_structural_stop > closes[-1]
            else None
        )
        short_stop_distance = (
            short_structural_distance
            if settings.boolean("use_short_structural_stop")
            and short_structural_distance is not None
            else atr_value * settings.number("atr_mult")
            if atr_value is not None
            else None
        )
        short_stop_percent = (
            short_stop_distance / closes[-1] * 100.0
            if short_stop_distance is not None and closes[-1] > 0.0
            else None
        )
        short_no_chase = settings.boolean("use_short_no_chase_filter") and (
            short_distance_avwap >= settings.number("short_max_dist_below_avwap_atr")
            or (short_rsi is not None and short_rsi <= settings.number("short_oversold_rsi"))
            or down_count >= settings.integer("short_down_bars_len")
            or (
                short_stop_percent is not None
                and short_stop_percent > settings.number("short_max_stop_pct")
            )
        )
        short_support = _previous_extreme(
            tuple(float(value) for value in lows), settings.integer("short_support_len"), "low"
        )
        room_to_support = (
            (closes[-1] - short_support) / atr_value
            if short_support is not None
            and atr_value is not None
            and atr_value > 0.0
            and closes[-1] > short_support
            else None
        )
        short_support_block = (
            settings.boolean("use_short_support_filter")
            and room_to_support is not None
            and room_to_support <= settings.number("short_min_room_to_support_atr")
        )
        short_squeeze = settings.boolean("use_short_squeeze_filter") and (
            (
                atr_value is not None
                and closes[-1] > opens[-1]
                and closes[-1] - opens[-1] >= atr_value * settings.number("short_green_body_atr")
            )
            or (
                len(closes) > 1
                and atr_value is not None
                and opens[-1] - closes[-2] >= atr_value * settings.number("short_gap_up_atr")
            )
            or (atr_percent is not None and atr_percent >= settings.number("short_high_atr_pct"))
            or (mansfield is not None and mansfield < 0.0 and mans_rising)
        )
        short_markov_core = (
            regime == -1
            and markov_gate_probability is not None
            and markov_gate_probability * 100.0 >= settings.number("short_plus_min_stay_pct")
            and markov_n >= settings.integer("short_plus_min_markov_n")
            and active_age <= settings.integer("short_plus_max_regime_age")
            and (
                markov_maturity is None
                or markov_maturity <= settings.number("short_plus_max_maturity")
            )
            and (
                not settings.boolean("short_plus_use_dwell_gate")
                or dwell_percentile is None
                or dwell_percentile <= settings.number("short_plus_max_dwell_pctile")
            )
        )
        short_markov_gate = not settings.boolean("short_plus_strict_markov") or short_markov_core
        short_sector_laggard = rw_new_low and below_avwap
        short_primary_setup = short_failed_reclaim or short_breakdown or short_bear_retest
        short_secondary_setup = short_sector_laggard and (
            short_primary_setup or short_trigger_v2_base
        )
        short_plus_setup_ok = short_primary_setup or (
            not settings.boolean("short_plus_primary_only") and short_secondary_setup
        )
        short_session_ok = not settings.boolean("short_plus_session_filter") or (
            item.short_plus_in_session is True
        )
        atr_safe = max(atr_value or 0.0, settings.minimum_tick)
        short_range_atr = short_range / atr_safe
        short_range_ok = short_range_atr <= settings.number("short_plus_max_trigger_range_atr")
        short_close_location = 0.5 if short_range == 0.0 else (closes[-1] - lows[-1]) / short_range
        short_close_ok = not settings.boolean("short_plus_lower_close_on") or (
            short_close_location <= settings.number("short_plus_max_close_location")
        )
        short_volume_ma = _last(
            pine_sma(
                tuple(float(value) for value in volumes), settings.integer("short_plus_volume_len")
            )
        )
        # Intentional Pine compatibility: the enabled volume requirement fails
        # open while its moving average is unavailable.
        short_volume_ok = not settings.boolean("short_plus_volume_filter") or (
            short_volume_ma is None
            or volumes[-1] >= short_volume_ma * settings.number("short_plus_volume_mult")
        )
        short_continuation_score = min(
            100.0,
            (32.0 if short_markov_core else 0.0)
            + (22.0 if short_breakdown else 0.0)
            + (22.0 if short_bear_retest else 0.0)
            + (20.0 if short_failed_reclaim else 0.0)
            + (8.0 if short_sector_laggard else 0.0)
            + (8.0 if below_avwap else 0.0)
            + (8.0 if not short_no_chase else 0.0)
            + (6.0 if not short_support_block else 0.0)
            + (6.0 if not short_squeeze else 0.0)
            + (5.0 if short_range_ok else 0.0)
            + (5.0 if short_close_ok else 0.0)
            + (5.0 if short_volume_ok else 0.0),
        )
        short_plus_core = (
            short_markov_gate
            and short_plus_setup_ok
            and short_session_ok
            and short_range_ok
            and short_close_ok
            and short_volume_ok
            and short_continuation_score >= settings.integer("short_plus_min_cont_score")
        )
        short_plus_pass = (
            not settings.boolean("short_plus_enabled")
            or not settings.boolean("use_short_side_v2")
            or short_plus_core
        )
        short_permission_v2 = regime == -1 and entry_gates_ok and short_environment_ok
        short_block_v2 = chop_risk == 2 or short_no_chase or short_support_block or short_squeeze
        short_review_v2 = short_permission_v2 and short_rw_ok_v2 and not short_block_v2
        short_v2_score = min(
            100.0,
            (
                45.0
                if short_failed_reclaim
                else 32.0
                if short_breakdown
                else 36.0
                if short_bear_retest
                else 15.0
                if short_flip_trigger
                else 0.0
            )
            + (35.0 if rw_new_low else 25.0 if rw_leader_short else 14.0 if rw_below_zero else 0.0)
            + (20.0 if below_avwap else 0.0),
        )
        short_boost = (
            settings.number("short_plus_score_boost")
            if settings.boolean("short_plus_enabled")
            and settings.boolean("use_short_side_v2")
            and settings.boolean("short_plus_score_boost_on")
            and short_plus_core
            else 0.0
        )
        active_short_score = min(
            100.0,
            (short_v2_score if settings.boolean("use_short_side_v2") else short_score)
            + short_boost,
        )
        active_short_trigger = (
            short_trigger_v2 if settings.boolean("use_short_side_v2") else legacy_short_trigger
        )
        active_short_review = (
            short_review_v2 and short_plus_pass
            if settings.boolean("use_short_side_v2")
            else entry_gates_ok and rs_short_ok and short_environment_ok
        )

        long_rs_above_zero = mansfield is not None and mansfield > 0.0
        long_rs_mode = settings.text("long_rs_mode")
        long_rs_ok_v2 = (
            long_rs_above_zero
            if long_rs_mode == "Mansfield above zero"
            else rs_new_high and long_rs_above_zero
            if long_rs_mode == "New RS high"
            else rs_leader
        ) and not rs_laggard
        pullback_support = _previous_extreme(
            tuple(float(value) for value in lows), settings.integer("long_pullback_lookback"), "low"
        )
        base_support = _previous_extreme(
            tuple(float(value) for value in lows), settings.integer("long_base_lookback"), "low"
        )
        base_pivot = _previous_extreme(
            tuple(float(value) for value in highs), settings.integer("long_base_lookback"), "high"
        )
        previous_high = _previous_extreme(
            tuple(float(value) for value in highs), settings.integer("long_base_lookback"), "high"
        )
        previous_low = _previous_extreme(
            tuple(float(value) for value in lows), settings.integer("long_base_lookback"), "low"
        )
        base_range_percent = (
            (previous_high - previous_low) / closes[-1] * 100.0
            if previous_high is not None and previous_low is not None and closes[-1] > 0.0
            else None
        )
        base_tight = base_range_percent is not None and base_range_percent <= settings.number(
            "long_base_tightness_max_pct"
        )
        if flip_to_bull:
            long_retest_seen = False
            long_retest_taken = False
        elif (
            regime == 1
            and not long_retest_taken
            and (
                below_avwap
                or (
                    flip_vwap is not None
                    and atr_value is not None
                    and atr_value > 0.0
                    and abs(closes[-1] - flip_vwap)
                    <= settings.number("long_pullback_tolerance_atr") * atr_value
                )
            )
        ):
            long_retest_seen = True
        long_range = highs[-1] - lows[-1]
        lower_wick = min(opens[-1], closes[-1]) - lows[-1]
        defense_candle = closes[-1] > opens[-1] or (
            long_range > 0.0 and lower_wick / long_range >= 0.5
        )
        pullback_defended = (
            pullback_support is not None
            and atr_value is not None
            and atr_value > 0.0
            and lows[-1]
            <= pullback_support + settings.number("long_pullback_tolerance_atr") * atr_value
            and closes[-1]
            >= pullback_support - settings.number("long_pullback_tolerance_atr") * atr_value
        )
        previous_above_avwap = (
            previous_vwap is not None and len(closes) > 1 and closes[-2] > previous_vwap
        )
        leader_pullback = (
            regime == 1
            and (
                long_rs_ok_v2 if settings.boolean("require_leader_pullback") else long_rs_above_zero
            )
            and previous_above_avwap
            and pullback_defended
            and defense_candle
        )
        bull_retest = (
            settings.boolean("allow_bull_retest_trigger")
            and regime == 1
            and long_retest_seen
            and not long_retest_taken
            and (
                reclaim_up
                or (len(closes) > 1 and closes[-1] > opens[-1] and closes[-1] > closes[-2])
            )
            and (long_rs_above_zero or mans_rising)
        )
        long_avwap_reclaim = (
            settings.boolean("allow_avwap_reclaim_trigger")
            and regime == 1
            and reclaim_up
            and (long_rs_above_zero or mans_rising or rs_leader or rs_new_high)
        )
        long_base_breakout = (
            settings.boolean("allow_base_breakout_trigger")
            and regime == 1
            and (long_rs_ok_v2 or rs_new_high)
            and base_tight
            and base_pivot is not None
            and closes[-1] > base_pivot
            and (
                flip_vwap is None
                or atr_value is None
                or atr_value <= 0.0
                or (closes[-1] - flip_vwap) / atr_value
                <= settings.number("long_max_dist_above_avwap_atr")
            )
        )
        long_trigger_v2 = leader_pullback or bull_retest or long_avwap_reclaim or long_base_breakout
        long_support = base_support if long_base_breakout else pullback_support
        long_structural_reference = (
            flip_vwap
            if flip_vwap is not None and long_support is not None and flip_vwap < long_support
            else long_support
        )
        long_structural_stop = (
            long_structural_reference - settings.number("long_stop_buffer_atr") * atr_value
            if long_structural_reference is not None and atr_value is not None
            else None
        )
        long_structural_distance = (
            closes[-1] - long_structural_stop
            if long_structural_stop is not None and closes[-1] > long_structural_stop
            else None
        )
        long_stop_distance = (
            long_structural_distance
            if settings.boolean("use_long_structural_stop") and long_structural_distance is not None
            else atr_value * settings.number("atr_mult")
            if atr_value is not None
            else None
        )
        long_stop_percent = (
            long_stop_distance / closes[-1] * 100.0
            if long_stop_distance is not None and closes[-1] > 0.0
            else None
        )
        long_rsi = _last(pine_rsi(closes, settings.integer("long_rsi_len")))
        long_distance_avwap = (
            max(0.0, (closes[-1] - flip_vwap) / atr_value)
            if flip_vwap is not None and atr_value is not None and atr_value > 0.0
            else 0.0
        )
        up_count = sum(
            closes[position] > closes[position - 1]
            for position in range(
                max(1, len(closes) - settings.integer("long_up_bars_len")), len(closes)
            )
        )
        long_no_chase = settings.boolean("use_long_no_chase_filter") and (
            long_distance_avwap >= settings.number("long_max_dist_above_avwap_atr")
            or (long_rsi is not None and long_rsi >= settings.number("long_overbought_rsi"))
            or up_count >= settings.integer("long_up_bars_len")
            or (
                long_stop_percent is not None
                and long_stop_percent > settings.number("long_max_stop_pct")
            )
        )
        resistance = _previous_extreme(
            tuple(float(value) for value in highs),
            settings.integer("long_resistance_lookback"),
            "high",
        )
        room_to_resistance = (
            (resistance - closes[-1]) / atr_value
            if resistance is not None
            and atr_value is not None
            and atr_value > 0.0
            and resistance > closes[-1]
            else None
        )
        resistance_applies = (
            resistance is not None and base_pivot is not None and resistance > base_pivot
            if long_base_breakout
            else True
        )
        resistance_block = (
            settings.boolean("use_long_resistance_filter")
            and resistance_applies
            and room_to_resistance is not None
            and room_to_resistance <= settings.number("long_min_room_to_resistance_atr")
        )
        exhaustion_block = settings.boolean("use_long_exhaustion_filter") and (
            len(closes) > 1
            and atr_value is not None
            and opens[-1] - closes[-2] >= settings.number("long_gap_up_atr") * atr_value
            and atr_percent is not None
            and atr_percent >= settings.number("long_exhaustion_atr_pct")
        )
        long_permission_v2 = regime == 1 and entry_gates_ok and long_environment_ok
        long_block_v2 = chop_risk == 2 or long_no_chase or resistance_block or exhaustion_block
        long_review_v2 = long_permission_v2 and long_rs_ok_v2 and not long_block_v2
        long_v2_score = min(
            100.0,
            (
                40.0
                if leader_pullback
                else 35.0
                if bull_retest
                else 30.0
                if long_avwap_reclaim
                else 40.0
                if long_base_breakout
                else 0.0
            )
            + (30.0 if rs_leader else 18.0 if long_rs_above_zero else 0.0)
            + (15.0 if above_avwap else 0.0)
            + (15.0 if rs_new_high else 0.0),
        )
        long_markov_core = (
            regime == 1
            and markov_gate_probability is not None
            and markov_gate_probability * 100.0 >= settings.number("long_plus_min_stay_pct")
            and markov_n >= settings.integer("long_plus_min_markov_n")
            # Pine prose calls age optional, but strict_markov unconditionally
            # contains both age and maturity.  Preserve source behavior.
            and active_age <= settings.integer("long_plus_max_regime_age")
            and (
                markov_maturity is None
                or markov_maturity <= settings.number("long_plus_max_maturity")
            )
            and (
                not settings.boolean("long_plus_use_dwell_gate")
                or dwell_percentile is None
                or dwell_percentile <= settings.number("long_plus_max_dwell_pctile")
            )
        )
        long_markov_gate = not settings.boolean("long_plus_strict_markov") or long_markov_core
        long_session_ok = not settings.boolean("long_plus_session_filter") or (
            item.long_plus_in_session is True
        )
        long_range_atr = long_range / atr_safe
        long_range_ok = long_range_atr <= settings.number("long_plus_max_trigger_range_atr")
        long_close_location = 0.5 if long_range == 0.0 else (closes[-1] - lows[-1]) / long_range
        long_close_ok = not settings.boolean("long_plus_upper_close_on") or (
            long_close_location >= settings.number("long_plus_min_close_location")
        )
        long_volume_ma = _last(
            pine_sma(
                tuple(float(value) for value in volumes), settings.integer("long_plus_volume_len")
            )
        )
        long_volume_ok = not settings.boolean("long_plus_volume_filter") or (
            long_volume_ma is None
            or volumes[-1] >= long_volume_ma * settings.number("long_plus_volume_mult")
        )
        long_plus_core = (
            long_markov_gate
            and long_session_ok
            and long_range_ok
            and long_close_ok
            and long_volume_ok
        )
        long_plus_pass = (
            not settings.boolean("long_plus_enabled")
            or not settings.boolean("use_long_side_v2")
            or long_plus_core
        )
        active_long_score = long_v2_score if settings.boolean("use_long_side_v2") else long_score
        active_long_trigger = (
            long_trigger_v2 if settings.boolean("use_long_side_v2") else legacy_long_trigger
        )
        active_long_review = (
            long_review_v2 and long_plus_pass
            if settings.boolean("use_long_side_v2")
            else entry_gates_ok and rs_long_ok and long_environment_ok
        )

        in_test_window = True
        if settings.boolean("use_date_filter"):
            in_test_window = (
                _parse_pine_timestamp(settings.text("test_start"))
                <= timestamp
                <= _parse_pine_timestamp(settings.text("test_end"))
            )
        is_flat = item.account.position is Side.FLAT
        can_long = (
            in_test_window
            and regime == 1
            and active_long_review
            and active_long_trigger
            and active_long_score >= settings.integer("min_score")
            and is_flat
        )
        can_short = (
            in_test_window
            and settings.boolean("allow_shorts")
            and regime == -1
            and active_short_review
            and active_short_trigger
            and active_short_score >= settings.integer("min_score")
            and is_flat
        )
        long_setup = _select_setup(
            flip=long_flip_trigger,
            hidden=long_hidden_trigger,
            regular=long_regular_trigger,
            reclaim=long_reclaim_trigger,
            long_v2=(leader_pullback, bull_retest, long_avwap_reclaim, long_base_breakout)
            if settings.boolean("use_long_side_v2")
            else None,
        )
        short_setup = _select_setup(
            flip=short_flip_trigger,
            hidden=short_hidden_trigger,
            regular=short_regular_trigger,
            reclaim=short_reclaim_trigger,
            short_v2=(short_failed_reclaim, short_breakdown, short_bear_retest)
            if settings.boolean("use_short_side_v2")
            else None,
        )

        exit_reason = "none"
        intent = SignalIntent.NONE
        if item.account.position is Side.LONG:
            adverse = regime == -1 or (settings.boolean("exit_on_neutral") and regime == 0)
            avwap_exit = (
                settings.boolean("use_long_side_v2")
                and settings.boolean("use_long_avwap_exit")
                and reclaim_down
            )
            rs_exit = (
                settings.boolean("use_long_side_v2")
                and settings.boolean("use_long_rs_deterioration_exit")
                and (mansfield is not None and mansfield < 0.0)
            )
            time_exit = (
                settings.boolean("use_time_stop")
                and item.account.entry_bar_index is not None
                and index - item.account.entry_bar_index >= settings.integer("max_bars_in_trade")
            )
            if adverse or avwap_exit or rs_exit or time_exit:
                intent = SignalIntent.EXIT_LONG
                exit_reason = (
                    "adverse_regime"
                    if adverse
                    else "avwap_failure"
                    if avwap_exit
                    else "relative_strength_deterioration"
                    if rs_exit
                    else "time_stop"
                )
        elif item.account.position is Side.SHORT:
            adverse = regime == 1 or (settings.boolean("exit_on_neutral") and regime == 0)
            avwap_exit = settings.boolean("use_short_avwap_exit") and reclaim_up
            time_exit = (
                settings.boolean("use_time_stop")
                and item.account.entry_bar_index is not None
                and index - item.account.entry_bar_index >= settings.integer("max_bars_in_trade")
            )
            if adverse or avwap_exit or time_exit:
                intent = SignalIntent.EXIT_SHORT
                exit_reason = (
                    "adverse_regime" if adverse else "avwap_reclaim" if avwap_exit else "time_stop"
                )
        elif can_long:
            intent = SignalIntent.ENTER_LONG
        elif can_short:
            intent = SignalIntent.ENTER_SHORT

        if intent is SignalIntent.ENTER_LONG:
            pending_side = Side.LONG
            pending_setup = long_setup
        elif intent is SignalIntent.ENTER_SHORT:
            pending_side = Side.SHORT
            pending_setup = short_setup

        event_side = (
            Side.LONG
            if intent in {SignalIntent.ENTER_LONG, SignalIntent.EXIT_LONG}
            else Side.SHORT
            if intent in {SignalIntent.ENTER_SHORT, SignalIntent.EXIT_SHORT}
            else Side.FLAT
        )
        event_setup = long_setup if event_side is Side.LONG else short_setup
        events: tuple[SignalEvent, ...] = ()
        emitted_ids = state.emitted_event_ids
        if intent is not SignalIntent.NONE:
            event_id = _stable_digest(
                {
                    "strategy": "five_tool_confluence_v3_6",
                    "timestamp": timestamp.isoformat(),
                    "intent": intent.value,
                    "side": event_side.value,
                }
            )
            if event_id not in emitted_ids:
                events = (
                    SignalEvent(
                        event_id=event_id,
                        kind=intent.value,
                        timestamp_utc=timestamp,
                        side=event_side,
                        setup=event_setup,
                    ),
                )
                emitted_ids = (*emitted_ids, event_id)

        warmup: list[str] = []
        if regime_z is None:
            warmup.append("regime_z")
        if benchmark_close is None:
            warmup.append("benchmark_initial_gap")
        if mansfield is None:
            warmup.append("mansfield")
        if _last(oscillator_series) is None:
            warmup.append("oscillator")
        if flip_vwap is None:
            warmup.append("avwap_anchor_or_weight")
        if settings.boolean("use_htf_filter") and (item.htf_close is None or item.htf_ema is None):
            warmup.append("prior_completed_htf")
        if use_external and not external_live:
            warmup.append("external_regime_latch")

        features: dict[str, TraceValue] = {
            "regime_z": regime_z,
            "enter_z": enter_z,
            "exit_z": exit_z,
            "candidate_regime": candidate,
            "internal_regime": internal_regime,
            "regime": regime,
            "regime_age": active_age,
            "strength": strength,
            "adx": adx_value,
            "efficiency_ratio": efficiency_ratio,
            "chop_risk": chop_risk,
            "risk_scale": risk_scale,
            "gap_shock": gap_shock,
            "mansfield": mansfield,
            "mansfield_rising": mans_rising,
            "rs_new_high": rs_new_high,
            "rs_new_low": rs_new_low,
            "oscillator": _last(oscillator_series),
            "regular_bull_divergence": regular_bull,
            "hidden_bull_divergence": hidden_bull,
            "regular_bear_divergence": regular_bear,
            "hidden_bear_divergence": hidden_bear,
            "avwap": flip_vwap,
            "avwap_sd": flip_sd,
            "avwap_reset": avwap_reset,
            "avwap_stale_reset": stale_reset,
            "avwap_age": avwap_age,
            "markov_row_n": markov_n,
            "markov_stay_count": stay_count,
            "markov_p_stay": markov_p_stay,
            "markov_gate_p_stay": markov_gate_probability,
            "markov_maturity": markov_maturity,
            "dwell_percentile": dwell_percentile,
            "long_score": active_long_score,
            "short_score": active_short_score,
            "short_continuation_score": short_continuation_score,
            "atr": atr_value,
            "atr_percent": atr_percent,
            "long_stop_distance": long_stop_distance,
            "short_stop_distance": short_stop_distance,
            "equity_drawdown_percent": equity_drawdown,
            "daily_drawdown_percent": daily_drawdown,
            "exit_reason_signal": exit_reason,
        }
        gates = {
            "trend_quality": trend_quality_ok,
            "playbook_quality": playbook_quality_ok,
            "playbook_strength": playbook_strength_ok,
            "playbook_stability": playbook_stability_ok,
            "playbook_gap": playbook_gap_ok,
            "entry_gates": entry_gates_ok,
            "benchmark_long": benchmark_long_ok,
            "benchmark_short": benchmark_short_ok,
            "htf_long": htf_long_ok,
            "htf_short": htf_short_ok,
            "avwap_entries": avwap_entries_ok,
            "atr_percent": atr_percent_ok,
            "liquidity": liquidity_ok,
            "cooldown": cooldown_ok,
            "long_risk_halt_clear": not long_risk_halt,
            "short_risk_halt_clear": not short_risk_halt,
            "long_review": active_long_review,
            "short_review": active_short_review,
            "long_plus": long_plus_pass,
            "short_plus": short_plus_pass,
            "long_volume": long_volume_ok,
            "short_volume": short_volume_ok,
            "can_long": can_long,
            "can_short": can_short,
        }

        new_state = replace(
            state,
            observations=observations,
            candidate_regimes=candidates,
            confirmed_core=confirmed_core,
            last_regime=last_regime,
            have_regime=have_regime,
            previous_selected_regime=regime,
            active_bars_in_regime=active_age,
            dwell_bull=dwell_bull,
            dwell_neutral=dwell_neutral,
            dwell_bear=dwell_bear,
            markov_counts=tuple(markov_counts),
            markov_rows=tuple(markov_rows),
            markov_last_regime=markov_last_regime,
            markov_last_bar_index=markov_last_bar,
            external_ok_run=external_ok_run,
            external_bad_run=external_bad_run,
            external_latched=external_latched,
            external_last=external_last,
            avwap_pv=avwap_pv,
            avwap_weight=avwap_weight,
            avwap_p2v=avwap_p2v,
            avwap_on=avwap_on,
            avwap_age=avwap_age,
            previous_pivot_low=previous_pivot_low,
            previous_pivot_high=previous_pivot_high,
            short_retest_seen=short_retest_seen,
            short_retest_taken=short_retest_taken,
            long_retest_seen=long_retest_seen,
            long_retest_taken=long_retest_taken,
            pending_entry_side=pending_side,
            pending_entry_setup=pending_setup,
            equity_peak=equity_peak,
            equity_history=equity_history,
            long_equity_peak=long_equity_peak,
            long_equity_history=long_equity_history,
            short_equity_peak=short_equity_peak,
            short_equity_history=short_equity_history,
            day_start_equity=day_start_equity,
            long_day_start_equity=long_day_start,
            short_day_start_equity=short_day_start,
            day_session=item.primary.session_date,
            daily_halt_latched=daily_halt_latched,
            long_daily_halt_latched=long_daily_latched,
            short_daily_halt_latched=short_daily_latched,
            previous_position=item.account.position,
            last_exit_bar_index=last_exit_index,
            emitted_event_ids=emitted_ids,
        )
        state_digest = _stable_digest(asdict(new_state))
        trace = FiveToolTrace(
            bar_index=index,
            timestamp_utc=timestamp,
            primary_sequence_id=f"{item.primary.sequence_id}:{timestamp.isoformat()}",
            benchmark_source_id=(
                item.benchmark.source_sequence_id if item.benchmark is not None else None
            ),
            htf_source_id=(
                item.htf_close.source_sequence_id if item.htf_close is not None else None
            ),
            history_start_utc=settings.history_start_utc,
            features=tuple(features.items()),
            gates=tuple(gates.items()),
            warmup_blockers=tuple(warmup),
            long_setup=long_setup,
            short_setup=short_setup,
            intent=intent,
            events=events,
            state_digest=state_digest,
        )
        self.state = new_state
        return trace


def evaluate_batch(
    settings: FiveToolSettings, inputs: tuple[FiveToolBarInput, ...]
) -> tuple[FiveToolTrace, ...]:
    """Batch façade intentionally implemented by repeated causal ``step`` calls."""

    engine = FiveToolEngine(settings)
    return tuple(engine.step(item) for item in inputs)


def resume_batch(
    settings: FiveToolSettings,
    state: FiveToolState,
    inputs: tuple[FiveToolBarInput, ...],
) -> tuple[FiveToolTrace, ...]:
    engine = FiveToolEngine(settings, state=state)
    return tuple(engine.step(item) for item in inputs)
