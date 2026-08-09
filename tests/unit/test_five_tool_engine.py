from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.research.five_tool.alignment import AccountProvider, align_five_tool_inputs
from chronos.research.five_tool.checkpoint import state_from_json, state_to_json
from chronos.research.five_tool.engine import (
    FiveToolEngine,
    _avwap_display_hidden,
    evaluate_batch,
    resume_batch,
)
from chronos.research.five_tool.indicators import (
    confirmed_pivot,
    pine_ema,
    pine_mfi,
    pine_percentrank,
)
from chronos.research.five_tool.models import (
    AccountSnapshot,
    FiveToolBarInput,
    FiveToolInputError,
    FiveToolSettings,
    SetupFamily,
    Side,
    SignalIntent,
    pine_timeframe_seconds,
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
                exchange="AMEX" if symbol == "SPY" else "NYSE",
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
        "lookback_custom": 5,
        "enter_z_custom": 0.10,
        "exit_z_custom": 0.05,
        "confirm_custom": 1,
        "use_vol_percentile_adjustment": False,
        "use_ema_filter": False,
        "strength_filter": "Off",
        "playbook_quality_filter": "Off",
        "min_strength_for_bias": 0.0,
        "mans_len": 5,
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
    settings: FiveToolSettings | None = None,
) -> tuple[FiveToolBarInput, ...]:
    primary = _series("AAA", count=count)
    benchmark = _series("SPY", count=count, missing=missing_benchmark, price_scale=4.0)
    return align_five_tool_inputs(
        settings or _settings(),
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


def test_pine_percentrank_keeps_missing_slots_in_the_lookback_window() -> None:
    assert pine_percentrank((1.0, 2.0, None, 3.0, 4.0, 5.0), 2) == (
        None,
        None,
        None,
        None,
        None,
        100.0,
    )


def test_pine_mfi_includes_first_bar_flow_in_both_directional_sums() -> None:
    values = (1.0, 2.0, 1.0, 2.0, 1.0)
    assert pine_mfi(values, values, values, (100.0,) * 5, 3) == (
        None,
        None,
        60.0,
        80.0,
        50.0,
    )


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
    aligned = align_five_tool_inputs(_settings(), primary, benchmark)
    assert aligned[0].benchmark is None
    assert aligned[3].benchmark == aligned[2].benchmark
    assert aligned[4].benchmark == aligned[2].benchmark
    assert aligned[5].benchmark is not None
    assert aligned[5].benchmark.source_timestamp_utc <= aligned[5].primary.timestamp_utc


def test_external_regime_latch_flip_and_entry_emit_distinct_events() -> None:
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
    flip_entries = [trace for trace in entries if trace.feature("regime_flip") is True]
    assert len(flip_entries) == 1
    assert [event.kind for event in flip_entries[0].events] == [
        "regime_flip",
        SignalIntent.ENTER_LONG.value,
    ]
    entry_events = [
        event
        for trace in entries
        for event in trace.events
        if event.kind == SignalIntent.ENTER_LONG.value
    ]
    assert len({event.event_id for event in entry_events}) == len(entry_events)


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


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("lookback_custom", 0),
        ("confirm_custom", -3),
        ("strength_filter", "TYPO"),
        ("mk_laplace_alpha", -1.0),
    ],
)
def test_settings_reject_values_outside_frozen_pine_contract(
    name: str, value: bool | int | float | str
) -> None:
    with pytest.raises(FiveToolInputError):
        FiveToolSettings.defaults(history_start_utc=START, overrides={name: value})


def test_settings_canonicalize_pine_time_inputs_to_unix_milliseconds() -> None:
    settings = FiveToolSettings.defaults(history_start_utc=START)
    assert settings.integer("test_start") == 1_514_764_800_000
    assert settings.integer("test_end") == 4_102_444_740_000
    assert settings.integer("oos_start") == 1_640_995_200_000

    equivalent = FiveToolSettings.defaults(
        history_start_utc=START,
        overrides={"test_start": 1_514_764_800_000},
    )
    assert equivalent.digest == settings.digest
    with pytest.raises(FiveToolInputError, match="incompatible type"):
        FiveToolSettings.defaults(
            history_start_utc=START,
            overrides={"test_start": "not a timestamp"},
        )


def test_equal_timeframe_htf_filter_is_inert_like_pine() -> None:
    settings = _settings(use_htf_filter=True, htf_tf="D")
    traces = evaluate_batch(settings, _inputs(count=10, settings=settings))
    assert traces[-1].feature("htf_valid") is False
    assert traces[-1].gate("htf_long") is True
    assert traces[-1].gate("htf_short") is True
    assert "prior_completed_htf" not in traces[-1].warmup_blockers


def test_liquidity_point_value_applies_only_when_pine_toggle_is_enabled() -> None:
    common = {
        "use_liquidity_filter": True,
        "liq_len": 5,
        "min_dollar_volume": 1_000_000_000.0,
    }
    disabled = _settings(**common, use_pointvalue_sizing=False)
    enabled = FiveToolSettings.defaults(
        history_start_utc=START,
        overrides={**dict(disabled.inputs), "use_pointvalue_sizing": True},
        point_value=50.0,
    )
    disabled = replace(disabled, point_value=50.0)
    assert (
        evaluate_batch(disabled, _inputs(count=8, settings=disabled))[-1].gate("liquidity") is False
    )
    assert evaluate_batch(enabled, _inputs(count=8, settings=enabled))[-1].gate("liquidity") is True


def test_avwap_stale_counter_counts_only_positive_weight_observations() -> None:
    settings = _settings(use_external=True, vwap_weighting="Volume")
    aligned = list(_inputs(count=24, settings=settings))
    for index, item in enumerate(aligned):
        primary = replace(item.primary, volume=0.0)
        aligned[index] = replace(
            item,
            primary=primary,
            external_regime=0.0 if index < 20 else 1.0,
        )
    trace = evaluate_batch(settings, tuple(aligned))[-1]
    assert trace.feature("avwap_age") == 4
    assert trace.feature("avwap_valid_observations") == 0


def test_base_breakout_signal_checkpoints_its_signal_bar_pivot() -> None:
    settings = _settings(
        use_long_side_v2=True,
        long_rs_mode="Mansfield above zero",
        allow_bull_retest_trigger=False,
        allow_avwap_reclaim_trigger=False,
        allow_base_breakout_trigger=True,
        long_base_tightness_max_pct=50.0,
        long_max_dist_above_avwap_atr=15.0,
        use_long_no_chase_filter=False,
        use_long_resistance_filter=False,
        use_long_exhaustion_filter=False,
    )
    primary = _series("AAA", count=30)
    benchmark_raw = _series("SPY", count=30, price_scale=4.0)
    benchmark_bars: list[Bar] = []
    for index, bar in enumerate(benchmark_raw.bars):
        close = 400.0 + index * 0.05 + (0.1 if index % 2 else -0.1)
        benchmark_bars.append(
            replace(
                bar,
                open=close - 0.1,
                high=close + 0.3,
                low=close - 0.3,
                close=close,
            )
        )
    inputs = align_five_tool_inputs(
        settings,
        primary,
        BarSeries(symbol="SPY", interval=benchmark_raw.interval, bars=tuple(benchmark_bars)),
    )

    engine = FiveToolEngine(settings)
    entry_trace = None
    for item in inputs:
        trace = engine.step(item)
        if trace.intent is SignalIntent.ENTER_LONG:
            entry_trace = trace
            break
    assert entry_trace is not None
    assert entry_trace.long_setup is SetupFamily.BASE_BREAKOUT
    lookback = settings.integer("long_base_lookback")
    expected_pivot = max(
        item.primary.high
        for item in inputs[entry_trace.bar_index - lookback : entry_trace.bar_index]
    )
    pending = engine.checkpoint()
    assert pending.pending_entry_side is Side.LONG
    assert pending.pending_entry_setup is SetupFamily.BASE_BREAKOUT
    assert pending.pending_base_pivot_at_entry == expected_pivot
    assert state_from_json(state_to_json(pending)).pending_base_pivot_at_entry == expected_pivot


def test_base_breakout_failure_exit_uses_entry_setup_and_pivot() -> None:
    settings = _settings(
        use_external=True,
        use_long_side_v2=True,
        use_long_base_failure_exit=True,
        use_long_avwap_exit=False,
        use_long_rs_deterioration_exit=False,
    )
    aligned = list(_inputs(count=24, settings=settings))
    for index, item in enumerate(aligned):
        aligned[index] = replace(item, external_regime=0.0 if index < 20 else 1.0)
    engine = FiveToolEngine(settings)
    for item in aligned[:-1]:
        engine.step(item)
    last = aligned[-1]
    base_pivot = last.primary.close + 1.0
    queued = replace(
        engine.checkpoint(),
        pending_entry_side=Side.LONG,
        pending_entry_setup=SetupFamily.BASE_BREAKOUT,
        pending_base_pivot_at_entry=base_pivot,
    )
    engine = FiveToolEngine(settings, state=queued)
    trace = engine.step(
        replace(
            last,
            account=AccountSnapshot(
                position=Side.LONG,
                entry_setup=SetupFamily.BASE_BREAKOUT,
                base_pivot_at_entry=base_pivot,
            ),
        )
    )
    assert trace.intent is SignalIntent.EXIT_LONG
    assert trace.feature("exit_reason_signal") == "base_failure"
    assert trace.events[0].setup is SetupFamily.BASE_BREAKOUT


def test_primary_identity_cannot_change_mid_stream() -> None:
    settings = _settings()
    inputs = _inputs(count=2, settings=settings)
    engine = FiveToolEngine(settings)
    engine.step(inputs[0])
    changed = replace(inputs[1], primary=replace(inputs[1].primary, symbol="BBB"))
    with pytest.raises(FiveToolInputError, match="identity changed"):
        engine.step(changed)


@pytest.mark.parametrize(
    ("timeframe", "seconds"),
    [
        ("S", 1),
        ("45S", 45),
        ("1", 60),
        ("1440", 1440 * 60),
        ("2D", 2 * 24 * 60 * 60),
        ("3W", 3 * 7 * 24 * 60 * 60),
        ("12M", 12 * 30 * 24 * 60 * 60),
    ],
)
def test_pine_timeframe_parser_supports_complete_time_based_forms(
    timeframe: str, seconds: int
) -> None:
    assert pine_timeframe_seconds(timeframe) == seconds


@pytest.mark.parametrize(
    "timeframe",
    ["", "2H", "2S", "1441", "366D", "53W", "13M", "10T", "2d", "٢D"],
)
def test_unsupported_or_invalid_htf_is_rejected_when_settings_are_built(
    timeframe: str,
) -> None:
    with pytest.raises(FiveToolInputError, match="timeframe"):
        _settings(htf_tf=timeframe)


def test_multi_day_htf_is_validated_before_evaluation_and_remains_causal() -> None:
    settings = _settings(use_htf_filter=True, htf_tf="2D")
    # The alignment facade rejects 2D because BarInterval cannot represent it;
    # direct typed inputs still fail the HTF gate closed when evidence is absent.
    trace = evaluate_batch(settings, _inputs(count=1))[0]
    assert trace.feature("htf_valid") is True
    assert trace.gate("htf_long") is False
    assert "prior_completed_htf" in trace.warmup_blockers


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_engine_owns_entry_index_when_first_observation_is_nonflat(side: Side) -> None:
    settings = _settings(use_time_stop=True)
    engine = FiveToolEngine(settings)
    item = _inputs(
        count=1,
        settings=settings,
        account_provider=lambda _bar, _index: AccountSnapshot(position=side),
    )[0]
    engine.step(item)
    assert engine.checkpoint().active_entry_side is side
    assert engine.checkpoint().active_entry_bar_index == 0


def test_nonflat_base_failure_exit_requires_setup_and_base_pivot_metadata() -> None:
    settings = _settings(
        use_long_side_v2=True,
        use_long_base_failure_exit=True,
        use_time_stop=False,
    )
    missing_setup = _inputs(
        count=1,
        settings=settings,
        account_provider=lambda _bar, _index: AccountSnapshot(position=Side.LONG),
    )
    with pytest.raises(FiveToolInputError, match="entry_setup"):
        evaluate_batch(settings, missing_setup)

    missing_pivot = _inputs(
        count=1,
        settings=settings,
        account_provider=lambda _bar, _index: AccountSnapshot(
            position=Side.LONG,
            entry_setup=SetupFamily.BASE_BREAKOUT,
        ),
    )
    with pytest.raises(FiveToolInputError, match="base_pivot_at_entry"):
        evaluate_batch(settings, missing_pivot)

    account_pivot_only = _inputs(
        count=1,
        settings=settings,
        account_provider=lambda _bar, _index: AccountSnapshot(
            position=Side.LONG,
            entry_setup=SetupFamily.BASE_BREAKOUT,
            base_pivot_at_entry=90.0,
        ),
    )
    with pytest.raises(FiveToolInputError, match="cannot replace missing signal-bar evidence"):
        evaluate_batch(settings, account_pivot_only)

    non_base = _inputs(
        count=1,
        settings=settings,
        account_provider=lambda _bar, _index: AccountSnapshot(
            position=Side.LONG,
            entry_setup=SetupFamily.BULL_RETEST,
        ),
    )
    assert evaluate_batch(settings, non_base)[0].intent in {
        SignalIntent.NONE,
        SignalIntent.EXIT_LONG,
    }


def test_nonflat_account_rejects_future_entry_index() -> None:
    settings = _settings()
    with pytest.raises(FiveToolInputError, match="in the future"):
        evaluate_batch(
            settings,
            _inputs(
                count=1,
                settings=settings,
                account_provider=lambda _bar, _index: AccountSnapshot(
                    position=Side.SHORT,
                    entry_bar_index=1,
                ),
            ),
        )


def test_frozen_entry_metadata_rejects_caller_drift_and_keeps_exit_stable() -> None:
    settings = _settings(
        use_time_stop=True,
        max_bars_in_trade=1,
        exit_on_neutral=False,
        use_long_side_v2=False,
    )
    inputs = _inputs(count=4, settings=settings)
    engine = FiveToolEngine(settings)
    engine.step(inputs[0])

    base_pivot = inputs[0].primary.low - 1.0
    signal_bar_state = replace(
        engine.checkpoint(),
        pending_entry_side=Side.LONG,
        pending_entry_setup=SetupFamily.BASE_BREAKOUT,
        pending_base_pivot_at_entry=base_pivot,
    )
    signal_bar_json = state_to_json(signal_bar_state)
    restored_signal = state_from_json(signal_bar_json)
    assert restored_signal.pending_base_pivot_at_entry == base_pivot

    engine = FiveToolEngine(settings, state=restored_signal)
    entry_item = replace(
        inputs[1],
        account=AccountSnapshot(
            position=Side.LONG,
            entry_bar_index=1,
            entry_setup=SetupFamily.BASE_BREAKOUT,
            base_pivot_at_entry=base_pivot,
        ),
    )
    engine.step(entry_item)
    active = engine.checkpoint()
    assert (
        active.active_entry_side,
        active.active_entry_bar_index,
        active.active_entry_setup,
        active.active_base_pivot_at_entry,
    ) == (Side.LONG, 1, SetupFamily.BASE_BREAKOUT, base_pivot)
    assert active.pending_entry_side is Side.FLAT
    assert active.pending_base_pivot_at_entry is None

    active_json = state_to_json(active)
    engine = FiveToolEngine(settings, state=state_from_json(active_json))
    drift_cases = (
        AccountSnapshot(position=Side.LONG, entry_bar_index=0),
        AccountSnapshot(position=Side.LONG, entry_setup=SetupFamily.BULL_RETEST),
        AccountSnapshot(position=Side.LONG, base_pivot_at_entry=base_pivot + 1.0),
    )
    for drift in drift_cases:
        with pytest.raises(FiveToolInputError, match="contradicts frozen engine state"):
            engine.step(replace(inputs[2], account=drift))

    exit_trace = engine.step(replace(inputs[2], account=AccountSnapshot(position=Side.LONG)))
    assert exit_trace.intent is SignalIntent.EXIT_LONG
    assert exit_trace.feature("exit_reason_signal") == "time_stop"
    assert exit_trace.events[0].setup is SetupFamily.BASE_BREAKOUT

    engine.step(replace(inputs[3], account=AccountSnapshot(position=Side.FLAT)))
    cleared = engine.checkpoint()
    assert (
        cleared.active_entry_side,
        cleared.active_entry_bar_index,
        cleared.active_entry_setup,
        cleared.active_base_pivot_at_entry,
    ) == (Side.FLAT, None, SetupFamily.NONE, None)


def test_export_only_features_use_configuration_conditioned_na() -> None:
    settings = _settings(short_plus_enabled=False, show_avwap_plot=False)
    trace = evaluate_batch(
        settings,
        _inputs(
            count=1,
            settings=settings,
            account_provider=lambda _bar, _index: AccountSnapshot(
                long_virtual_equity=55_000.0,
                short_virtual_equity=45_000.0,
            ),
        ),
    )[0]
    assert trace.feature("short_continuation_score") is None
    assert isinstance(trace.feature("short_continuation_score_raw"), float)
    assert trace.feature("long_virtual_equity") is None
    assert trace.feature("short_virtual_equity") is None
    assert trace.feature("avwap_display_hidden") is False


def test_extension_export_remains_raw_when_external_regime_makes_risk_inactive() -> None:
    settings = _settings(use_external=True, extension_strength_threshold=50.0)
    inputs = tuple(
        replace(item, external_regime=-1.0) for item in _inputs(count=30, settings=settings)
    )
    traces = evaluate_batch(settings, inputs)
    assert any(
        trace.feature("extension") is True and trace.feature("extension_active") is False
        for trace in traces
    )


def test_avwap_hidden_projection_checks_show_flag_and_both_value_zone_bands() -> None:
    common = {
        "avwap_on": True,
        "close": 100.0,
        "atr": 2.0,
        "avwap": 100.0,
        "avwap_sd": 5.0,
        "maximum_atr_distance": 2.0,
    }
    assert _avwap_display_hidden(show_avwap_plot=True, **common) is True
    assert _avwap_display_hidden(show_avwap_plot=False, **common) is False
    assert (
        _avwap_display_hidden(
            show_avwap_plot=True,
            **{**common, "avwap_sd": 3.0},
        )
        is False
    )
