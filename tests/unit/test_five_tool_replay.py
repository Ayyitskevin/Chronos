from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.research.five_tool import IncompleteReplayError as PublicIncompleteReplayError
from chronos.research.five_tool.alignment import align_five_tool_inputs, source_bar_id
from chronos.research.five_tool.models import (
    AccountSnapshot,
    FiveToolBarInput,
    FiveToolSettings,
    Side,
    SignalIntent,
)
from chronos.research.five_tool.planning import (
    ExitReason,
    FillPolicy,
    LegId,
    OhlcBar,
    PositionSide,
    UnsupportedMagnifierError,
)
from chronos.research.five_tool.replay import (
    FiveToolReplayPolicy,
    IncompleteReplayError,
    ReplayBar,
    ReplayInputError,
    TerminalPositionPolicy,
    replay_five_tool,
)

START = datetime(2020, 1, 2, 21, tzinfo=UTC)


def _series(
    symbol: str,
    *,
    count: int,
    direction: int = 1,
    price_scale: float = 1.0,
) -> BarSeries:
    bars: list[Bar] = []
    for index in range(count):
        timestamp = START + timedelta(days=index)
        if direction > 0:
            raw_close = 100.0 + index * 0.55 + (0.35 if index % 2 else -0.25)
            open_offset = -0.15
        else:
            raw_close = 100.0 - index * 0.55 + (0.25 if index % 2 == 0 else -0.35)
            open_offset = 0.15
        close = raw_close * price_scale
        bars.append(
            Bar(
                symbol=symbol,
                source="internal_spec",
                exchange="AMEX" if symbol == "SPY" else "NYSE",
                interval=BarInterval.DAY_1,
                session_date=timestamp.date(),
                timestamp_utc=timestamp,
                open=close + open_offset,
                high=close + 0.8,
                low=close - 0.8,
                close=close,
                volume=1_000_000.0 + index * 100.0,
            )
        )
    return BarSeries(symbol=symbol, interval=BarInterval.DAY_1, bars=tuple(bars))


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
        "use_external": True,
        "use_ext_strength": True,
        "trig_hidden": False,
        "trig_regular": False,
        "trig_reclaim": False,
        "use_value_zone": False,
        "use_benchmark_filter": False,
        "use_htf_filter": False,
        "veto_laggard": False,
        "use_trail": False,
        "allow_shorts": False,
        "cooldown_bars_after_trade": 10,
    }
    overrides.update(extra)
    return FiveToolSettings.defaults(history_start_utc=START, overrides=overrides)


def _inputs(
    settings: FiveToolSettings,
    *,
    count: int,
    direction: int = 1,
    hostile_equity: float = 7.0,
) -> tuple[FiveToolBarInput, ...]:
    aligned = align_five_tool_inputs(
        settings,
        _series("AAA", count=count, direction=direction),
        _series("SPY", count=count, direction=direction, price_scale=4.0),
    )
    selected_regime = 1.0 if direction > 0 else -1.0
    hostile_side = Side.SHORT if direction > 0 else Side.LONG
    return tuple(
        replace(
            item,
            external_regime=0.0 if index < 20 else selected_regime,
            external_strength=50.0,
            account=AccountSnapshot(equity=hostile_equity, position=hostile_side),
        )
        for index, item in enumerate(aligned)
    )


def _replay_bars(
    inputs: tuple[FiveToolBarInput, ...],
    *,
    replacements: dict[int, dict[str, float]] | None = None,
) -> tuple[ReplayBar, ...]:
    result: list[ReplayBar] = []
    for index, item in enumerate(inputs):
        if replacements and index in replacements:
            changes = replacements[index]
            item = replace(
                item,
                primary=replace(
                    item.primary,
                    open=changes.get("open", item.primary.open),
                    high=changes.get("high", item.primary.high),
                    low=changes.get("low", item.primary.low),
                    close=changes.get("close", item.primary.close),
                ),
            )
        result.append(
            ReplayBar(
                item,
                open_timestamp_utc=item.primary.timestamp_utc - timedelta(hours=6),
            )
        )
    return tuple(result)


def _time_stop_settings(**extra: bool | int | float | str) -> FiveToolSettings:
    return _settings(**{"use_time_stop": True, "max_bars_in_trade": 2, **extra})


def _magnified_replay_bars(inputs: tuple[FiveToolBarInput, ...]) -> tuple[ReplayBar, ...]:
    result: list[ReplayBar] = []
    for replay_bar in _replay_bars(inputs):
        primary = replay_bar.input.primary
        opened = replay_bar.open_timestamp_utc
        midpoint = opened + (primary.timestamp_utc - opened) / 2
        lower = (
            OhlcBar(
                sequence_id=f"{primary.sequence_id}:lower:0",
                timestamp_utc=midpoint,
                open=primary.open,
                high=primary.high,
                low=primary.open,
                close=primary.open,
                start_timestamp_utc=opened,
                symbol=primary.symbol,
                source=primary.source,
                interval="3h",
            ),
            OhlcBar(
                sequence_id=f"{primary.sequence_id}:lower:1",
                timestamp_utc=primary.timestamp_utc,
                open=primary.open,
                high=max(primary.open, primary.close),
                low=primary.low,
                close=primary.close,
                start_timestamp_utc=midpoint,
                symbol=primary.symbol,
                source=primary.source,
                interval="3h",
            ),
        )
        result.append(replace(replay_bar, lower_timeframe_bars=lower))
    return tuple(result)


def test_replay_owns_account_and_emits_complete_next_open_evidence_with_exact_costs() -> None:
    settings = _time_stop_settings()
    bars = _replay_bars(_inputs(settings, count=25))
    policy = FiveToolReplayPolicy()

    result = replay_five_tool(settings, bars, policy=policy)

    entry_trace = next(trace for trace in result.traces if trace.intent is SignalIntent.ENTER_LONG)
    exit_trace = next(trace for trace in result.traces if trace.intent is SignalIntent.EXIT_LONG)
    plan = result.position_plans[0]
    assert result.account_snapshots[0] == AccountSnapshot(equity=policy.initial_equity)
    assert result.account_snapshots[entry_trace.bar_index + 1].position is Side.LONG
    assert plan.signal_quantity_plan.request.equity == policy.initial_equity
    assert plan.signal_timestamp_utc == entry_trace.timestamp_utc
    assert plan.entry_timestamp_utc == bars[entry_trace.bar_index + 1].open_timestamp_utc
    assert {fill.timestamp_utc for fill in result.entry_fills} == {plan.entry_timestamp_utc}
    assert {fill.fill.timestamp_utc for fill in result.exit_fills} == {
        bars[exit_trace.bar_index + 1].open_timestamp_utc
    }
    assert len(result.entry_fills) == len(result.closed_legs) == 3
    assert len(result.economic_positions) == 1
    assert all(leg.exit_reason is ExitReason.TIME_STOP for leg in result.closed_legs)

    entry_by_leg = {fill.leg_id: fill for fill in result.entry_fills}
    exit_by_leg = {receipt.fill.leg_id: receipt for receipt in result.exit_fills}
    sign = 1.0
    for leg in result.closed_legs:
        entry = entry_by_leg[leg.leg_id]
        exit_receipt = exit_by_leg[leg.leg_id]
        expected_gross = (
            sign
            * (exit_receipt.fill.price - entry.raw_price)
            * entry.quantity
            * settings.point_value
        )
        assert leg.gross_pnl == pytest.approx(expected_gross)
        assert leg.commission_cost == pytest.approx(
            entry.commission_cost + exit_receipt.commission_cost
        )
        assert leg.slippage_cost == pytest.approx(entry.slippage_cost + exit_receipt.slippage_cost)
        assert leg.turnover == pytest.approx(
            (entry.execution_price + exit_receipt.execution_price)
            * entry.quantity
            * settings.point_value
        )
    assert result.ending_equity == pytest.approx(
        policy.initial_equity + math.fsum(leg.net_pnl for leg in result.closed_legs)
    )
    assert result.execution_parity == "UNVERIFIED"
    assert result.policy_digest == policy.digest

    different_caller_accounts = _replay_bars(
        _inputs(settings, count=25, hostile_equity=999_999_999.0)
    )
    replayed = replay_five_tool(settings, different_caller_accounts, policy=policy)
    assert replayed == result
    assert replayed.digest == result.digest


@pytest.mark.parametrize(
    ("fill_policy", "expected_reasons"),
    [
        (
            FillPolicy.OHLC_STOP_FIRST,
            (ExitReason.INITIAL_STOP,) * 3,
        ),
        (
            FillPolicy.OHLC_TARGET_FIRST,
            (ExitReason.TARGET_1, ExitReason.TARGET_2, ExitReason.INITIAL_STOP),
        ),
    ],
)
def test_entry_bar_targets_stay_inactive_and_later_ambiguity_uses_frozen_priority(
    fill_policy: FillPolicy,
    expected_reasons: tuple[ExitReason, ...],
) -> None:
    settings = _settings()
    inputs = _inputs(settings, count=23)
    bars = _replay_bars(
        inputs,
        replacements={
            # Targets are not yet active even though the entry bar reaches both.
            21: {"high": 200.0},
            22: {"high": 200.0, "low": 1.0},
        },
    )
    result = replay_five_tool(
        settings,
        bars,
        policy=FiveToolReplayPolicy(fill_policy=fill_policy),
    )

    entry_time = result.position_plans[0].entry_timestamp_utc
    assert entry_time == bars[21].open_timestamp_utc
    assert all(
        receipt.fill.source_bar_id != result.traces[21].primary_sequence_id
        for receipt in result.exit_fills
    )
    assert all(
        receipt.fill.source_bar_id == result.traces[22].primary_sequence_id
        for receipt in result.exit_fills
    )
    assert tuple(leg.exit_reason for leg in result.closed_legs) == expected_reasons
    assert len(result.economic_positions) == 1


@pytest.mark.parametrize(
    ("direction", "gap_through"),
    [(1, False), (1, True), (-1, False), (-1, True)],
)
def test_entry_bar_signal_stop_resolves_every_leg_for_long_short_touch_and_gap(
    direction: int,
    gap_through: bool,
) -> None:
    settings = _settings(
        allow_shorts=direction < 0,
        use_short_side_v2=False,
    )
    inputs = _inputs(settings, count=22, direction=direction)
    baseline = replay_five_tool(
        settings,
        _replay_bars(inputs),
        policy=FiveToolReplayPolicy(
            terminal_position_policy=TerminalPositionPolicy.EXCLUDE_INCOMPLETE
        ),
    )
    signal_stop = baseline.position_plans[0].signal_quantity_plan.request.stop_price
    bars = list(_replay_bars(inputs))
    primary = bars[21].input.primary
    if direction > 0:
        opened = signal_stop - 1.0 if gap_through else signal_stop + 1.0
        changed = replace(
            primary,
            open=opened,
            high=opened + 0.5,
            low=opened - 0.5 if gap_through else signal_stop - 0.1,
            close=opened,
        )
    else:
        opened = signal_stop + 1.0 if gap_through else signal_stop - 1.0
        changed = replace(
            primary,
            open=opened,
            high=opened + 0.5 if gap_through else signal_stop + 0.1,
            low=opened - 0.5,
            close=opened,
        )
    bars[21] = replace(bars[21], input=replace(bars[21].input, primary=changed))

    result = replay_five_tool(settings, bars, policy=FiveToolReplayPolicy())

    assert len(result.entry_fills) == len(result.exit_fills) == len(result.closed_legs) == 3
    assert {receipt.fill.reason for receipt in result.exit_fills} == {ExitReason.INITIAL_STOP}
    assert {receipt.fill.price for receipt in result.exit_fills} == {
        opened if gap_through else signal_stop
    }
    assert {receipt.fill.gap_through for receipt in result.exit_fills} == {gap_through}
    assert {receipt.fill.timestamp_utc for receipt in result.exit_fills} == {
        bars[21].open_timestamp_utc if gap_through else bars[21].input.primary.timestamp_utc
    }
    assert all(receipt.fill.oco_cancelled_reason is None for receipt in result.exit_fills)
    assert all(
        receipt.fill.source_bar_id == result.traces[21].primary_sequence_id
        for receipt in result.exit_fills
    )


def test_target_one_fill_can_move_break_even_only_for_a_later_bar() -> None:
    settings = _settings()
    inputs = _inputs(settings, count=24)
    bars = _replay_bars(
        inputs,
        replacements={
            # This bar reaches T1 and also trades below the future BE price.  The
            # explicit T1 evidence is not allowed to rewrite its own bar's orders.
            22: {"high": 118.0, "low": 110.0},
            23: {"high": 114.0, "low": 110.0},
        },
    )
    result = replay_five_tool(
        settings,
        bars,
        policy=FiveToolReplayPolicy(fill_policy=FillPolicy.OHLC_STOP_FIRST),
    )

    by_leg = {leg.leg_id: leg for leg in result.closed_legs}
    assert by_leg[LegId.LEG_1].exit_reason is ExitReason.TARGET_1
    assert by_leg[LegId.LEG_1].exit_time_utc == bars[22].input.primary.timestamp_utc
    assert by_leg[LegId.LEG_2].exit_reason is ExitReason.BREAKEVEN_STOP
    assert by_leg[LegId.LEG_3].exit_reason is ExitReason.BREAKEVEN_STOP
    assert by_leg[LegId.LEG_2].exit_time_utc == bars[23].input.primary.timestamp_utc
    assert by_leg[LegId.LEG_3].exit_time_utc == bars[23].input.primary.timestamp_utc


def test_entry_close_trailing_update_can_fill_runner_only_on_a_later_bar() -> None:
    settings = _time_stop_settings(
        max_bars_in_trade=1,
        use_trail=True,
        ch_len=1,
        ch_mult=0.5,
        trail_after_r=0.0,
    )
    inputs = _inputs(settings, count=24)
    bars = _replay_bars(
        inputs,
        replacements={22: {"high": 114.0, "low": 107.0}},
    )
    result = replay_five_tool(settings, bars, policy=FiveToolReplayPolicy())

    by_leg = {leg.leg_id: leg for leg in result.closed_legs}
    assert by_leg[LegId.LEG_3].exit_reason is ExitReason.TRAILING_STOP
    assert by_leg[LegId.LEG_3].exit_time_utc == bars[22].open_timestamp_utc
    assert by_leg[LegId.LEG_1].exit_reason is ExitReason.TIME_STOP
    assert by_leg[LegId.LEG_2].exit_reason is ExitReason.TIME_STOP
    assert by_leg[LegId.LEG_1].exit_time_utc == bars[23].open_timestamp_utc
    assert by_leg[LegId.LEG_2].exit_time_utc == bars[23].open_timestamp_utc


def test_gap_entry_rebases_active_ladder_but_does_not_resize_signal_risk() -> None:
    settings = _time_stop_settings()
    inputs = _inputs(settings, count=25)
    baseline = replay_five_tool(settings, _replay_bars(inputs), policy=FiveToolReplayPolicy())
    original = inputs[21].primary
    gap_open = original.open + 7.0
    gap = replay_five_tool(
        settings,
        _replay_bars(
            inputs,
            replacements={21: {"open": gap_open, "high": gap_open + 1.0}},
        ),
        policy=FiveToolReplayPolicy(),
    )

    before = baseline.position_plans[0]
    after = gap.position_plans[0]
    assert after.signal_quantity_plan.quantity == before.signal_quantity_plan.quantity
    assert after.signal_quantity_plan.stop_distance == before.signal_quantity_plan.stop_distance
    assert after.filled_position_plan.entry_reference_price == pytest.approx(gap_open + 0.02)
    stop_distance = after.signal_quantity_plan.stop_distance
    assert stop_distance is not None
    assert after.filled_position_plan.initial_stop_price == pytest.approx(
        after.filled_position_plan.entry_reference_price - stop_distance
    )
    assert all(
        after_leg.quantity == before_leg.quantity
        for after_leg, before_leg in zip(
            after.filled_position_plan.legs,
            before.filled_position_plan.legs,
            strict=True,
        )
    )


def test_gap_through_protective_stop_fills_at_exact_next_open_with_adverse_slippage() -> None:
    settings = _settings()
    inputs = _inputs(settings, count=23)
    bars = _replay_bars(
        inputs,
        replacements={22: {"open": 100.0, "high": 113.0, "low": 99.0}},
    )
    result = replay_five_tool(settings, bars, policy=FiveToolReplayPolicy())

    assert all(receipt.fill.gap_through for receipt in result.exit_fills)
    assert {receipt.fill.price for receipt in result.exit_fills} == {100.0}
    assert {receipt.fill.timestamp_utc for receipt in result.exit_fills} == {
        bars[22].open_timestamp_utc
    }
    assert all(receipt.execution_price == 99.98 for receipt in result.exit_fills)


def test_short_replay_is_symmetric_and_charges_adverse_entry_and_exit_slippage() -> None:
    settings = _time_stop_settings(
        allow_shorts=True,
        use_short_side_v2=False,
        max_bars_in_trade=1,
    )
    result = replay_five_tool(
        settings,
        _replay_bars(_inputs(settings, count=24, direction=-1)),
        policy=FiveToolReplayPolicy(),
    )

    plan = result.position_plans[0].filled_position_plan
    assert plan.side is PositionSide.SHORT
    assert plan.initial_stop_price > plan.entry_reference_price
    assert all(
        leg.target_price is None or leg.target_price < plan.entry_reference_price
        for leg in plan.legs
    )
    assert all(
        fill.execution_price == pytest.approx(fill.raw_price - 0.02) for fill in result.entry_fills
    )
    assert all(
        receipt.execution_price == pytest.approx(receipt.fill.price + 0.02)
        for receipt in result.exit_fills
    )
    assert all(leg.side is PositionSide.SHORT for leg in result.closed_legs)
    assert result.ending_equity == pytest.approx(
        100_000.0 + math.fsum(leg.net_pnl for leg in result.closed_legs)
    )


def test_single_leg_position_remains_one_economic_observation() -> None:
    settings = _time_stop_settings()
    result = replay_five_tool(
        settings,
        _replay_bars(_inputs(settings, count=25)),
        policy=FiveToolReplayPolicy(initial_equity=1_500.0),
    )

    assert len(result.position_plans[0].filled_position_plan.legs) == 1
    assert [fill.leg_id for fill in result.entry_fills] == [LegId.LEG_1]
    assert len(result.closed_legs) == len(result.economic_positions) == 1
    assert result.economic_positions[0].leg_count == 1


def test_contract_point_value_always_scales_economics_even_when_sizing_toggle_is_off() -> None:
    settings = replace(
        _time_stop_settings(use_pointvalue_sizing=False),
        point_value=50.0,
    )
    result = replay_five_tool(
        settings,
        _replay_bars(_inputs(settings, count=25)),
        policy=FiveToolReplayPolicy(),
    )

    plan = result.position_plans[0]
    assert plan.signal_quantity_plan.request.point_value == 1.0
    entry = result.entry_fills[0]
    exit_receipt = result.exit_fills[0]
    leg = result.closed_legs[0]
    assert leg.gross_pnl == pytest.approx(
        (exit_receipt.fill.price - entry.raw_price) * entry.quantity * 50.0
    )
    assert leg.turnover == pytest.approx(
        (entry.execution_price + exit_receipt.execution_price) * entry.quantity * 50.0
    )


def test_terminal_policy_refuses_or_excludes_a_partially_closed_position() -> None:
    settings = _settings()
    inputs = _inputs(settings, count=23)
    # The first target trades, but neither T2 nor the stop does.
    bars = _replay_bars(inputs, replacements={22: {"high": 118.0, "low": 110.0}})

    with pytest.raises(IncompleteReplayError, match="incomplete position"):
        replay_five_tool(settings, bars, policy=FiveToolReplayPolicy())

    result = replay_five_tool(
        settings,
        bars,
        policy=FiveToolReplayPolicy(
            fill_policy=FillPolicy.OHLC_TARGET_FIRST,
            terminal_position_policy=TerminalPositionPolicy.EXCLUDE_INCOMPLETE,
        ),
    )
    assert [leg.exit_reason for leg in result.closed_legs] == [ExitReason.TARGET_1]
    assert result.validation_closed_legs == ()
    assert result.economic_positions == ()
    assert result.excluded_incomplete_position_ids == (result.position_plans[0].position_id,)
    assert result.terminal_open_leg_ids == (LegId.LEG_2, LegId.LEG_3)


def test_terminal_policy_also_tracks_an_entry_signal_with_no_next_open() -> None:
    settings = _settings()
    bars = _replay_bars(_inputs(settings, count=21))
    with pytest.raises(IncompleteReplayError):
        replay_five_tool(settings, bars, policy=FiveToolReplayPolicy())

    result = replay_five_tool(
        settings,
        bars,
        policy=FiveToolReplayPolicy(
            terminal_position_policy=TerminalPositionPolicy.EXCLUDE_INCOMPLETE
        ),
    )
    assert result.position_plans == ()
    assert result.terminal_pending_entry_position_id is not None
    assert result.excluded_incomplete_position_ids == (result.terminal_pending_entry_position_id,)


def test_blended_side_equity_is_adapter_owned_and_reconciles_every_bar() -> None:
    settings = _time_stop_settings(
        use_blended_capital_split=True,
        long_capital_alloc_pct=65.0,
    )
    result = replay_five_tool(
        settings,
        _replay_bars(_inputs(settings, count=25)),
        policy=FiveToolReplayPolicy(),
    )

    for point in result.equity_curve:
        assert point.long_virtual_equity is not None
        assert point.short_virtual_equity is not None
        assert point.long_virtual_equity + point.short_virtual_equity == pytest.approx(
            point.total_equity
        )
    assert result.equity_curve[-1].short_virtual_equity == 35_000.0


def test_replay_boundaries_reject_string_policies_and_untyped_magnifier_bars() -> None:
    settings = _settings()
    item = _inputs(settings, count=1)[0]
    opened = item.primary.timestamp_utc - timedelta(hours=6)

    with pytest.raises(ValueError, match="fill policy"):
        FiveToolReplayPolicy(fill_policy=cast(FillPolicy, "ohlc_stop_first"))
    with pytest.raises(ValueError, match="terminal position policy"):
        FiveToolReplayPolicy(terminal_position_policy=cast(TerminalPositionPolicy, "require_flat"))
    with pytest.raises(ValueError, match="tuple of OhlcBar"):
        ReplayBar(item, opened, lower_timeframe_bars=cast(tuple[OhlcBar, ...], []))
    with pytest.raises(ValueError, match="tuple of OhlcBar"):
        ReplayBar(
            item,
            opened,
            lower_timeframe_bars=cast(tuple[OhlcBar, ...], ("not-an-ohlc-bar",)),
        )


def test_public_api_exports_incomplete_replay_error() -> None:
    assert PublicIncompleteReplayError is IncompleteReplayError


def test_replay_requires_full_history_from_origin_and_has_no_suffix_resume() -> None:
    settings = _settings(enable_orders=False)
    bars = _replay_bars(_inputs(settings, count=3))

    with pytest.raises(ReplayInputError, match="non-empty full history"):
        replay_five_tool(settings, (), policy=FiveToolReplayPolicy())
    with pytest.raises(ReplayInputError, match=r"full history prefix.*checkpoint/resume"):
        replay_five_tool(settings, bars[1:], policy=FiveToolReplayPolicy())


@pytest.mark.parametrize("incomplete_kind", ["missing", "partial"])
def test_magnifier_fails_closed_on_incomplete_coverage_for_every_flat_replay_bar(
    incomplete_kind: str,
) -> None:
    settings = _settings(enable_orders=False)
    bars = list(_magnified_replay_bars(_inputs(settings, count=5)))
    policy = FiveToolReplayPolicy(fill_policy=FillPolicy.LOWER_TIMEFRAME_MAGNIFIER)
    assert len(replay_five_tool(settings, bars, policy=policy).traces) == len(bars)

    bars[3] = replace(
        bars[3],
        lower_timeframe_bars=(
            () if incomplete_kind == "missing" else bars[3].lower_timeframe_bars[:1]
        ),
    )
    with pytest.raises(UnsupportedMagnifierError):
        replay_five_tool(settings, bars, policy=policy)


def test_replay_rejects_lower_sequence_identity_reused_across_parent_bars() -> None:
    settings = _settings(enable_orders=False)
    bars = list(_magnified_replay_bars(_inputs(settings, count=5)))
    first_lower_id = bars[0].lower_timeframe_bars[0].sequence_id
    bars[1] = replace(
        bars[1],
        lower_timeframe_bars=(
            replace(bars[1].lower_timeframe_bars[0], sequence_id=first_lower_id),
            bars[1].lower_timeframe_bars[1],
        ),
    )

    with pytest.raises(ReplayInputError, match="globally unique"):
        replay_five_tool(
            settings,
            bars,
            policy=FiveToolReplayPolicy(fill_policy=FillPolicy.LOWER_TIMEFRAME_MAGNIFIER),
        )


def test_replay_rejects_lower_sequence_identity_equal_to_its_parent() -> None:
    settings = _settings(enable_orders=False)
    bars = list(_magnified_replay_bars(_inputs(settings, count=5)))
    bars[0] = replace(
        bars[0],
        lower_timeframe_bars=(
            bars[0].lower_timeframe_bars[0],
            replace(
                bars[0].lower_timeframe_bars[1],
                sequence_id=source_bar_id(bars[0].input.primary),
            ),
        ),
    )

    with pytest.raises(UnsupportedMagnifierError, match="sequence identities"):
        replay_five_tool(
            settings,
            bars,
            policy=FiveToolReplayPolicy(fill_policy=FillPolicy.LOWER_TIMEFRAME_MAGNIFIER),
        )


def test_result_digest_binds_effective_input_open_and_lower_ohlc_identity_evidence() -> None:
    settings = _settings(enable_orders=False)
    ordinary = list(_replay_bars(_inputs(settings, count=5)))
    baseline = replay_five_tool(settings, ordinary, policy=FiveToolReplayPolicy())

    ordinary[2] = replace(
        ordinary[2],
        open_timestamp_utc=ordinary[2].open_timestamp_utc + timedelta(minutes=1),
    )
    changed_open = replay_five_tool(settings, ordinary, policy=FiveToolReplayPolicy())

    identity_bars = list(_replay_bars(_inputs(settings, count=5)))
    benchmark = identity_bars[2].input.benchmark
    assert benchmark is not None
    identity_bars[2] = replace(
        identity_bars[2],
        input=replace(
            identity_bars[2].input,
            benchmark=replace(
                benchmark,
                source_sequence_id=benchmark.source_sequence_id + ":identity-change",
            ),
        ),
    )
    changed_identity = replay_five_tool(
        settings,
        identity_bars,
        policy=FiveToolReplayPolicy(),
    )

    magnified = list(_magnified_replay_bars(_inputs(settings, count=5)))
    magnifier_policy = FiveToolReplayPolicy(fill_policy=FillPolicy.LOWER_TIMEFRAME_MAGNIFIER)
    magnifier_baseline = replay_five_tool(settings, magnified, policy=magnifier_policy)
    first, second = magnified[2].lower_timeframe_bars
    changed_lower = (
        replace(first, high=max(first.open, first.close)),
        replace(second, high=magnified[2].input.primary.high),
    )
    magnified[2] = replace(magnified[2], lower_timeframe_bars=changed_lower)
    changed_lower_evidence = replay_five_tool(
        settings,
        magnified,
        policy=magnifier_policy,
    )

    ordinary_digests = {
        baseline.replay_input_digest,
        changed_open.replay_input_digest,
        changed_identity.replay_input_digest,
    }
    assert len(ordinary_digests) == 3
    assert magnifier_baseline.replay_input_digest != changed_lower_evidence.replay_input_digest
    assert baseline.digest != changed_open.digest != changed_identity.digest
    assert magnifier_baseline.digest != changed_lower_evidence.digest


def test_default_policy_payload_matches_checked_campaign_manifest() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads(
        (root / "research/five_tool_v3_6_campaign_manifest.json").read_text(encoding="utf-8")
    )
    policy = FiveToolReplayPolicy()

    assert manifest["replay_policy"]["canonical"] == policy.canonical_payload
    assert manifest["replay_policy"]["sha256"] == policy.digest
