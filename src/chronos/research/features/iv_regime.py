"""Closed-bar port of Pine 31 index-vol weather.  Index context, not symbol IV."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from chronos.marketdata.bars import Bar, BarSeries
from chronos.research.features.alignment import align_companions
from chronos.research.features.models import (
    FeatureFamily,
    FeatureInputError,
    FeaturePolicy,
    FeatureSnapshot,
    IvState,
)
from chronos.research.five_tool.alignment import source_bar_id
from chronos.research.five_tool.indicators import pine_percentrank

_IV_ORDER = (IvState.LOW, IvState.NORMAL, IvState.ELEVATED, IvState.STRESS)


@dataclass(frozen=True, slots=True)
class IvObservation:
    snapshot: FeatureSnapshot
    state: IvState | None
    backwardation: bool | None


def _state_from_percentile(percentile: float, policy: FeaturePolicy) -> IvState:
    if percentile < policy.iv_cut_low:
        return IvState.LOW
    if percentile < policy.iv_cut_elevated:
        return IvState.NORMAL
    if percentile < policy.iv_cut_stress:
        return IvState.ELEVATED
    return IvState.STRESS


def _escalate(state: IvState) -> IvState:
    index = _IV_ORDER.index(state)
    return _IV_ORDER[min(index + 1, len(_IV_ORDER) - 1)]


def evaluate_iv_regime(
    primary: Sequence[Bar],
    vix: BarSeries | None,
    vix3m: BarSeries | None = None,
    policy: FeaturePolicy | None = None,
) -> tuple[IvObservation, ...]:
    """Prior-completed VIX close, 252-day percentrank, optional VIX3M term ratio."""

    settings = policy or FeaturePolicy()
    if not primary:
        raise FeatureInputError("IV evaluation requires a non-empty primary series")
    aligned = align_companions(
        primary,
        {"vix": vix, "vix3m": vix3m},
        allow_equal={"vix": False, "vix3m": False},
    )
    vix_closes: list[float | None] = []
    observations: list[IvObservation] = []
    watch_on = False
    watch_days = 0
    previous_percentile: float | None = None
    episodes: deque[int] = deque(maxlen=50)
    previous_session = None
    for bar, companions in zip(primary, aligned, strict=True):
        vix_bar = companions["vix"]
        vix3m_bar = companions["vix3m"]
        missing = () if vix_bar is not None else ("vix",)
        vix_closes.append(None if vix_bar is None else vix_bar.close)
        ranks = pine_percentrank(vix_closes, settings.iv_percentile_length)
        percentile = ranks[-1]
        term_ratio = None
        if vix_bar is not None and vix3m_bar is not None and vix3m_bar.close > 0.0:
            term_ratio = vix_bar.close / vix3m_bar.close
        backwardation = None if term_ratio is None else term_ratio > 1.0
        base = None if percentile is None else _state_from_percentile(percentile, settings)
        state = None if base is None else (_escalate(base) if backwardation else base)
        new_day = previous_session is None or bar.session_date != previous_session
        if new_day and percentile is not None:
            if watch_on:
                watch_days += 1
                if percentile < settings.iv_crush_exit:
                    episodes.append(watch_days)
                    watch_on = False
            elif (
                previous_percentile is not None
                and previous_percentile < settings.iv_cut_stress
                and percentile >= settings.iv_cut_stress
            ):
                watch_on = True
                watch_days = 0
            previous_percentile = percentile
        crush_median = None if not episodes else sorted(episodes)[len(episodes) // 2]
        snapshot = FeatureSnapshot(
            family=FeatureFamily.IV_REGIME,
            timestamp_utc=bar.timestamp_utc,
            primary_sequence_id=source_bar_id(bar),
            warmup=percentile is None,
            missing_required=missing,
            values=(
                ("IVP_STATE", None if state is None else state.value),
                ("IVP_PCTILE", percentile),
                ("IVP_TERM_RATIO", term_ratio),
                ("IVP_BACKWARDATION", backwardation),
                ("IVP_CRUSH_MEDIAN", crush_median),
                ("IVP_EPISODE_N", len(episodes)),
                ("VIX_CLOSE", None if vix_bar is None else vix_bar.close),
            ),
        )
        observations.append(
            IvObservation(snapshot=snapshot, state=state, backwardation=backwardation)
        )
        previous_session = bar.session_date
    return tuple(observations)
