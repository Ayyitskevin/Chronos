"""Pure Five-Tool sizing, leg planning, and OHLC fill approximations.

This module is deliberately research-only.  It does not import a broker, an order
gateway, or any production execution type.  Pine's risk-sizing arithmetic is kept
separate from the explicitly approximate OHLC fill model so a caller cannot mistake
chart-bar simulation for executable order logic.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


class LegId(StrEnum):
    LEG_1 = "leg_1"
    LEG_2 = "leg_2"
    LEG_3 = "leg_3"


class ExitReason(StrEnum):
    """Semantic exit attribution; never inferred from a missing leg."""

    INITIAL_STOP = "initial_stop"
    TARGET_1 = "target_1"
    TARGET_2 = "target_2"
    BREAKEVEN_STOP = "breakeven_stop"
    TRAILING_STOP = "trailing_stop"
    REGIME_EXIT = "regime_exit"
    AVWAP_EXIT = "avwap_exit"
    RELATIVE_STRENGTH_EXIT = "relative_strength_exit"
    BASE_FAILURE_EXIT = "base_failure_exit"
    TIME_STOP = "time_stop"
    DIRECT_REVERSAL = "direct_reversal"


class FillPolicy(StrEnum):
    """Declared substitute for TradingView's unavailable intrabar path."""

    LOWER_TIMEFRAME_MAGNIFIER = "lower_timeframe_magnifier"
    OHLC_STOP_FIRST = "ohlc_stop_first"
    OHLC_TARGET_FIRST = "ohlc_target_first"


class SizingRejection(StrEnum):
    INVALID_EQUITY = "invalid_equity"
    INVALID_ENTRY_PRICE = "invalid_entry_price"
    INVALID_STOP_DISTANCE = "invalid_stop_distance"
    INVALID_POINT_VALUE = "invalid_point_value"
    INVALID_RISK_INPUT = "invalid_risk_input"
    INVALID_CAP_INPUT = "invalid_cap_input"
    INVALID_QUANTITY_RULE = "invalid_quantity_rule"
    BELOW_MINIMUM = "below_minimum"


class PlanningError(ValueError):
    """A position or fill request is internally inconsistent."""


class UnsupportedMagnifierError(PlanningError):
    """Lower-timeframe coverage cannot support the requested magnifier policy."""


class SleeveReconciliationError(PlanningError):
    """Side-owned sleeve deltas do not reconcile to total account equity."""


@dataclass(frozen=True, slots=True)
class SizingRequest:
    side: PositionSide
    equity: float
    entry_reference_price: float
    stop_price: float
    risk_pct: float = 1.0
    risk_scale: float = 1.0
    risk_multiplier: float = 1.0
    cap_pct: float = 100.0
    point_value: float = 1.0
    quantity_step: float = 1.0
    minimum_quantity: float = 1.0


@dataclass(frozen=True, slots=True)
class QuantityPlan:
    request: SizingRequest
    stop_distance: float | None
    risk_quantity: float | None
    cap_quantity: float | None
    raw_quantity: float | None
    quantity: float
    rejection: SizingRejection | None

    @property
    def accepted(self) -> bool:
        return self.rejection is None and self.quantity > 0.0


@dataclass(frozen=True, slots=True)
class PlannedLeg:
    leg_id: LegId
    quantity: float
    stop_price: float
    target_price: float | None
    target_reason: ExitReason | None


@dataclass(frozen=True, slots=True)
class PositionPlan:
    side: PositionSide
    entry_reference_price: float
    initial_stop_price: float
    risk_distance: float
    requested_quantity: float
    planned_quantity: float
    unallocated_quantity: float
    legs: tuple[PlannedLeg, ...]


def pine_quantity_plan(request: SizingRequest) -> QuantityPlan:
    """Reproduce ``f_plan_qty_mult`` with symmetric fail-closed geometry.

    Pine clamps a negative conviction/risk multiplier to zero, caps quantity by
    position notional, then floors to the configured step.  This implementation
    preserves that arithmetic while rejecting non-finite inputs and non-positive
    equity instead of allowing NaN/negative quantities to escape.
    """

    if not _positive_finite(request.equity):
        return _rejected(request, SizingRejection.INVALID_EQUITY)
    if not _positive_finite(request.entry_reference_price):
        return _rejected(request, SizingRejection.INVALID_ENTRY_PRICE)
    if not _positive_finite(request.point_value):
        return _rejected(request, SizingRejection.INVALID_POINT_VALUE)
    if not _positive_finite(request.quantity_step) or not _positive_finite(
        request.minimum_quantity
    ):
        return _rejected(request, SizingRejection.INVALID_QUANTITY_RULE)
    if not all(
        math.isfinite(value) and value >= 0.0 for value in (request.risk_pct, request.risk_scale)
    ) or not math.isfinite(request.risk_multiplier):
        return _rejected(request, SizingRejection.INVALID_RISK_INPUT)
    if not math.isfinite(request.cap_pct) or request.cap_pct < 0.0:
        return _rejected(request, SizingRejection.INVALID_CAP_INPUT)
    if not _positive_finite(request.stop_price):
        return _rejected(request, SizingRejection.INVALID_STOP_DISTANCE)

    distance = (
        request.entry_reference_price - request.stop_price
        if request.side is PositionSide.LONG
        else request.stop_price - request.entry_reference_price
    )
    if not _positive_finite(distance):
        return _rejected(request, SizingRejection.INVALID_STOP_DISTANCE)

    risk_fraction = (
        request.risk_pct / 100.0 * request.risk_scale * max(request.risk_multiplier, 0.0)
    )
    unit_risk = distance * request.point_value
    unit_notional = request.entry_reference_price * request.point_value
    risk_quantity = request.equity * risk_fraction / unit_risk
    cap_quantity = request.equity * request.cap_pct / 100.0 / unit_notional
    raw_quantity = min(risk_quantity, cap_quantity)
    quantity = _floor_to_step(raw_quantity, request.quantity_step)
    if quantity < request.minimum_quantity:
        return QuantityPlan(
            request=request,
            stop_distance=distance,
            risk_quantity=risk_quantity,
            cap_quantity=cap_quantity,
            raw_quantity=raw_quantity,
            quantity=0.0,
            rejection=SizingRejection.BELOW_MINIMUM,
        )
    return QuantityPlan(
        request=request,
        stop_distance=distance,
        risk_quantity=risk_quantity,
        cap_quantity=cap_quantity,
        raw_quantity=raw_quantity,
        quantity=quantity,
        rejection=None,
    )


def build_position_plan(
    quantity_plan: QuantityPlan,
    *,
    target_1_r: float = 1.0,
    target_2_r: float = 2.0,
) -> PositionPlan:
    """Build Pine's one-leg or split-leg position plan.

    A position below ``3 * minimum_quantity`` is a single leg whose sole target
    is target 2.  A splittable position uses Pine's floor/third/remainder rules;
    any sub-minimum third leg is merged into leg 2.
    """

    if not quantity_plan.accepted or quantity_plan.stop_distance is None:
        raise PlanningError("cannot build legs from a rejected quantity plan")
    if not all(math.isfinite(value) and value >= 0.0 for value in (target_1_r, target_2_r)):
        raise PlanningError("target R multiples must be finite and non-negative")

    request = quantity_plan.request
    quantity = quantity_plan.quantity
    step = request.quantity_step
    minimum = request.minimum_quantity
    can_split = quantity >= minimum * 3.0
    if can_split:
        first = _floor_to_step(quantity / 3.0, step)
        if first < minimum:
            raise PlanningError(
                "Pine split geometry would orphan target legs below minimum quantity"
            )
        second = first
        third = _floor_to_step(quantity - first - second, step)
        if third < minimum:
            second = _floor_to_step(quantity - first, step)
            third = 0.0
        quantities = (first, second, third)
    else:
        quantities = (quantity, 0.0, 0.0)

    sign = 1.0 if request.side is PositionSide.LONG else -1.0
    effective_target_2_r = max(target_1_r, target_2_r)
    targets = (
        (
            request.entry_reference_price
            + sign
            * (target_1_r if can_split else effective_target_2_r)
            * quantity_plan.stop_distance
        ),
        request.entry_reference_price + sign * effective_target_2_r * quantity_plan.stop_distance,
        None,
    )
    reasons: tuple[ExitReason | None, ...] = (
        ExitReason.TARGET_1 if can_split else ExitReason.TARGET_2,
        ExitReason.TARGET_2,
        None,
    )
    legs = tuple(
        PlannedLeg(
            leg_id=leg_id,
            quantity=leg_quantity,
            stop_price=request.stop_price,
            target_price=targets[index],
            target_reason=reasons[index],
        )
        for index, (leg_id, leg_quantity) in enumerate(zip(LegId, quantities, strict=True))
        if leg_quantity >= minimum
    )
    planned = math.fsum(leg.quantity for leg in legs)
    return PositionPlan(
        side=request.side,
        entry_reference_price=request.entry_reference_price,
        initial_stop_price=request.stop_price,
        risk_distance=quantity_plan.stop_distance,
        requested_quantity=quantity,
        planned_quantity=planned,
        unallocated_quantity=max(0.0, quantity - planned),
        legs=legs,
    )


@dataclass(frozen=True, slots=True)
class OhlcBar:
    sequence_id: str
    timestamp_utc: datetime
    open: float
    high: float
    low: float
    close: float
    start_timestamp_utc: datetime | None = None
    symbol: str | None = None
    source: str | None = None
    interval: str | None = None

    def __post_init__(self) -> None:
        if not self.sequence_id:
            raise PlanningError("bar sequence_id is required")
        for name, value in (
            ("symbol", self.symbol),
            ("source", self.source),
            ("interval", self.interval),
        ):
            if value is not None and not value.strip():
                raise PlanningError(f"bar {name} identity cannot be blank")
        if self.timestamp_utc.tzinfo is None or self.timestamp_utc.utcoffset() is None:
            raise PlanningError("bar timestamp must be timezone-aware")
        object.__setattr__(self, "timestamp_utc", self.timestamp_utc.astimezone(UTC))
        if self.start_timestamp_utc is not None:
            if (
                self.start_timestamp_utc.tzinfo is None
                or self.start_timestamp_utc.utcoffset() is None
            ):
                raise PlanningError("bar start timestamp must be timezone-aware")
            start = self.start_timestamp_utc.astimezone(UTC)
            if start >= self.timestamp_utc:
                raise PlanningError("bar start must precede bar close")
            object.__setattr__(self, "start_timestamp_utc", start)
        if not all(
            _positive_finite(value) for value in (self.open, self.high, self.low, self.close)
        ):
            raise PlanningError("OHLC values must be finite and positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise PlanningError("OHLC range does not contain open and close")
        if self.low > self.high:
            raise PlanningError("OHLC low exceeds high")


@dataclass(frozen=True, slots=True)
class ExitOrder:
    position_id: str
    side: PositionSide
    leg_id: LegId
    quantity: float
    stop_price: float
    target_price: float | None = None
    stop_reason: ExitReason = ExitReason.INITIAL_STOP
    target_reason: ExitReason | None = None

    def __post_init__(self) -> None:
        if not self.position_id:
            raise PlanningError("position_id is required")
        if not _positive_finite(self.quantity):
            raise PlanningError("exit quantity must be finite and positive")
        if not _positive_finite(self.stop_price):
            raise PlanningError("stop price must be finite and positive")
        if self.target_price is not None and not _positive_finite(self.target_price):
            raise PlanningError("target price must be finite and positive")
        if (self.target_price is None) != (self.target_reason is None):
            raise PlanningError("target price and target reason must be supplied together")
        if self.target_reason not in (None, ExitReason.TARGET_1, ExitReason.TARGET_2):
            raise PlanningError("target exits require an explicit target reason")
        if self.stop_reason in (ExitReason.TARGET_1, ExitReason.TARGET_2):
            raise PlanningError("a protective stop cannot carry a target reason")
        if self.target_price is not None:
            if self.side is PositionSide.LONG and self.target_price <= self.stop_price:
                raise PlanningError("long target must be above its stop")
            if self.side is PositionSide.SHORT and self.target_price >= self.stop_price:
                raise PlanningError("short target must be below its stop")


@dataclass(frozen=True, slots=True)
class FillEvent:
    fill_id: str
    position_id: str
    owner_side: PositionSide
    leg_id: LegId
    quantity: float
    price: float
    reason: ExitReason
    timestamp_utc: datetime
    source_bar_id: str
    policy: FillPolicy
    gap_through: bool
    oco_cancelled_reason: ExitReason | None

    def __post_init__(self) -> None:
        if not self.fill_id or not self.position_id or not self.source_bar_id:
            raise PlanningError("fill, position, and source-bar ids are required")
        if not _positive_finite(self.quantity) or not _positive_finite(self.price):
            raise PlanningError("fill quantity and price must be finite and positive")
        if self.timestamp_utc.tzinfo is None or self.timestamp_utc.utcoffset() is None:
            raise PlanningError("fill timestamp must be timezone-aware")
        object.__setattr__(self, "timestamp_utc", self.timestamp_utc.astimezone(UTC))
        if self.reason is ExitReason.TARGET_1 and self.leg_id is not LegId.LEG_1:
            raise PlanningError("TARGET_1 can only attribute leg 1")
        if self.reason is ExitReason.TARGET_2 and self.leg_id not in (
            LegId.LEG_1,
            LegId.LEG_2,
        ):
            raise PlanningError("TARGET_2 can only attribute the single leg or leg 2")


def resolve_exit_fill(
    order: ExitOrder,
    bar: OhlcBar,
    *,
    policy: FillPolicy,
    lower_timeframe_bars: Sequence[OhlcBar] | None = None,
) -> FillEvent | None:
    """Resolve one leg's stop/target OCO under a declared approximation.

    Stops are treated as stop-market orders: an adverse gap fills at the open.
    Targets are limits: a favorable gap receives the better opening price.  If
    both prices trade inside one OHLC bar, the selected stop-first/target-first
    rule is the only source of priority.  Exactly one event is returned, so the
    sibling OCO order is cancelled atomically.
    """

    if policy is FillPolicy.LOWER_TIMEFRAME_MAGNIFIER:
        subbars = tuple(lower_timeframe_bars or ())
        _validate_magnifier_coverage(bar, subbars)
        for subbar in subbars:
            event = _resolve_ohlc(order, subbar, FillPolicy.OHLC_STOP_FIRST)
            if event is not None:
                return replace(event, policy=policy)
        return None
    if lower_timeframe_bars is not None:
        raise PlanningError("lower_timeframe_bars are only valid for magnifier policy")
    return _resolve_ohlc(order, bar, policy)


def _resolve_ohlc(order: ExitOrder, bar: OhlcBar, policy: FillPolicy) -> FillEvent | None:
    if policy not in (FillPolicy.OHLC_STOP_FIRST, FillPolicy.OHLC_TARGET_FIRST):
        raise PlanningError(f"unsupported OHLC policy: {policy}")

    if order.side is PositionSide.LONG:
        stop_gap = bar.open <= order.stop_price
        target_gap = order.target_price is not None and bar.open >= order.target_price
        stop_hit = bar.low <= order.stop_price
        target_hit = order.target_price is not None and bar.high >= order.target_price
    else:
        stop_gap = bar.open >= order.stop_price
        target_gap = order.target_price is not None and bar.open <= order.target_price
        stop_hit = bar.high >= order.stop_price
        target_hit = order.target_price is not None and bar.low <= order.target_price

    reason: ExitReason
    price: float
    gap_through: bool
    cancelled: ExitReason | None
    if stop_gap:
        reason, price, gap_through = order.stop_reason, bar.open, True
        cancelled = order.target_reason
    elif target_gap and order.target_price is not None and order.target_reason is not None:
        reason, price, gap_through = order.target_reason, bar.open, True
        cancelled = order.stop_reason
    elif stop_hit and target_hit:
        choose_stop = policy is FillPolicy.OHLC_STOP_FIRST
        reason = order.stop_reason if choose_stop else _required_target_reason(order)
        price = order.stop_price if choose_stop else _required_target_price(order)
        gap_through = False
        cancelled = order.target_reason if choose_stop else order.stop_reason
    elif stop_hit:
        reason, price, gap_through = order.stop_reason, order.stop_price, False
        cancelled = order.target_reason
    elif target_hit:
        reason = _required_target_reason(order)
        price = _required_target_price(order)
        gap_through = False
        cancelled = order.stop_reason
    else:
        return None

    fill_id = f"{order.position_id}:{order.leg_id}:{bar.sequence_id}:{reason}"
    return FillEvent(
        fill_id=fill_id,
        position_id=order.position_id,
        owner_side=order.side,
        leg_id=order.leg_id,
        quantity=order.quantity,
        price=price,
        reason=reason,
        timestamp_utc=bar.timestamp_utc,
        source_bar_id=bar.sequence_id,
        policy=policy,
        gap_through=gap_through,
        oco_cancelled_reason=cancelled,
    )


@dataclass(frozen=True, slots=True)
class PositionMilestones:
    position_id: str
    plan: PositionPlan
    closed_legs: frozenset[LegId] = frozenset()
    target_1_filled: bool = False
    target_2_filled: bool = False
    break_even_armed: bool = False
    fills: tuple[FillEvent, ...] = ()
    break_even_after_target_1: bool = True

    @property
    def side(self) -> PositionSide:
        return self.plan.side

    def __post_init__(self) -> None:
        if not self.position_id:
            raise PlanningError("milestone position_id is required")
        if not isinstance(self.break_even_after_target_1, bool):
            raise PlanningError("break_even_after_target_1 must be boolean")
        _validate_milestone_plan(self.plan)
        break_even_available = False
        for fill in self.fills:
            _validate_fill_against_plan(self.position_id, self.plan, fill)
            if fill.reason is ExitReason.BREAKEVEN_STOP and not break_even_available:
                raise PlanningError("breakeven fill requires the plan's actual TARGET_1 fill first")
            if fill.reason is ExitReason.TARGET_1 and self.break_even_after_target_1:
                break_even_available = True
        fill_ids = tuple(fill.fill_id for fill in self.fills)
        if len(fill_ids) != len(set(fill_ids)):
            raise PlanningError("milestone fills contain a duplicate fill id")
        filled_legs = tuple(fill.leg_id for fill in self.fills)
        if len(filled_legs) != len(set(filled_legs)):
            raise PlanningError("milestone fills contain a duplicate closing leg")

        derived_closed_legs = frozenset(filled_legs)
        derived_target_1 = any(fill.reason is ExitReason.TARGET_1 for fill in self.fills)
        derived_target_2 = any(fill.reason is ExitReason.TARGET_2 for fill in self.fills)
        derived_break_even = self.break_even_after_target_1 and derived_target_1
        if self.closed_legs != derived_closed_legs:
            raise PlanningError("closed_legs must be derived from explicit fill events")
        if self.target_1_filled is not derived_target_1:
            raise PlanningError("target_1_filled must be derived from a TARGET_1 fill event")
        if self.target_2_filled is not derived_target_2:
            raise PlanningError("target_2_filled must be derived from a TARGET_2 fill event")
        if self.break_even_armed is not derived_break_even:
            raise PlanningError("break_even_armed must be derived from target fills and policy")


def _validate_milestone_plan(plan: PositionPlan) -> None:
    legs = {leg.leg_id: leg for leg in plan.legs}
    if not legs or len(legs) != len(plan.legs):
        raise PlanningError("milestone plan requires unique planned legs")
    for leg in plan.legs:
        if (leg.target_price is None) != (leg.target_reason is None):
            raise PlanningError("planned target price and reason must be supplied together")

    leg_ids = frozenset(legs)
    expected_targets: dict[LegId, ExitReason | None]
    if leg_ids == {LegId.LEG_1}:
        expected_targets = {LegId.LEG_1: ExitReason.TARGET_2}
    elif leg_ids in (
        {LegId.LEG_1, LegId.LEG_2},
        {LegId.LEG_1, LegId.LEG_2, LegId.LEG_3},
    ):
        expected_targets = {
            LegId.LEG_1: ExitReason.TARGET_1,
            LegId.LEG_2: ExitReason.TARGET_2,
            LegId.LEG_3: None,
        }
    else:
        raise PlanningError("milestone plan has unsupported leg geometry")
    if any(leg.target_reason is not expected_targets[leg.leg_id] for leg in plan.legs):
        raise PlanningError("milestone plan target attribution is inconsistent")


def _validate_fill_against_plan(
    position_id: str,
    plan: PositionPlan,
    fill: FillEvent,
) -> None:
    if fill.position_id != position_id or fill.owner_side is not plan.side:
        raise PlanningError("milestone fill does not belong to the position plan")
    planned_leg = next((leg for leg in plan.legs if leg.leg_id is fill.leg_id), None)
    if planned_leg is None:
        raise PlanningError("milestone fill references a leg absent from the position plan")
    if not math.isclose(
        fill.quantity,
        planned_leg.quantity,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise PlanningError("milestone fill quantity does not close its entire planned leg")
    target_reasons = (ExitReason.TARGET_1, ExitReason.TARGET_2)
    if fill.reason in target_reasons and fill.reason is not planned_leg.target_reason:
        raise PlanningError("milestone target fill contradicts the position plan")
    if (
        fill.oco_cancelled_reason in target_reasons
        and fill.oco_cancelled_reason is not planned_leg.target_reason
    ):
        raise PlanningError("milestone OCO target attribution contradicts the position plan")


def apply_fill_to_milestones(
    state: PositionMilestones,
    fill: FillEvent,
    *,
    break_even_after_target_1: bool | None = None,
) -> PositionMilestones:
    """Advance milestones from explicit fills, never from open-leg absence."""

    _validate_fill_against_plan(state.position_id, state.plan, fill)
    if fill.leg_id in state.closed_legs:
        raise PlanningError(f"duplicate closing fill for {fill.leg_id}")
    if fill.reason is ExitReason.BREAKEVEN_STOP and not state.break_even_armed:
        raise PlanningError("breakeven fill requires the plan's actual TARGET_1 fill first")

    break_even_policy = (
        state.break_even_after_target_1
        if break_even_after_target_1 is None
        else break_even_after_target_1
    )
    if state.fills and break_even_policy is not state.break_even_after_target_1:
        raise PlanningError("break-even policy cannot change after the first closing fill")
    fills = (*state.fills, fill)
    target_1 = state.target_1_filled or fill.reason is ExitReason.TARGET_1
    return PositionMilestones(
        position_id=state.position_id,
        plan=state.plan,
        closed_legs=state.closed_legs | {fill.leg_id},
        target_1_filled=target_1,
        target_2_filled=state.target_2_filled or fill.reason is ExitReason.TARGET_2,
        break_even_armed=state.break_even_armed
        or (break_even_policy and fill.reason is ExitReason.TARGET_1),
        fills=fills,
        break_even_after_target_1=break_even_policy,
    )


def milestone_stop(
    state: PositionMilestones,
    *,
    current_stop: float,
    entry_price: float,
    risk_distance: float,
    break_even_offset_r: float = 0.0,
) -> float:
    """Tighten, but never loosen, a stop once an actual T1 fill armed BE."""

    if not all(_positive_finite(value) for value in (current_stop, entry_price, risk_distance)):
        raise PlanningError("stop inputs must be finite and positive")
    if not math.isfinite(break_even_offset_r):
        raise PlanningError("break_even_offset_r must be finite")
    if not state.break_even_armed:
        return current_stop
    sign = 1.0 if state.side is PositionSide.LONG else -1.0
    break_even = entry_price + sign * break_even_offset_r * risk_distance
    return (
        max(current_stop, break_even)
        if state.side is PositionSide.LONG
        else min(current_stop, break_even)
    )


@dataclass(frozen=True, slots=True)
class SleeveState:
    total_equity: float
    long_equity: float
    short_equity: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in self.as_tuple()):
            raise SleeveReconciliationError("sleeve equity must be finite")
        if not math.isclose(
            self.long_equity + self.short_equity,
            self.total_equity,
            rel_tol=1e-12,
            abs_tol=1e-9,
        ):
            raise SleeveReconciliationError("long and short sleeves must sum to total equity")

    def as_tuple(self) -> tuple[float, float, float]:
        return self.total_equity, self.long_equity, self.short_equity


@dataclass(frozen=True, slots=True)
class SleeveAttribution:
    owner_side: PositionSide
    equity_delta: float
    label: str
    source_fill_id: str | None = None


def reconcile_sleeves(
    state: SleeveState,
    *,
    new_total_equity: float,
    attributions: Sequence[SleeveAttribution],
    fills: Sequence[FillEvent] = (),
) -> SleeveState:
    """Reconcile account equity by fill-owned side, including direct reversals.

    A reversal can contain a long closing fill and a short opening-cost fill in
    the same account observation.  Attribution therefore follows each event's
    immutable ``owner_side`` rather than the previous or resulting net position.
    """

    if not math.isfinite(new_total_equity):
        raise SleeveReconciliationError("new total equity must be finite")
    fill_by_id = {fill.fill_id: fill for fill in fills}
    if len(fill_by_id) != len(fills):
        raise SleeveReconciliationError("fill ids must be unique")

    if fills and any(attribution.source_fill_id is None for attribution in attributions):
        raise SleeveReconciliationError(
            "fill reconciliation cannot mix unlinked mark-to-market attributions"
        )
    referenced_fill_ids = [
        attribution.source_fill_id
        for attribution in attributions
        if attribution.source_fill_id is not None
    ]
    if len(referenced_fill_ids) != len(set(referenced_fill_ids)):
        raise SleeveReconciliationError("each fill may be attributed only once")
    if fills and set(referenced_fill_ids) != set(fill_by_id):
        raise SleeveReconciliationError("every supplied fill must be attributed exactly once")

    long_delta = 0.0
    short_delta = 0.0
    for attribution in attributions:
        if not math.isfinite(attribution.equity_delta) or not attribution.label:
            raise SleeveReconciliationError("attributions require finite delta and label")
        if attribution.source_fill_id is not None:
            fill = fill_by_id.get(attribution.source_fill_id)
            if fill is None:
                raise SleeveReconciliationError("attribution references an unknown fill")
            if fill.owner_side is not attribution.owner_side:
                raise SleeveReconciliationError("attribution side disagrees with fill ownership")
        if attribution.owner_side is PositionSide.LONG:
            long_delta += attribution.equity_delta
        else:
            short_delta += attribution.equity_delta

    account_delta = new_total_equity - state.total_equity
    attributed_delta = math.fsum((long_delta, short_delta))
    if not math.isclose(account_delta, attributed_delta, rel_tol=1e-12, abs_tol=1e-9):
        raise SleeveReconciliationError(
            f"equity delta {account_delta!r} does not reconcile to {attributed_delta!r}"
        )
    return SleeveState(
        total_equity=new_total_equity,
        long_equity=state.long_equity + long_delta,
        short_equity=state.short_equity + short_delta,
    )


def _rejected(request: SizingRequest, reason: SizingRejection) -> QuantityPlan:
    return QuantityPlan(
        request=request,
        stop_distance=None,
        risk_quantity=None,
        cap_quantity=None,
        raw_quantity=None,
        quantity=0.0,
        rejection=reason,
    )


def _positive_finite(value: float) -> bool:
    return math.isfinite(value) and value > 0.0


def _floor_to_step(quantity: float, step: float) -> float:
    return math.floor(quantity / step) * step


def _required_target_price(order: ExitOrder) -> float:
    if order.target_price is None:
        raise PlanningError("target hit without a target price")
    return order.target_price


def _required_target_reason(order: ExitOrder) -> ExitReason:
    if order.target_reason is None:
        raise PlanningError("target hit without a target reason")
    return order.target_reason


def _validate_magnifier_coverage(parent: OhlcBar, subbars: tuple[OhlcBar, ...]) -> None:
    if not subbars:
        raise UnsupportedMagnifierError("magnifier policy requires lower-timeframe bars")
    if parent.symbol is None or parent.source is None or parent.interval is None:
        raise UnsupportedMagnifierError(
            "magnifier coverage requires explicit parent symbol/source/interval identity"
        )
    if any(bar.symbol is None or bar.source is None or bar.interval is None for bar in subbars):
        raise UnsupportedMagnifierError(
            "magnifier coverage requires explicit sub-bar symbol/source/interval identity"
        )
    if any((bar.symbol, bar.source) != (parent.symbol, parent.source) for bar in subbars):
        raise UnsupportedMagnifierError(
            "lower-timeframe symbol/source identity does not match the parent"
        )
    subbar_intervals = {bar.interval for bar in subbars}
    if len(subbar_intervals) != 1 or parent.interval in subbar_intervals:
        raise UnsupportedMagnifierError(
            "magnifier bars must share one lower interval identity distinct from the parent"
        )
    if parent.start_timestamp_utc is None or any(
        bar.start_timestamp_utc is None for bar in subbars
    ):
        raise UnsupportedMagnifierError(
            "magnifier coverage requires explicit parent and sub-bar intervals"
        )
    if any(
        current.timestamp_utc <= previous.timestamp_utc for previous, current in pairwise(subbars)
    ):
        raise UnsupportedMagnifierError("lower-timeframe bars are not chronological")
    if (
        subbars[0].start_timestamp_utc != parent.start_timestamp_utc
        or subbars[-1].timestamp_utc != parent.timestamp_utc
        or any(
            current.start_timestamp_utc != previous.timestamp_utc
            for previous, current in pairwise(subbars)
        )
    ):
        raise UnsupportedMagnifierError(
            "lower-timeframe bars do not continuously cover the parent interval"
        )
    durations = {
        bar.timestamp_utc - bar.start_timestamp_utc  # type: ignore[operator]
        for bar in subbars
    }
    if (
        len(durations) != 1
        or next(iter(durations)) >= parent.timestamp_utc - parent.start_timestamp_utc
    ):
        raise UnsupportedMagnifierError(
            "magnifier bars must have one consistent resolution below the parent"
        )
    # Complete coverage is evidenced by reproducing all four parent OHLC extrema.
    if not all(
        math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)
        for left, right in (
            (subbars[0].open, parent.open),
            (subbars[-1].close, parent.close),
            (max(bar.high for bar in subbars), parent.high),
            (min(bar.low for bar in subbars), parent.low),
        )
    ):
        raise UnsupportedMagnifierError(
            "lower-timeframe bars do not reproduce the complete parent OHLC"
        )
