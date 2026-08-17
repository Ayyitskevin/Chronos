"""Pure deterministic Five-Tool signal-to-ledger research replay.

This adapter owns its account snapshots and models fills locally.  It is not a
broker emulator, does not import Chronos execution or order types, and must not
be used as evidence of TradingView or live-execution parity.  The signal-time
stop pre-submitted with an entry is eligible on the entry bar for every leg;
targets and the fill-rebased ladder become eligible only on later bars.  A T1
fill can arm break-even and close-time trailing state only for later bars.

Every call starts a fresh engine at ``settings.history_start_utc`` and replays
the complete supplied history.  This adapter has no checkpoint/resume contract;
chunked or suffix-only replay is unsupported.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from enum import StrEnum

from chronos.research.five_tool.alignment import source_bar_id
from chronos.research.five_tool.engine import FiveToolEngine
from chronos.research.five_tool.models import (
    AccountSnapshot,
    FiveToolBarInput,
    FiveToolSettings,
    FiveToolTrace,
    SetupFamily,
    Side,
    SignalEvent,
    SignalIntent,
)
from chronos.research.five_tool.planning import (
    ExitOrder,
    ExitReason,
    FillEvent,
    FillPolicy,
    LegId,
    OhlcBar,
    PlannedLeg,
    PositionMilestones,
    PositionPlan,
    PositionSide,
    QuantityPlan,
    SizingRequest,
    apply_fill_to_milestones,
    build_filled_position_plan,
    milestone_stop,
    pine_quantity_plan,
    resolve_exit_fill,
    validate_magnifier_coverage,
)
from chronos.research.five_tool.validation import (
    ClosedLeg,
    EconomicPosition,
    aggregate_economic_positions,
)


class ReplayInputError(ValueError):
    """Replay inputs or frozen policy cannot produce coherent evidence."""


class IncompleteReplayError(ReplayInputError):
    """The terminal policy refused a pending or partly open position."""


class TerminalPositionPolicy(StrEnum):
    REQUIRE_FLAT = "require_flat"
    EXCLUDE_INCOMPLETE = "exclude_incomplete"


@dataclass(frozen=True, slots=True)
class ReplayBar:
    """One aligned close-time input plus its explicit executable open instant."""

    input: FiveToolBarInput
    open_timestamp_utc: datetime
    lower_timeframe_bars: tuple[OhlcBar, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.input, FiveToolBarInput):
            raise ReplayInputError("replay input must be a FiveToolBarInput")
        if self.open_timestamp_utc.tzinfo is None or self.open_timestamp_utc.utcoffset() is None:
            raise ReplayInputError("bar open timestamp must be timezone-aware")
        opened = self.open_timestamp_utc.astimezone(UTC)
        if opened >= self.input.primary.timestamp_utc:
            raise ReplayInputError("bar open timestamp must precede its close")
        object.__setattr__(self, "open_timestamp_utc", opened)
        if not isinstance(self.lower_timeframe_bars, tuple) or any(
            not isinstance(item, OhlcBar) for item in self.lower_timeframe_bars
        ):
            raise ReplayInputError("lower-timeframe bars must be a tuple of OhlcBar values")


@dataclass(frozen=True, slots=True)
class FiveToolReplayPolicy:
    """Frozen approximation and friction policy for one replay."""

    initial_equity: float = 100_000.0
    parameter_variant: str = "five_tool_default"
    fill_policy: FillPolicy = FillPolicy.OHLC_STOP_FIRST
    commission_bps_per_fill: float = 3.0
    slippage_ticks_per_fill: int = 2
    apply_slippage_to_target_limits: bool = False
    terminal_position_policy: TerminalPositionPolicy = TerminalPositionPolicy.REQUIRE_FLAT

    def __post_init__(self) -> None:
        if (
            isinstance(self.initial_equity, bool)
            or not math.isfinite(self.initial_equity)
            or self.initial_equity <= 0.0
        ):
            raise ReplayInputError("initial equity must be finite and positive")
        if not isinstance(self.parameter_variant, str) or not self.parameter_variant.strip():
            raise ReplayInputError("parameter variant is required")
        if (
            isinstance(self.commission_bps_per_fill, bool)
            or not math.isfinite(self.commission_bps_per_fill)
            or self.commission_bps_per_fill < 0.0
        ):
            raise ReplayInputError("commission bps must be finite and non-negative")
        if (
            isinstance(self.slippage_ticks_per_fill, bool)
            or not isinstance(self.slippage_ticks_per_fill, int)
            or self.slippage_ticks_per_fill < 0
        ):
            raise ReplayInputError("slippage ticks must be a non-negative integer")
        if not isinstance(self.apply_slippage_to_target_limits, bool):
            raise ReplayInputError("target-limit slippage policy must be boolean")
        if not isinstance(self.fill_policy, FillPolicy):
            raise ReplayInputError("fill policy must be a FillPolicy")
        if not isinstance(self.terminal_position_policy, TerminalPositionPolicy):
            raise ReplayInputError("terminal position policy must be a TerminalPositionPolicy")

    @property
    def canonical_payload(self) -> dict[str, object]:
        """Return the complete, manifest-bindable execution approximation."""

        return {
            "schema_version": "chronos-five-tool-replay-policy-v1",
            "initial_equity": self.initial_equity,
            "parameter_variant": self.parameter_variant,
            "fill_policy": self.fill_policy.value,
            "commission_bps_per_fill": self.commission_bps_per_fill,
            "slippage_ticks_per_fill": self.slippage_ticks_per_fill,
            "apply_slippage_to_target_limits": self.apply_slippage_to_target_limits,
            "terminal_position_policy": self.terminal_position_policy.value,
            "signal_clock": "confirmed_primary_bar_close",
            "market_order_eligibility": "next_primary_bar_open",
            "entry_bar_stop": (
                "signal_time_absolute_stop_active_for_every_leg_with_stop_market_gap_semantics"
            ),
            "entry_bar_targets": "inactive",
            "post_entry_ladder": (
                "signal_time_quantity_and_risk_distance_frozen_then_stop_and_targets_rebased_"
                "to_adverse_actual_next_open_execution_for_later_bars"
            ),
            "discretionary_priority": (
                "queued_close_time_discretionary_exit_executes_at_next_open_before_protective_"
                "resolution"
            ),
            "protective_priority": (
                "on_later_bars_protective_resolution_precedes_close_time_signal_evaluation"
            ),
            "replay_scope": "full_from_settings_history_start_only",
            "checkpoint_resume": "unsupported",
            "execution_parity": "UNVERIFIED",
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self.canonical_payload)


_DEFAULT_REPLAY_POLICY = FiveToolReplayPolicy()


@dataclass(frozen=True, slots=True)
class EntryFillEvent:
    fill_id: str
    position_id: str
    owner_side: PositionSide
    leg_id: LegId
    quantity: float
    signal_timestamp_utc: datetime
    timestamp_utc: datetime
    source_bar_id: str
    raw_price: float
    execution_price: float
    commission_cost: float
    slippage_cost: float


@dataclass(frozen=True, slots=True)
class ExitFillReceipt:
    fill: FillEvent
    execution_price: float
    commission_cost: float
    slippage_cost: float


@dataclass(frozen=True, slots=True)
class PositionReplayPlan:
    position_id: str
    signal_timestamp_utc: datetime
    entry_timestamp_utc: datetime
    signal_quantity_plan: QuantityPlan
    filled_position_plan: PositionPlan


@dataclass(frozen=True, slots=True)
class SizingRejectionEvidence:
    signal_event_id: str
    signal_timestamp_utc: datetime
    side: PositionSide
    quantity_plan: QuantityPlan


@dataclass(frozen=True, slots=True)
class ReplayEquityPoint:
    bar_index: int
    timestamp_utc: datetime
    total_equity: float
    realized_net_pnl: float
    unrealized_net_pnl: float
    position: Side
    open_quantity: float
    long_virtual_equity: float | None
    short_virtual_equity: float | None


@dataclass(frozen=True, slots=True)
class FiveToolReplayResult:
    settings_digest: str
    replay_input_digest: str
    policy_digest: str
    policy: FiveToolReplayPolicy
    traces: tuple[FiveToolTrace, ...]
    account_snapshots: tuple[AccountSnapshot, ...]
    position_plans: tuple[PositionReplayPlan, ...]
    entry_fills: tuple[EntryFillEvent, ...]
    exit_fills: tuple[ExitFillReceipt, ...]
    closed_legs: tuple[ClosedLeg, ...]
    validation_closed_legs: tuple[ClosedLeg, ...]
    economic_positions: tuple[EconomicPosition, ...]
    sizing_rejections: tuple[SizingRejectionEvidence, ...]
    equity_curve: tuple[ReplayEquityPoint, ...]
    ending_equity: float
    excluded_incomplete_position_ids: tuple[str, ...]
    terminal_open_leg_ids: tuple[LegId, ...]
    terminal_pending_entry_position_id: str | None
    terminal_pending_exit_position_id: str | None
    accounting_basis: str = (
        "raw-OHLC gross P&L minus explicit entry/exit commission and adverse slippage; "
        "turnover uses execution notional"
    )
    execution_parity: str = "UNVERIFIED"

    @property
    def digest(self) -> str:
        payload = json.dumps(
            asdict(self),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_json_default,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _PendingEntry:
    position_id: str
    signal_event: SignalEvent
    side: PositionSide
    setup: SetupFamily
    base_pivot_at_entry: float | None
    regime: str
    quantity_plan: QuantityPlan


@dataclass(frozen=True, slots=True)
class _PendingExit:
    position_id: str
    side: PositionSide
    reason: ExitReason
    signal_timestamp_utc: datetime


@dataclass(slots=True)
class _ActivePosition:
    position_id: str
    instrument: str
    side: PositionSide
    setup: SetupFamily
    base_pivot_at_entry: float | None
    regime: str
    entry_bar_index: int
    entry_time_utc: datetime
    raw_entry_price: float
    execution_entry_price: float
    plan: PositionPlan
    milestones: PositionMilestones
    entry_fills: tuple[EntryFillEvent, ...]
    leg_stop: float
    leg_stop_reason: ExitReason
    runner_stop: float
    runner_stop_reason: ExitReason

    @property
    def open_legs(self) -> tuple[PlannedLeg, ...]:
        return tuple(leg for leg in self.plan.legs if leg.leg_id not in self.milestones.closed_legs)


def replay_five_tool(
    settings: FiveToolSettings,
    bars: Sequence[ReplayBar],
    *,
    policy: FiveToolReplayPolicy = _DEFAULT_REPLAY_POLICY,
) -> FiveToolReplayResult:
    """Drive signal traces, next-open fills, account feedback, and validation ledgers.

    Causal order is fixed: a queued discretionary exit executes at a bar's
    explicit open; a queued entry and its pre-submitted signal-time stop become
    eligible next; later-bar protective orders resolve before the close; then
    the engine receives an adapter-owned account snapshot and may queue an
    action for the following open. Entry-bar targets are inactive.

    The call must contain the complete prefix beginning exactly at
    ``settings.history_start_utc``. A fresh engine and replay ledger are created
    on every call; replay checkpoint/resume is intentionally unsupported.
    """

    if not isinstance(policy, FiveToolReplayPolicy):
        raise ReplayInputError("policy must be a FiveToolReplayPolicy")
    replay_bars = tuple(bars)
    if not replay_bars:
        raise ReplayInputError("replay requires a non-empty full history prefix")
    _validate_replay_bars(replay_bars, policy)
    if replay_bars[0].input.primary.timestamp_utc != settings.history_start_utc:
        raise ReplayInputError(
            "replay requires the full history prefix beginning exactly at "
            "settings.history_start_utc; checkpoint/resume and suffix replay are unsupported"
        )
    replay_input_digest = _replay_input_digest(replay_bars)
    engine = FiveToolEngine(settings)
    traces: list[FiveToolTrace] = []
    account_snapshots: list[AccountSnapshot] = []
    position_plans: list[PositionReplayPlan] = []
    entry_fills: list[EntryFillEvent] = []
    exit_fills: list[ExitFillReceipt] = []
    closed_legs: list[ClosedLeg] = []
    rejections: list[SizingRejectionEvidence] = []
    equity_curve: list[ReplayEquityPoint] = []
    active: _ActivePosition | None = None
    pending_entry: _PendingEntry | None = None
    pending_exit: _PendingExit | None = None
    long_virtual, short_virtual = _initial_sleeves(settings, policy.initial_equity)
    previous_equity = policy.initial_equity
    primary_history: list[OhlcBar] = []

    for index, replay_bar in enumerate(replay_bars):
        primary = replay_bar.input.primary
        ohlc = _primary_ohlc(replay_bar)
        primary_history.append(ohlc)
        owner_for_bar = active.side if active is not None else None
        entry_bar_signal_stop: float | None = None

        if pending_exit is not None:
            if active is None or active.position_id != pending_exit.position_id:
                raise ReplayInputError("queued discretionary exit has no matching position")
            owner_for_bar = active.side
            for leg in active.open_legs:
                fill = FillEvent(
                    fill_id=(
                        f"{active.position_id}:{leg.leg_id}:{ohlc.sequence_id}:"
                        f"{pending_exit.reason}"
                    ),
                    position_id=active.position_id,
                    owner_side=active.side,
                    leg_id=leg.leg_id,
                    quantity=leg.quantity,
                    price=primary.open,
                    reason=pending_exit.reason,
                    timestamp_utc=replay_bar.open_timestamp_utc,
                    source_bar_id=ohlc.sequence_id,
                    policy=policy.fill_policy,
                    gap_through=False,
                    oco_cancelled_reason=leg.target_reason,
                )
                active = _close_leg(
                    active,
                    fill,
                    settings=settings,
                    policy=policy,
                    exit_fills=exit_fills,
                    closed_legs=closed_legs,
                )
                if active is None:
                    break
            pending_exit = None

        if pending_entry is not None:
            if active is not None:
                raise ReplayInputError("queued entry would overlap an active position")
            raw_entry = primary.open
            entry_bar_signal_stop = pending_entry.quantity_plan.request.stop_price
            execution_entry = _adverse_price(
                raw_entry,
                pending_entry.side,
                settings=settings,
                policy=policy,
                opening=True,
                target_limit=False,
            )
            plan = build_filled_position_plan(
                pending_entry.quantity_plan,
                entry_fill_price=execution_entry,
                target_1_r=settings.number("t1_r"),
                target_2_r=settings.number("t2_r"),
            )
            new_entry_fills = tuple(
                _entry_fill(
                    pending_entry,
                    leg,
                    raw_entry=raw_entry,
                    execution_entry=execution_entry,
                    entry_timestamp_utc=replay_bar.open_timestamp_utc,
                    source_id=ohlc.sequence_id,
                    settings=settings,
                    policy=policy,
                )
                for leg in plan.legs
            )
            entry_fills.extend(new_entry_fills)
            milestones = PositionMilestones(
                pending_entry.position_id,
                plan,
                break_even_after_target_1=settings.boolean("be_after_t1"),
            )
            active = _ActivePosition(
                position_id=pending_entry.position_id,
                instrument=primary.symbol,
                side=pending_entry.side,
                setup=pending_entry.setup,
                base_pivot_at_entry=pending_entry.base_pivot_at_entry,
                regime=pending_entry.regime,
                entry_bar_index=index,
                entry_time_utc=replay_bar.open_timestamp_utc,
                raw_entry_price=raw_entry,
                execution_entry_price=execution_entry,
                plan=plan,
                milestones=milestones,
                entry_fills=new_entry_fills,
                leg_stop=plan.initial_stop_price,
                leg_stop_reason=ExitReason.INITIAL_STOP,
                runner_stop=plan.initial_stop_price,
                runner_stop_reason=ExitReason.INITIAL_STOP,
            )
            position_plans.append(
                PositionReplayPlan(
                    position_id=pending_entry.position_id,
                    signal_timestamp_utc=pending_entry.signal_event.timestamp_utc,
                    entry_timestamp_utc=replay_bar.open_timestamp_utc,
                    signal_quantity_plan=pending_entry.quantity_plan,
                    filled_position_plan=plan,
                )
            )
            owner_for_bar = active.side
            pending_entry = None

        if active is not None:
            owner_for_bar = active.side
            if active.entry_bar_index == index:
                if entry_bar_signal_stop is None:
                    raise ReplayInputError("entry-bar position lost its signal-time stop")
                fills = _entry_bar_stop_fills(
                    active,
                    ohlc,
                    signal_stop_price=entry_bar_signal_stop,
                    lower_timeframe_bars=replay_bar.lower_timeframe_bars,
                    policy=policy,
                )
            else:
                fills = _protective_fills(
                    active,
                    ohlc,
                    lower_timeframe_bars=replay_bar.lower_timeframe_bars,
                    policy=policy,
                )
            for fill in fills:
                active = _close_leg(
                    active,
                    fill,
                    settings=settings,
                    policy=policy,
                    exit_fills=exit_fills,
                    closed_legs=closed_legs,
                )
                if active is None:
                    break

        realized = math.fsum(leg.net_pnl for leg in closed_legs)
        unrealized = _unrealized_net(active, mark_price=primary.close, settings=settings)
        total_equity = math.fsum((policy.initial_equity, realized, unrealized))
        long_virtual, short_virtual = _attribute_sleeve_delta(
            settings,
            previous_total=previous_equity,
            new_total=total_equity,
            owner_side=owner_for_bar,
            long_virtual=long_virtual,
            short_virtual=short_virtual,
        )
        previous_equity = total_equity
        account = _account_snapshot(
            active,
            total_equity=total_equity,
            long_virtual=long_virtual,
            short_virtual=short_virtual,
        )
        owned_input = replace(replay_bar.input, account=account)
        account_snapshots.append(account)
        trace = engine.step(owned_input)
        traces.append(trace)
        equity_curve.append(
            ReplayEquityPoint(
                bar_index=index,
                timestamp_utc=primary.timestamp_utc,
                total_equity=total_equity,
                realized_net_pnl=realized,
                unrealized_net_pnl=unrealized,
                position=account.position,
                open_quantity=(
                    math.fsum(leg.quantity for leg in active.open_legs)
                    if active is not None
                    else 0.0
                ),
                long_virtual_equity=long_virtual,
                short_virtual_equity=short_virtual,
            )
        )

        if active is not None:
            _update_stops(active, trace, primary_history, settings=settings)

        if settings.boolean("enable_orders"):
            if trace.intent in (SignalIntent.ENTER_LONG, SignalIntent.ENTER_SHORT):
                if pending_entry is not None or active is not None:
                    raise ReplayInputError("entry intent was emitted while replay was not flat")
                candidate = _plan_entry(
                    trace,
                    owned_input,
                    engine=engine,
                    settings=settings,
                    policy=policy,
                    long_virtual=long_virtual,
                    short_virtual=short_virtual,
                )
                if isinstance(candidate, SizingRejectionEvidence):
                    rejections.append(candidate)
                else:
                    pending_entry = candidate
            elif trace.intent in (SignalIntent.EXIT_LONG, SignalIntent.EXIT_SHORT):
                if active is None:
                    raise ReplayInputError("exit intent was emitted without an active position")
                if pending_exit is not None:
                    raise ReplayInputError("multiple discretionary exits were queued")
                pending_exit = _PendingExit(
                    position_id=active.position_id,
                    side=active.side,
                    reason=_exit_reason(trace),
                    signal_timestamp_utc=trace.timestamp_utc,
                )

    incomplete_ids = tuple(
        sorted(
            {
                *([active.position_id] if active is not None else []),
                *([pending_entry.position_id] if pending_entry is not None else []),
                *([pending_exit.position_id] if pending_exit is not None else []),
            }
        )
    )
    if incomplete_ids and policy.terminal_position_policy is TerminalPositionPolicy.REQUIRE_FLAT:
        raise IncompleteReplayError(
            "replay ended with incomplete position evidence: " + ", ".join(incomplete_ids)
        )

    validation_legs = tuple(
        leg for leg in closed_legs if leg.position_id not in set(incomplete_ids)
    )
    economic_positions = aggregate_economic_positions(validation_legs)
    ending_equity = equity_curve[-1].total_equity if equity_curve else policy.initial_equity
    return FiveToolReplayResult(
        settings_digest=settings.digest,
        replay_input_digest=replay_input_digest,
        policy_digest=policy.digest,
        policy=policy,
        traces=tuple(traces),
        account_snapshots=tuple(account_snapshots),
        position_plans=tuple(position_plans),
        entry_fills=tuple(entry_fills),
        exit_fills=tuple(exit_fills),
        closed_legs=tuple(closed_legs),
        validation_closed_legs=validation_legs,
        economic_positions=economic_positions,
        sizing_rejections=tuple(rejections),
        equity_curve=tuple(equity_curve),
        ending_equity=ending_equity,
        excluded_incomplete_position_ids=incomplete_ids,
        terminal_open_leg_ids=(
            tuple(leg.leg_id for leg in active.open_legs) if active is not None else ()
        ),
        terminal_pending_entry_position_id=(
            pending_entry.position_id if pending_entry is not None else None
        ),
        terminal_pending_exit_position_id=(
            pending_exit.position_id if pending_exit is not None else None
        ),
    )


def _validate_replay_bars(bars: tuple[ReplayBar, ...], policy: FiveToolReplayPolicy) -> None:
    previous: ReplayBar | None = None
    seen_sequence_ids: set[str] = set()
    for item in bars:
        if not isinstance(item, ReplayBar):
            raise ReplayInputError("replay bars must contain only ReplayBar values")
        if previous is not None:
            if item.input.primary.timestamp_utc <= previous.input.primary.timestamp_utc:
                raise ReplayInputError("replay bar closes must be strictly increasing")
            if item.open_timestamp_utc < previous.input.primary.timestamp_utc:
                raise ReplayInputError("a replay bar opens before the prior bar closes")
        if policy.fill_policy is FillPolicy.LOWER_TIMEFRAME_MAGNIFIER:
            validate_magnifier_coverage(
                _primary_ohlc(item),
                item.lower_timeframe_bars,
            )
        elif item.lower_timeframe_bars:
            raise ReplayInputError(
                "lower-timeframe bars require the lower-timeframe magnifier policy"
            )
        evidence_ids = (
            source_bar_id(item.input.primary),
            *(bar.sequence_id for bar in item.lower_timeframe_bars),
        )
        if len(set(evidence_ids)) != len(evidence_ids) or seen_sequence_ids.intersection(
            evidence_ids
        ):
            raise ReplayInputError("replay evidence sequence identities must be globally unique")
        seen_sequence_ids.update(evidence_ids)
        previous = item


def _primary_ohlc(item: ReplayBar) -> OhlcBar:
    bar = item.input.primary
    return OhlcBar(
        sequence_id=source_bar_id(bar),
        timestamp_utc=bar.timestamp_utc,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        start_timestamp_utc=item.open_timestamp_utc,
        symbol=bar.symbol,
        source=bar.source,
        interval=bar.interval.value,
    )


def _replay_input_digest(bars: tuple[ReplayBar, ...]) -> str:
    """Bind all effective replay facts while excluding overwritten account claims."""

    payload: list[dict[str, object]] = []
    for item in bars:
        effective_input = asdict(item.input)
        effective_input.pop("account")
        payload.append(
            {
                "effective_input": effective_input,
                "primary_sequence_id": source_bar_id(item.input.primary),
                "open_timestamp_utc": item.open_timestamp_utc,
                "lower_timeframe_bars": tuple(
                    asdict(lower_bar) for lower_bar in item.lower_timeframe_bars
                ),
                "caller_account_snapshot": "excluded_because_replay_overwrites_it",
            }
        )
    return _canonical_digest(payload)


def _initial_sleeves(
    settings: FiveToolSettings, initial_equity: float
) -> tuple[float | None, float | None]:
    if not settings.boolean("use_blended_capital_split"):
        return None, None
    long_virtual = initial_equity * settings.number("long_capital_alloc_pct") / 100.0
    return long_virtual, initial_equity - long_virtual


def _attribute_sleeve_delta(
    settings: FiveToolSettings,
    *,
    previous_total: float,
    new_total: float,
    owner_side: PositionSide | None,
    long_virtual: float | None,
    short_virtual: float | None,
) -> tuple[float | None, float | None]:
    if not settings.boolean("use_blended_capital_split"):
        return None, None
    if long_virtual is None or short_virtual is None:
        raise ReplayInputError("blended replay lost its side-owned equity state")
    delta = new_total - previous_total
    if not math.isclose(delta, 0.0, rel_tol=0.0, abs_tol=1e-12):
        if owner_side is None:
            raise ReplayInputError("account equity changed without a side owner")
        if owner_side is PositionSide.LONG:
            long_virtual += delta
        else:
            short_virtual += delta
    if not math.isclose(
        long_virtual + short_virtual,
        new_total,
        rel_tol=1e-12,
        abs_tol=1e-8,
    ):
        raise ReplayInputError("side-owned equity does not reconcile to account equity")
    return long_virtual, short_virtual


def _account_snapshot(
    active: _ActivePosition | None,
    *,
    total_equity: float,
    long_virtual: float | None,
    short_virtual: float | None,
) -> AccountSnapshot:
    if active is None:
        return AccountSnapshot(
            equity=total_equity,
            long_virtual_equity=long_virtual,
            short_virtual_equity=short_virtual,
        )
    return AccountSnapshot(
        equity=total_equity,
        position=Side.LONG if active.side is PositionSide.LONG else Side.SHORT,
        average_entry_price=active.execution_entry_price,
        entry_bar_index=active.entry_bar_index,
        entry_setup=active.setup,
        base_pivot_at_entry=active.base_pivot_at_entry,
        long_virtual_equity=long_virtual,
        short_virtual_equity=short_virtual,
    )


def _plan_entry(
    trace: FiveToolTrace,
    item: FiveToolBarInput,
    *,
    engine: FiveToolEngine,
    settings: FiveToolSettings,
    policy: FiveToolReplayPolicy,
    long_virtual: float | None,
    short_virtual: float | None,
) -> _PendingEntry | SizingRejectionEvidence:
    side = PositionSide.LONG if trace.intent is SignalIntent.ENTER_LONG else PositionSide.SHORT
    signal_event = _intent_event(trace)
    stop_distance = _required_trace_float(
        trace,
        "long_stop_distance" if side is PositionSide.LONG else "short_stop_distance",
    )
    risk_scale = _required_trace_float(trace, "risk_scale")
    signal_price = item.primary.close
    sign = 1.0 if side is PositionSide.LONG else -1.0
    stop_price = signal_price - sign * stop_distance
    if settings.boolean("use_blended_capital_split"):
        sizing_equity = long_virtual if side is PositionSide.LONG else short_virtual
        if sizing_equity is None:
            raise ReplayInputError("blended sizing requires side-owned equity")
    else:
        sizing_equity = item.account.equity
    risk_multiplier = 1.0
    if (
        side is PositionSide.SHORT
        and settings.boolean("short_plus_enabled")
        and settings.boolean("use_short_side_v2")
        and settings.boolean("short_plus_risk_mult_on")
    ):
        risk_multiplier = _required_trace_float(
            trace,
            "short_plus_risk_multiplier",
            strictly_positive=False,
        )
    quantity_plan = pine_quantity_plan(
        SizingRequest(
            side=side,
            equity=sizing_equity,
            entry_reference_price=signal_price,
            stop_price=stop_price,
            risk_pct=settings.number("risk_pct"),
            risk_scale=risk_scale,
            risk_multiplier=risk_multiplier,
            cap_pct=settings.number("cap_pct"),
            point_value=(
                settings.point_value if settings.boolean("use_pointvalue_sizing") else 1.0
            ),
            quantity_step=settings.number("qty_step"),
            minimum_quantity=settings.number("min_qty"),
        )
    )
    if not quantity_plan.accepted:
        return SizingRejectionEvidence(
            signal_event_id=signal_event.event_id,
            signal_timestamp_utc=signal_event.timestamp_utc,
            side=side,
            quantity_plan=quantity_plan,
        )
    checkpoint = engine.checkpoint()
    return _PendingEntry(
        position_id=_position_id(
            signal_event,
            trace=trace,
            settings=settings,
            policy=policy,
        ),
        signal_event=signal_event,
        side=side,
        setup=signal_event.setup,
        base_pivot_at_entry=checkpoint.pending_base_pivot_at_entry,
        regime=_regime_label(trace.feature("regime")),
        quantity_plan=quantity_plan,
    )


def _intent_event(trace: FiveToolTrace) -> SignalEvent:
    for event in trace.events:
        if event.kind == trace.intent.value:
            return event
    raise ReplayInputError("signal intent is missing its immutable event identity")


def _position_id(
    signal_event: SignalEvent,
    *,
    trace: FiveToolTrace,
    settings: FiveToolSettings,
    policy: FiveToolReplayPolicy,
) -> str:
    payload = json.dumps(
        {
            "parameter_variant": policy.parameter_variant,
            "primary_sequence_id": trace.primary_sequence_id,
            "settings_digest": settings.digest,
            "signal_event_id": signal_event.event_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _required_trace_float(
    trace: FiveToolTrace,
    name: str,
    *,
    strictly_positive: bool = True,
) -> float:
    value = trace.feature(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ReplayInputError(f"entry intent has no numeric {name}")
    result = float(value)
    if not math.isfinite(result) or (strictly_positive and result <= 0.0):
        raise ReplayInputError(f"entry intent has invalid {name}")
    return result


def _regime_label(value: object) -> str:
    return "bull" if value == 1 else "bear" if value == -1 else "neutral"


def _entry_fill(
    pending: _PendingEntry,
    leg: PlannedLeg,
    *,
    raw_entry: float,
    execution_entry: float,
    entry_timestamp_utc: datetime,
    source_id: str,
    settings: FiveToolSettings,
    policy: FiveToolReplayPolicy,
) -> EntryFillEvent:
    point_value = _point_value(settings)
    return EntryFillEvent(
        fill_id=f"{pending.position_id}:{leg.leg_id}:{source_id}:entry",
        position_id=pending.position_id,
        owner_side=pending.side,
        leg_id=leg.leg_id,
        quantity=leg.quantity,
        signal_timestamp_utc=pending.signal_event.timestamp_utc,
        timestamp_utc=entry_timestamp_utc,
        source_bar_id=source_id,
        raw_price=raw_entry,
        execution_price=execution_entry,
        commission_cost=(
            execution_entry * leg.quantity * point_value * policy.commission_bps_per_fill / 10_000.0
        ),
        slippage_cost=abs(execution_entry - raw_entry) * leg.quantity * point_value,
    )


def _entry_bar_stop_fills(
    active: _ActivePosition,
    bar: OhlcBar,
    *,
    signal_stop_price: float,
    lower_timeframe_bars: tuple[OhlcBar, ...],
    policy: FiveToolReplayPolicy,
) -> tuple[FillEvent, ...]:
    """Resolve only the stop that existed when the entry order was submitted."""

    orders = tuple(
        ExitOrder(
            position_id=active.position_id,
            side=active.side,
            leg_id=leg.leg_id,
            quantity=leg.quantity,
            stop_price=signal_stop_price,
            stop_reason=ExitReason.INITIAL_STOP,
        )
        for leg in active.open_legs
    )
    return _resolve_protective_orders(
        orders,
        bar,
        lower_timeframe_bars=lower_timeframe_bars,
        policy=policy,
    )


def _protective_fills(
    active: _ActivePosition,
    bar: OhlcBar,
    *,
    lower_timeframe_bars: tuple[OhlcBar, ...],
    policy: FiveToolReplayPolicy,
) -> tuple[FillEvent, ...]:
    orders = tuple(
        ExitOrder(
            position_id=active.position_id,
            side=active.side,
            leg_id=leg.leg_id,
            quantity=leg.quantity,
            stop_price=(
                active.runner_stop
                if leg.leg_id is LegId.LEG_3 or len(active.plan.legs) == 1
                else active.leg_stop
            ),
            target_price=leg.target_price,
            stop_reason=(
                active.runner_stop_reason
                if leg.leg_id is LegId.LEG_3 or len(active.plan.legs) == 1
                else active.leg_stop_reason
            ),
            target_reason=leg.target_reason,
        )
        for leg in active.open_legs
    )
    return _resolve_protective_orders(
        orders,
        bar,
        lower_timeframe_bars=lower_timeframe_bars,
        policy=policy,
    )


def _resolve_protective_orders(
    orders: tuple[ExitOrder, ...],
    bar: OhlcBar,
    *,
    lower_timeframe_bars: tuple[OhlcBar, ...],
    policy: FiveToolReplayPolicy,
) -> tuple[FillEvent, ...]:
    fills: list[FillEvent] = []
    for order in orders:
        fill = resolve_exit_fill(
            order,
            bar,
            policy=policy.fill_policy,
            lower_timeframe_bars=(
                lower_timeframe_bars
                if policy.fill_policy is FillPolicy.LOWER_TIMEFRAME_MAGNIFIER
                else None
            ),
        )
        if fill is not None:
            if fill.gap_through:
                fill_bar = next(
                    (
                        candidate
                        for candidate in (bar, *lower_timeframe_bars)
                        if candidate.sequence_id == fill.source_bar_id
                    ),
                    None,
                )
                if fill_bar is None or fill_bar.start_timestamp_utc is None:
                    raise ReplayInputError("gap fill is missing its exact bar-open timestamp")
                fill = replace(fill, timestamp_utc=fill_bar.start_timestamp_utc)
            fills.append(fill)
    return tuple(fills)


def _close_leg(
    active: _ActivePosition,
    fill: FillEvent,
    *,
    settings: FiveToolSettings,
    policy: FiveToolReplayPolicy,
    exit_fills: list[ExitFillReceipt],
    closed_legs: list[ClosedLeg],
) -> _ActivePosition | None:
    target_limit = fill.reason in (ExitReason.TARGET_1, ExitReason.TARGET_2)
    execution_exit = _adverse_price(
        fill.price,
        active.side,
        settings=settings,
        policy=policy,
        opening=False,
        target_limit=target_limit,
    )
    entry_fill = next(item for item in active.entry_fills if item.leg_id is fill.leg_id)
    point_value = _point_value(settings)
    exit_commission = (
        execution_exit * fill.quantity * point_value * policy.commission_bps_per_fill / 10_000.0
    )
    exit_slippage = abs(execution_exit - fill.price) * fill.quantity * point_value
    receipt = ExitFillReceipt(
        fill=fill,
        execution_price=execution_exit,
        commission_cost=exit_commission,
        slippage_cost=exit_slippage,
    )
    exit_fills.append(receipt)
    sign = 1.0 if active.side is PositionSide.LONG else -1.0
    gross = sign * (fill.price - active.raw_entry_price) * fill.quantity * point_value
    closed_legs.append(
        ClosedLeg(
            position_id=active.position_id,
            leg_id=fill.leg_id,
            side=active.side,
            instrument=active.instrument,
            regime=active.regime,
            parameter_variant=policy.parameter_variant,
            planned_leg_count=len(active.plan.legs),
            entry_time_utc=active.entry_time_utc,
            exit_time_utc=fill.timestamp_utc,
            gross_pnl=gross,
            commission_cost=entry_fill.commission_cost + exit_commission,
            slippage_cost=entry_fill.slippage_cost + exit_slippage,
            turnover=((entry_fill.execution_price + execution_exit) * fill.quantity * point_value),
            exit_reason=fill.reason,
        )
    )
    active.milestones = apply_fill_to_milestones(active.milestones, fill)
    return active if active.open_legs else None


def _adverse_price(
    raw_price: float,
    side: PositionSide,
    *,
    settings: FiveToolSettings,
    policy: FiveToolReplayPolicy,
    opening: bool,
    target_limit: bool,
) -> float:
    ticks = (
        0
        if target_limit and not policy.apply_slippage_to_target_limits
        else policy.slippage_ticks_per_fill
    )
    movement = ticks * settings.minimum_tick
    sign = 1.0 if side is PositionSide.LONG else -1.0
    execution = raw_price + sign * movement if opening else raw_price - sign * movement
    if not math.isfinite(execution) or execution <= 0.0:
        raise ReplayInputError("slippage produced a non-positive execution price")
    return execution


def _point_value(settings: FiveToolSettings) -> float:
    # The Pine toggle controls only f_plan_qty/f_point_value.  Trading P&L,
    # commission notional, slippage, and turnover still carry the instrument's
    # economic contract multiplier.
    return settings.point_value


def _unrealized_net(
    active: _ActivePosition | None, *, mark_price: float, settings: FiveToolSettings
) -> float:
    if active is None:
        return 0.0
    sign = 1.0 if active.side is PositionSide.LONG else -1.0
    point_value = _point_value(settings)
    values = []
    for leg in active.open_legs:
        entry = next(item for item in active.entry_fills if item.leg_id is leg.leg_id)
        gross = sign * (mark_price - active.raw_entry_price) * leg.quantity * point_value
        values.append(gross - entry.commission_cost - entry.slippage_cost)
    return math.fsum(values)


def _update_stops(
    active: _ActivePosition,
    trace: FiveToolTrace,
    history: Sequence[OhlcBar],
    *,
    settings: FiveToolSettings,
) -> None:
    tightened_leg = milestone_stop(
        active.milestones,
        current_stop=active.leg_stop,
        entry_price=active.execution_entry_price,
        risk_distance=active.plan.risk_distance,
        break_even_offset_r=settings.number("be_offset_r"),
    )
    if not math.isclose(tightened_leg, active.leg_stop, rel_tol=1e-12, abs_tol=1e-12):
        active.leg_stop = tightened_leg
        active.leg_stop_reason = ExitReason.BREAKEVEN_STOP
    synced_runner = _tighter(active.side, active.runner_stop, active.leg_stop)
    if not math.isclose(synced_runner, active.runner_stop, rel_tol=1e-12, abs_tol=1e-12):
        active.runner_stop = synced_runner
        active.runner_stop_reason = active.leg_stop_reason

    if not settings.boolean("use_trail"):
        return
    sign = 1.0 if active.side is PositionSide.LONG else -1.0
    close = history[-1].close
    profit_r = sign * (close - active.execution_entry_price) / active.plan.risk_distance
    if profit_r < settings.number("trail_after_r"):
        return
    atr = trace.feature("atr")
    if isinstance(atr, bool) or not isinstance(atr, int | float) or not math.isfinite(atr):
        return
    lookback = settings.integer("ch_len")
    visible = history[-lookback:]
    candidate = (
        max(item.high for item in visible) - float(atr) * settings.number("ch_mult")
        if active.side is PositionSide.LONG
        else min(item.low for item in visible) + float(atr) * settings.number("ch_mult")
    )
    if candidate <= 0.0:
        return
    tightened_runner = _tighter(active.side, active.runner_stop, candidate)
    if not math.isclose(tightened_runner, active.runner_stop, rel_tol=1e-12, abs_tol=1e-12):
        active.runner_stop = tightened_runner
        active.runner_stop_reason = ExitReason.TRAILING_STOP


def _tighter(side: PositionSide, current: float, candidate: float) -> float:
    return max(current, candidate) if side is PositionSide.LONG else min(current, candidate)


def _exit_reason(trace: FiveToolTrace) -> ExitReason:
    raw = trace.feature("exit_reason_signal")
    reasons = {
        "adverse_regime": ExitReason.REGIME_EXIT,
        "avwap_failure": ExitReason.AVWAP_EXIT,
        "avwap_reclaim": ExitReason.AVWAP_EXIT,
        "relative_strength_deterioration": ExitReason.RELATIVE_STRENGTH_EXIT,
        "base_failure": ExitReason.BASE_FAILURE_EXIT,
        "time_stop": ExitReason.TIME_STOP,
    }
    if not isinstance(raw, str) or raw not in reasons:
        raise ReplayInputError(f"unsupported discretionary exit reason: {raw!r}")
    return reasons[raw]


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    raise TypeError(f"cannot encode {type(value).__name__}")
