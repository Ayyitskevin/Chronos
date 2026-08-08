from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from chronos.research.five_tool.planning import (
    ExitOrder,
    ExitReason,
    FillEvent,
    FillPolicy,
    LegId,
    OhlcBar,
    PlanningError,
    PositionMilestones,
    PositionSide,
    SizingRejection,
    SizingRequest,
    SleeveAttribution,
    SleeveReconciliationError,
    SleeveState,
    UnsupportedMagnifierError,
    apply_fill_to_milestones,
    build_position_plan,
    milestone_stop,
    pine_quantity_plan,
    reconcile_sleeves,
    resolve_exit_fill,
)

NOW = datetime(2025, 1, 2, 21, tzinfo=UTC)


def _request(side: PositionSide, stop: float, **changes: float) -> SizingRequest:
    values = {
        "equity": 100_000.0,
        "entry_reference_price": 100.0,
        "stop_price": stop,
        "risk_pct": 1.0,
        "risk_scale": 1.0,
        "risk_multiplier": 1.0,
        "cap_pct": 100.0,
        "point_value": 1.0,
        "quantity_step": 1.0,
        "minimum_quantity": 1.0,
    }
    values.update(changes)
    return SizingRequest(side=side, **values)


def _bar(
    *,
    sequence_id: str = "daily",
    timestamp: datetime = NOW,
    open_: float = 100.0,
    high: float = 112.0,
    low: float = 88.0,
    close: float = 101.0,
) -> OhlcBar:
    return OhlcBar(sequence_id, timestamp, open_, high, low, close)


def _order(side: PositionSide = PositionSide.LONG) -> ExitOrder:
    return ExitOrder(
        position_id="p1",
        side=side,
        leg_id=LegId.LEG_1,
        quantity=10.0,
        stop_price=90.0 if side is PositionSide.LONG else 110.0,
        target_price=110.0 if side is PositionSide.LONG else 90.0,
        target_reason=ExitReason.TARGET_1,
    )


def _fill(
    *,
    fill_id: str,
    side: PositionSide,
    leg: LegId,
    reason: ExitReason,
    price: float = 100.0,
) -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        position_id="p1",
        owner_side=side,
        leg_id=leg,
        quantity=1.0,
        price=price,
        reason=reason,
        timestamp_utc=NOW,
        source_bar_id="b1",
        policy=FillPolicy.OHLC_STOP_FIRST,
        gap_through=False,
        oco_cancelled_reason=None,
    )


def test_long_and_short_sizing_use_symmetric_positive_stop_distance() -> None:
    long_plan = pine_quantity_plan(_request(PositionSide.LONG, 90.0))
    short_plan = pine_quantity_plan(_request(PositionSide.SHORT, 110.0))

    assert long_plan.accepted and short_plan.accepted
    assert long_plan.stop_distance == short_plan.stop_distance == 10.0
    assert long_plan.quantity == short_plan.quantity == 100.0


@pytest.mark.parametrize(
    ("side", "stop"),
    [
        (PositionSide.LONG, 100.0),
        (PositionSide.LONG, 110.0),
        (PositionSide.SHORT, 100.0),
        (PositionSide.SHORT, 90.0),
    ],
)
def test_both_sides_reject_zero_or_inverted_stop_geometry(side: PositionSide, stop: float) -> None:
    plan = pine_quantity_plan(_request(side, stop))
    assert plan.quantity == 0.0
    assert plan.rejection is SizingRejection.INVALID_STOP_DISTANCE


@pytest.mark.parametrize("equity", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_equity_fails_closed(equity: float) -> None:
    plan = pine_quantity_plan(_request(PositionSide.LONG, 90.0, equity=equity))
    assert plan.rejection is SizingRejection.INVALID_EQUITY
    assert plan.quantity == 0.0


def test_pine_quantity_formula_applies_point_value_multiplier_cap_and_floor() -> None:
    risk_bound = pine_quantity_plan(
        _request(
            PositionSide.LONG,
            95.0,
            equity=10_000.0,
            risk_pct=1.0,
            risk_scale=0.5,
            risk_multiplier=1.2,
            cap_pct=200.0,
            point_value=10.0,
            quantity_step=0.1,
            minimum_quantity=0.1,
        )
    )
    assert risk_bound.risk_quantity == pytest.approx(1.2)
    assert risk_bound.quantity == pytest.approx(1.1)  # Pine floor exposes float64 behavior.

    cap_bound = pine_quantity_plan(
        _request(
            PositionSide.SHORT,
            105.0,
            equity=10_000.0,
            risk_pct=5.0,
            cap_pct=10.0,
            quantity_step=3.0,
        )
    )
    assert cap_bound.cap_quantity == 10.0
    assert cap_bound.quantity == 9.0


def test_negative_risk_multiplier_matches_pine_clamp_and_rejects_below_minimum() -> None:
    plan = pine_quantity_plan(_request(PositionSide.LONG, 90.0, risk_multiplier=-1.0))
    assert plan.rejection is SizingRejection.BELOW_MINIMUM


def test_one_leg_plan_targets_t2_and_three_leg_plan_has_explicit_milestones() -> None:
    one = build_position_plan(
        pine_quantity_plan(
            _request(
                PositionSide.LONG,
                90.0,
                equity=2_000.0,
                risk_pct=1.0,
                cap_pct=200.0,
            )
        )
    )
    assert [(leg.leg_id, leg.quantity, leg.target_reason) for leg in one.legs] == [
        (LegId.LEG_1, 2.0, ExitReason.TARGET_2)
    ]
    assert one.legs[0].target_price == 120.0

    three = build_position_plan(
        pine_quantity_plan(
            _request(
                PositionSide.SHORT,
                110.0,
                equity=90_000.0,
                risk_pct=0.1,
                cap_pct=200.0,
            )
        )
    )
    assert [leg.quantity for leg in three.legs] == [3.0, 3.0, 3.0]
    assert [leg.target_reason for leg in three.legs] == [
        ExitReason.TARGET_1,
        ExitReason.TARGET_2,
        None,
    ]
    assert [leg.target_price for leg in three.legs] == [90.0, 80.0, None]


def test_same_bar_priority_is_policy_driven_and_oco_returns_one_fill() -> None:
    stop_first = resolve_exit_fill(_order(), _bar(), policy=FillPolicy.OHLC_STOP_FIRST)
    target_first = resolve_exit_fill(_order(), _bar(), policy=FillPolicy.OHLC_TARGET_FIRST)
    assert stop_first is not None and target_first is not None
    assert (stop_first.reason, stop_first.price, stop_first.oco_cancelled_reason) == (
        ExitReason.INITIAL_STOP,
        90.0,
        ExitReason.TARGET_1,
    )
    assert (target_first.reason, target_first.price, target_first.oco_cancelled_reason) == (
        ExitReason.TARGET_1,
        110.0,
        ExitReason.INITIAL_STOP,
    )


def test_short_same_bar_priority_is_symmetric() -> None:
    stop_fill = resolve_exit_fill(
        _order(PositionSide.SHORT), _bar(), policy=FillPolicy.OHLC_STOP_FIRST
    )
    target_fill = resolve_exit_fill(
        _order(PositionSide.SHORT), _bar(), policy=FillPolicy.OHLC_TARGET_FIRST
    )
    assert stop_fill is not None and stop_fill.price == 110.0
    assert target_fill is not None and target_fill.price == 90.0


def test_exit_orders_and_fill_events_reject_impossible_attribution() -> None:
    with pytest.raises(PlanningError, match="long target"):
        ExitOrder(
            position_id="p1",
            side=PositionSide.LONG,
            leg_id=LegId.LEG_1,
            quantity=1.0,
            stop_price=100.0,
            target_price=90.0,
            target_reason=ExitReason.TARGET_1,
        )
    with pytest.raises(PlanningError, match="protective stop"):
        ExitOrder(
            position_id="p1",
            side=PositionSide.LONG,
            leg_id=LegId.LEG_1,
            quantity=1.0,
            stop_price=90.0,
            stop_reason=ExitReason.TARGET_1,
        )
    with pytest.raises(PlanningError, match="TARGET_1"):
        _fill(
            fill_id="bad-target",
            side=PositionSide.LONG,
            leg=LegId.LEG_2,
            reason=ExitReason.TARGET_1,
        )


def test_gap_through_stop_and_target_fill_at_better_or_worse_open() -> None:
    stop = resolve_exit_fill(
        _order(),
        _bar(open_=85.0, high=95.0, low=80.0, close=90.0),
        policy=FillPolicy.OHLC_TARGET_FIRST,
    )
    target = resolve_exit_fill(
        _order(),
        _bar(open_=115.0, high=120.0, low=112.0, close=118.0),
        policy=FillPolicy.OHLC_STOP_FIRST,
    )
    assert stop is not None and (stop.price, stop.gap_through) == (85.0, True)
    assert target is not None and (target.price, target.gap_through) == (115.0, True)


def test_magnifier_requires_complete_lower_timeframe_coverage() -> None:
    with pytest.raises(UnsupportedMagnifierError):
        resolve_exit_fill(_order(), _bar(), policy=FillPolicy.LOWER_TIMEFRAME_MAGNIFIER)
    with pytest.raises(UnsupportedMagnifierError):
        resolve_exit_fill(
            _order(),
            _bar(),
            policy=FillPolicy.LOWER_TIMEFRAME_MAGNIFIER,
            lower_timeframe_bars=[_bar(sequence_id="partial", high=105.0, low=95.0)],
        )


def test_magnifier_uses_chronological_subbars_before_parent_ambiguity() -> None:
    parent = _bar(open_=100.0, high=112.0, low=88.0, close=95.0)
    first = _bar(
        sequence_id="m1",
        timestamp=NOW - timedelta(minutes=1),
        open_=100.0,
        high=112.0,
        low=99.0,
        close=109.0,
    )
    second = _bar(sequence_id="m2", open_=109.0, high=110.0, low=88.0, close=95.0)
    fill = resolve_exit_fill(
        _order(),
        parent,
        policy=FillPolicy.LOWER_TIMEFRAME_MAGNIFIER,
        lower_timeframe_bars=(first, second),
    )
    assert fill is not None
    assert fill.source_bar_id == "m1"
    assert fill.reason is ExitReason.TARGET_1
    assert fill.policy is FillPolicy.LOWER_TIMEFRAME_MAGNIFIER


def test_only_an_actual_target_1_fill_arms_breakeven() -> None:
    state = PositionMilestones("p1", PositionSide.LONG)
    stopped = apply_fill_to_milestones(
        state,
        _fill(
            fill_id="stop",
            side=PositionSide.LONG,
            leg=LegId.LEG_1,
            reason=ExitReason.INITIAL_STOP,
            price=90.0,
        ),
    )
    assert not stopped.target_1_filled
    assert not stopped.break_even_armed

    target_2 = apply_fill_to_milestones(
        state,
        _fill(
            fill_id="single-t2",
            side=PositionSide.LONG,
            leg=LegId.LEG_1,
            reason=ExitReason.TARGET_2,
            price=120.0,
        ),
    )
    assert not target_2.break_even_armed

    target_1 = apply_fill_to_milestones(
        state,
        _fill(
            fill_id="t1",
            side=PositionSide.LONG,
            leg=LegId.LEG_1,
            reason=ExitReason.TARGET_1,
            price=110.0,
        ),
    )
    assert target_1.target_1_filled and target_1.break_even_armed
    assert (
        milestone_stop(
            target_1,
            current_stop=90.0,
            entry_price=100.0,
            risk_distance=10.0,
            break_even_offset_r=0.1,
        )
        == 101.0
    )
    with pytest.raises(PlanningError, match="duplicate"):
        apply_fill_to_milestones(
            target_1,
            _fill(
                fill_id="duplicate",
                side=PositionSide.LONG,
                leg=LegId.LEG_1,
                reason=ExitReason.TARGET_1,
            ),
        )


def test_short_breakeven_tightens_downward_without_loosening() -> None:
    state = PositionMilestones(
        "p1", PositionSide.SHORT, target_1_filled=True, break_even_armed=True
    )
    assert (
        milestone_stop(
            state,
            current_stop=110.0,
            entry_price=100.0,
            risk_distance=10.0,
            break_even_offset_r=0.1,
        )
        == 99.0
    )
    assert milestone_stop(state, current_stop=95.0, entry_price=100.0, risk_distance=10.0) == 95.0


def test_direct_reversal_reconciles_each_fill_to_its_owned_sleeve() -> None:
    long_close = _fill(
        fill_id="long-close",
        side=PositionSide.LONG,
        leg=LegId.LEG_1,
        reason=ExitReason.DIRECT_REVERSAL,
    )
    short_open_cost = _fill(
        fill_id="short-open",
        side=PositionSide.SHORT,
        leg=LegId.LEG_1,
        reason=ExitReason.DIRECT_REVERSAL,
    )
    reconciled = reconcile_sleeves(
        SleeveState(total_equity=100.0, long_equity=60.0, short_equity=40.0),
        new_total_equity=109.0,
        fills=(long_close, short_open_cost),
        attributions=(
            SleeveAttribution(PositionSide.LONG, 10.0, "long realized", "long-close"),
            SleeveAttribution(PositionSide.SHORT, -1.0, "short entry cost", "short-open"),
        ),
    )
    assert reconciled == SleeveState(109.0, 70.0, 39.0)


def test_sleeve_reconciliation_rejects_side_switch_misattribution() -> None:
    fill = _fill(
        fill_id="long-close",
        side=PositionSide.LONG,
        leg=LegId.LEG_1,
        reason=ExitReason.DIRECT_REVERSAL,
    )
    with pytest.raises(SleeveReconciliationError, match="disagrees"):
        reconcile_sleeves(
            SleeveState(100.0, 60.0, 40.0),
            new_total_equity=101.0,
            fills=(fill,),
            attributions=(
                SleeveAttribution(PositionSide.SHORT, 1.0, "wrong sleeve", "long-close"),
            ),
        )
