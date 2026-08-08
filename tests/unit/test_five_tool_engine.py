from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.research.five_tool.alignment import AccountProvider, align_five_tool_inputs
from chronos.research.five_tool.checkpoint import state_from_json, state_to_json
from chronos.research.five_tool.engine import FiveToolEngine, evaluate_batch, resume_batch
from chronos.research.five_tool.indicators import confirmed_pivot, pine_ema
from chronos.research.five_tool.models import (
    AccountSnapshot,
    FiveToolBarInput,
    FiveToolInputError,
    FiveToolSettings,
    Side,
    SignalIntent,
)

START = datetime(2020, 1, 2, 21, tzinfo=UTC)


def _series(
    symbol: str,
    *,
    count: int = 70,
    start: datetime = START,
    missing: frozenset[int] = frozenset(),
    interval: BarInterval = BarInterval.DAY_1,
    price_scale: float = 1.0,
) -> BarSeries:
    bars: list[Bar] = []
    for index in range(count):
        if index in missing:
            continue
        timestamp = start + timedelta(days=index)
        # Alternating return magnitude keeps volatility non-zero while retaining
        # a strong upward window return.
        close = (100.0 + index * 0.55 + (0.35 if index % 2 else -0.25)) * price_scale
        bars.append(
            Bar(
                symbol=symbol,
                source="internal_spec",
                exchange="NYSE",
                interval=interval,
                session_date=timestamp.date(),
                timestamp_utc=timestamp,
                open=close - 0.15,
                high=close + 0.8,
                low=close - 0.8,
                close=close,
                volume=1_000_000.0 + index * 100.0,
            )
        )
    return BarSeries(symbol=symbol, interval=interval, bars=tuple(bars))


def _settings(**extra: bool | int | float | str) -> FiveToolSettings:
    overrides: dict[str, bool | int | float | str] = {
        "preset_input": "Custom",
        "lookback_custom": 3,
        "enter_z_custom": 0.10,
        "exit_z_custom": 0.05,
        "confirm_custom": 1,
        "use_vol_percentile_adjustment": False,
        "use_ema_filter": False,
        "strength_filter": "Off",
        "playbook_quality_filter": "Off",
        "min_strength_for_bias": 0.0,
        "mans_len": 3,
        "rs_mode": "Off",
        "min_score": 0,
        "use_equity_dd_halt": False,
    }
    overrides.update(extra)
    return FiveToolSettings.defaults(history_start_utc=START, overrides=overrides)


def _inputs(
    *,
    count: int = 70,
    missing_benchmark: frozenset[int] = frozenset(),
    account_provider: AccountProvider | None = None,
) -> tuple[FiveToolBarInput, ...]:
    primary = _series("AAA", count=count)
    benchmark = _series("SPY", count=count, missing=missing_benchmark, price_scale=4.0)
    return align_five_tool_inputs(
        primary,
        benchmark,
        account_provider=account_provider,
    )


def test_pine_ema_uses_first_value_seed_not_production_sma_seed() -> None:
    assert pine_ema((10.0, 20.0, 30.0), 3) == (10.0, 15.0, 22.5)


def test_pivot_is_published_only_on_right_confirmation_bar() -> None:
    values = (3.0, 2.0, 1.0, 2.0)
    assert confirmed_pivot(values[:3], left=1, right=1, kind="low") is None
    assert confirmed_pivot(values, left=1, right=1, kind="low") == (2, 1.0)


def test_batch_stream_and_serialized_checkpoint_are_exactly_equal() -> None:
    settings = _settings()
    inputs = _inputs(count=60)
    batch = evaluate_batch(settings, inputs)

    stream_engine = FiveToolEngine(settings)
    streamed = tuple(stream_engine.step(item) for item in inputs)
    assert streamed == batch

    split = 27
    first_engine = FiveToolEngine(settings)
    first = tuple(first_engine.step(item) for item in inputs[:split])
    serialized = state_to_json(first_engine.checkpoint())
    restored = state_from_json(serialized)
    second = resume_batch(settings, restored, inputs[split:])
    assert first + second == batch
    assert state_to_json(restored) == serialized


def test_checkpoint_integrity_is_fail_closed() -> None:
    settings = _settings()
    engine = FiveToolEngine(settings)
    engine.step(_inputs(count=1)[0])
    payload = json.loads(state_to_json(engine.checkpoint()))
    payload["state"]["active_bars_in_regime"] = 999
    with pytest.raises(FiveToolInputError, match="integrity"):
        state_from_json(json.dumps(payload))


def test_future_bar_perturbation_cannot_change_prior_trace() -> None:
    settings = _settings()
    inputs = _inputs(count=45)
    baseline = evaluate_batch(settings, inputs)
    future = inputs[-1]
    changed_bar = replace(
        future.primary,
        open=future.primary.open * 1.5,
        high=future.primary.high * 1.5,
        low=future.primary.low * 1.5,
        close=future.primary.close * 1.5,
    )
    changed_inputs = (*inputs[:-1], replace(future, primary=changed_bar))
    changed = evaluate_batch(settings, changed_inputs)
    assert changed[:-1] == baseline[:-1]


def test_history_origin_is_exact_and_part_of_identity() -> None:
    inputs = _inputs(count=5)
    wrong = FiveToolSettings.defaults(history_start_utc=START - timedelta(days=1))
    with pytest.raises(FiveToolInputError, match="pinned history_start"):
        FiveToolEngine(wrong).step(inputs[0])
    settings = _settings()
    assert settings.digest != replace(settings, history_start_utc=START + timedelta(days=1)).digest


def test_benchmark_gaps_carry_forward_and_initial_gap_does_not_backfill() -> None:
    primary = _series("AAA", count=8)
    benchmark = _series("SPY", count=8, missing=frozenset({0, 3, 4}), price_scale=4.0)
    aligned = align_five_tool_inputs(primary, benchmark)
    assert aligned[0].benchmark is None
    assert aligned[3].benchmark == aligned[2].benchmark
    assert aligned[4].benchmark == aligned[2].benchmark
    assert aligned[5].benchmark is not None
    assert aligned[5].benchmark.source_timestamp_utc <= aligned[5].primary.timestamp_utc


def test_external_regime_latch_flip_emits_one_logical_event() -> None:
    settings = _settings(
        use_external=True,
        trig_hidden=False,
        trig_regular=False,
        trig_reclaim=False,
        use_value_zone=False,
    )
    aligned = list(_inputs(count=24))
    for index, item in enumerate(aligned):
        regime = 0.0 if index < 20 else 1.0
        aligned[index] = replace(item, external_regime=regime)
    traces = evaluate_batch(settings, tuple(aligned))
    entries = [trace for trace in traces if trace.intent is SignalIntent.ENTER_LONG]
    assert entries
    assert all(len(trace.events) == 1 for trace in entries)
    assert len({trace.events[0].event_id for trace in entries}) == len(entries)


def test_daily_loss_halt_is_pine_exactly_inert_on_daily_bars() -> None:
    def account(_bar: Bar, index: int) -> AccountSnapshot:
        return AccountSnapshot(equity=100_000.0 - index * 10_000.0, position=Side.FLAT)

    settings = _settings(use_daily_loss_halt=True, daily_loss_halt_pct=1.0)
    traces = evaluate_batch(
        settings,
        _inputs(count=8, account_provider=account),
    )
    assert all(trace.feature("daily_drawdown_percent") == 0.0 for trace in traces)
    assert all(trace.gate("long_risk_halt_clear") for trace in traces)


def test_volume_expansion_gate_preserves_documented_pine_fail_open_warmup() -> None:
    settings = _settings(
        use_long_side_v2=True,
        long_plus_enabled=True,
        long_plus_volume_filter=True,
        long_plus_volume_len=20,
    )
    trace = evaluate_batch(settings, _inputs(count=1))[0]
    assert trace.gate("long_volume") is True


def test_blended_capital_refuses_sign_inference_without_side_equities() -> None:
    settings = _settings(use_blended_capital_split=True)
    with pytest.raises(FiveToolInputError, match="fill-attributed side equities"):
        evaluate_batch(settings, _inputs(count=1))
