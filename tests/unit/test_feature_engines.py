from __future__ import annotations

from dataclasses import replace

import pytest
from tests.fixtures.features.bars import daily_bars, intraday_bars

from chronos.marketdata.bars import BarSeries
from chronos.research.features.breadth import evaluate_breadth
from chronos.research.features.iv_regime import evaluate_iv_regime
from chronos.research.features.models import (
    FeatureInputError,
    FeaturePolicy,
    IvState,
    TailState,
    UsdState,
)
from chronos.research.features.rvol import evaluate_daily_rvol, evaluate_tod_rvol
from chronos.research.features.tail_risk import evaluate_tail_risk
from chronos.research.features.usd_regime import evaluate_usd_regime, require_certified_uup


def test_tail_risk_warms_then_flags_fat_tailed_after_shock() -> None:
    policy = FeaturePolicy(
        tail_window=20, enable_rvol=False, enable_iv_regime=False, enable_breadth=False
    )
    series = daily_bars("AAA", count=40, step=0.05, shock_index=35, shock_return=-0.35)
    observations = evaluate_tail_risk(series.bars, policy)
    assert observations[10].snapshot.warmup
    assert observations[-1].state is TailState.FAT_TAILED
    assert observations[-1].snapshot.value("TR_STATE") == "FAT_TAILED"
    assert observations[-1].snapshot.value("TR_KURT") is not None


def test_tail_risk_is_deterministic() -> None:
    series = daily_bars("AAA", count=30, step=0.1)
    first = evaluate_tail_risk(series.bars)
    second = evaluate_tail_risk(series.bars)
    assert [item.snapshot.values for item in first] == [item.snapshot.values for item in second]


def test_daily_rvol_in_play_and_warmup() -> None:
    policy = FeaturePolicy(rvol_lookback=5, rvol_min_avg_dollar_vol_millions=0.0)
    quiet = daily_bars("AAA", count=8, volume=1_000.0)
    bars = [*quiet.bars[:-1], replace(quiet.bars[-1], volume=50_000.0)]
    series = BarSeries(symbol="AAA", interval=quiet.interval, bars=tuple(bars))
    observations = evaluate_daily_rvol(series.bars, policy)
    assert observations[2].snapshot.warmup
    assert observations[-1].tod_inert is True
    assert observations[-1].snapshot.value("RVOL_TOD") is None
    assert observations[-1].in_play is True
    assert observations[-1].snapshot.value("RVOL_DAILY") is not None
    assert observations[-1].snapshot.value("RVOL_DAILY") >= 1.5


def test_tod_rvol_is_inert_on_daily_and_counts_on_intraday() -> None:
    daily = daily_bars("AAA", count=6)
    daily_tod = evaluate_tod_rvol(daily.bars, regime=[1] * 6)
    assert all(item.tod_inert for item in daily_tod)
    assert all(item.snapshot.value("RVOL_TOD") is None for item in daily_tod)

    intraday = intraday_bars("AAA", days=8, bars_per_day=4, elevated_day=7)
    regimes = [1] * len(intraday.bars)
    observations = evaluate_tod_rvol(intraday.bars, regime=regimes)
    assert observations[0].tod_inert is False
    last = observations[-1]
    assert last.snapshot.value("TOD_INERT") is False
    assert last.snapshot.value("RVOL_TOD") is not None
    assert last.snapshot.value("RVOL_TOD") > 1.5
    assert last.snapshot.value("TRF_ELEVATED") is True


def _vix_with_closes(closes: list[float]) -> BarSeries:
    base = daily_bars("VIX", count=len(closes), close=15.0, exchange="CBOE")
    return BarSeries(
        symbol="VIX",
        interval=base.interval,
        bars=tuple(
            replace(bar, close=price, open=price - 0.1, high=price + 0.2, low=price - 0.2)
            for bar, price in zip(base.bars, closes, strict=True)
        ),
    )


def test_iv_regime_stress_and_missing_vix_fail_closed() -> None:
    primary = daily_bars("AAA", count=260, step=0.01)
    closes = [15.0] * 260
    closes[-2] = 80.0
    observations = evaluate_iv_regime(primary.bars, _vix_with_closes(closes))
    assert observations[10].snapshot.warmup
    assert observations[-1].state is IvState.STRESS
    missing = evaluate_iv_regime(primary.bars, None)
    assert missing[-1].snapshot.missing_required == ("vix",)


def test_iv_backwardation_escalates_elevated_to_stress() -> None:
    primary = daily_bars("AAA", count=260)
    closes = [10.0] * 200 + [30.0] * 58 + [25.0, 25.0]
    vix = _vix_with_closes(closes)
    vix3m = daily_bars("VIX3M", count=260, close=10.0, exchange="CBOE")
    observations = evaluate_iv_regime(primary.bars, vix, vix3m)
    last = observations[-1]
    assert last.snapshot.value("IVP_PCTILE") is not None
    assert 75.0 <= float(last.snapshot.value("IVP_PCTILE")) < 90.0
    assert last.backwardation is True
    assert last.state is IvState.STRESS


def test_breadth_align_divergent_and_missing_companions() -> None:
    policy = FeaturePolicy(breadth_slope_lookback=3)
    primary = daily_bars("AAA", count=10, step=0.2)
    spy = daily_bars("SPY", count=10, step=0.2)
    rsp = daily_bars("RSP", count=10, step=-0.3)
    qqq = daily_bars("QQQ", count=10, step=-0.4)
    regime = [1] * 10
    observations = evaluate_breadth(
        primary.bars, spy=spy, rsp=rsp, qqq=qqq, regime=regime, policy=policy
    )
    assert observations[1].snapshot.warmup
    assert observations[-1].align == -1
    missing = evaluate_breadth(
        primary.bars, spy=None, rsp=None, qqq=None, regime=regime, policy=policy
    )
    assert missing[-1].snapshot.missing_required == ("spy", "rsp", "qqq")
    assert missing[-1].align is None


def test_usd_regime_flags_rising_dollar_and_fails_closed_without_uup() -> None:
    policy = FeaturePolicy(usd_slope_lookback=3, enable_usd_regime=True)
    primary = daily_bars("GLD", count=8, step=0.1)
    rising = daily_bars("UUP", count=8, step=0.4)
    falling = daily_bars("UUP", count=8, step=-0.4)
    up = evaluate_usd_regime(primary.bars, rising, policy)
    down = evaluate_usd_regime(primary.bars, falling, policy)
    assert up[1].snapshot.warmup
    assert up[-1].state is UsdState.RISING
    assert up[-1].snapshot.value("USD_STATE") == "RISING"
    assert down[-1].state is UsdState.FALLING
    missing = evaluate_usd_regime(primary.bars, None, policy)
    assert missing[-1].snapshot.missing_required == ("uup",)
    with pytest.raises(FeatureInputError, match="does not download"):
        require_certified_uup()
