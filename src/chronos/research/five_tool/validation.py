"""Deterministic, sample-honest validation evidence for Five-Tool research.

Closed Pine trades are *legs*, not independent economic positions. This module
keeps both views explicit, purges positions opened before the frozen OOS boundary,
and represents undefined metrics with tagged states instead of magic numbers.
It is descriptive evidence only: no field is a promotion decision.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum

from chronos.research.five_tool.planning import ExitReason, LegId, PositionSide


class ValidationInputError(ValueError):
    """A purported closed-leg ledger is not economically coherent."""


class ProfitFactorState(StrEnum):
    FINITE = "finite"
    UNBOUNDED_NO_LOSSES = "unbounded_no_losses"
    UNDEFINED_NO_TRADES = "undefined_no_trades"
    UNDEFINED_NO_GAINS = "undefined_no_gains"


@dataclass(frozen=True, slots=True)
class ClosedLeg:
    """One planned broker-emulator leg closure, not an independent trade."""

    position_id: str
    leg_id: LegId
    side: PositionSide
    instrument: str
    regime: str
    parameter_variant: str
    planned_leg_count: int
    entry_time_utc: datetime
    exit_time_utc: datetime
    gross_pnl: float
    commission_cost: float = 0.0
    slippage_cost: float = 0.0
    other_cost: float = 0.0
    turnover: float = 0.0
    exit_reason: ExitReason = ExitReason.INITIAL_STOP

    def __post_init__(self) -> None:
        for text_name, text_value in (
            ("position_id", self.position_id),
            ("instrument", self.instrument),
            ("regime", self.regime),
            ("parameter_variant", self.parameter_variant),
        ):
            if not text_value:
                raise ValidationInputError(f"{text_name} is required")
        for field_name, timestamp in (
            ("entry_time_utc", self.entry_time_utc),
            ("exit_time_utc", self.exit_time_utc),
        ):
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValidationInputError(f"{field_name} must be timezone-aware")
            object.__setattr__(self, field_name, timestamp.astimezone(UTC))
        if self.exit_time_utc < self.entry_time_utc:
            raise ValidationInputError("leg exit precedes entry")
        if type(self.planned_leg_count) is not int or self.planned_leg_count not in (
            1,
            2,
            3,
        ):
            raise ValidationInputError("planned_leg_count must be 1, 2, or 3")
        leg_number = {
            LegId.LEG_1: 1,
            LegId.LEG_2: 2,
            LegId.LEG_3: 3,
        }[self.leg_id]
        if leg_number > self.planned_leg_count:
            raise ValidationInputError("closed leg was not present in the entry plan")
        if not math.isfinite(self.gross_pnl):
            raise ValidationInputError("gross_pnl must be finite")
        for cost_name, cost_value in (
            ("commission_cost", self.commission_cost),
            ("slippage_cost", self.slippage_cost),
            ("other_cost", self.other_cost),
            ("turnover", self.turnover),
        ):
            if not math.isfinite(cost_value) or cost_value < 0.0:
                raise ValidationInputError(f"{cost_name} must be finite and non-negative")
        if self.exit_reason is ExitReason.TARGET_1 and not (
            self.planned_leg_count >= 2 and self.leg_id is LegId.LEG_1
        ):
            raise ValidationInputError("TARGET_1 requires split-plan leg 1")
        if self.exit_reason is ExitReason.TARGET_2:
            expected_target_2_leg = LegId.LEG_1 if self.planned_leg_count == 1 else LegId.LEG_2
            if self.leg_id is not expected_target_2_leg:
                raise ValidationInputError("TARGET_2 does not match the planned target-2 leg")

    @property
    def total_cost(self) -> float:
        return math.fsum((self.commission_cost, self.slippage_cost, self.other_cost))

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.total_cost


@dataclass(frozen=True, slots=True)
class EconomicPosition:
    """All legs sharing one entry thesis, aggregated to one observation."""

    position_id: str
    side: PositionSide
    instrument: str
    regime: str
    parameter_variant: str
    entry_time_utc: datetime
    exit_time_utc: datetime
    leg_count: int
    gross_pnl: float
    commission_cost: float
    slippage_cost: float
    other_cost: float
    turnover: float

    @property
    def total_cost(self) -> float:
        return math.fsum((self.commission_cost, self.slippage_cost, self.other_cost))

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.total_cost


@dataclass(frozen=True, slots=True)
class ProfitFactorEvidence:
    state: ProfitFactorState
    value: float | None
    gross_profit: float
    gross_loss: float
    observations: int


@dataclass(frozen=True, slots=True)
class ClosedLegChunk:
    index: int
    closed_legs: int
    net_pnl: float
    profit_factor: ProfitFactorEvidence


@dataclass(frozen=True, slots=True)
class PineDashboardHeuristic:
    label: str
    independent_observations: bool
    segment_basis: str
    chunks: tuple[ClosedLegChunk, ...]


@dataclass(frozen=True, slots=True)
class CostEvidence:
    gross_pnl: float
    commission_cost: float
    slippage_cost: float
    other_cost: float
    total_cost: float
    net_pnl: float
    turnover: float
    total_cost_fraction_of_turnover: float | None


@dataclass(frozen=True, slots=True)
class ConcentrationEvidence:
    largest_position_abs_pnl_share: float | None
    largest_instrument_abs_pnl_share: float | None
    largest_exit_month_abs_pnl_share: float | None
    best_position_share_of_gross_profit: float | None
    dominant_position_id: str | None
    dominant_instrument: str | None
    dominant_exit_month_utc: str | None


@dataclass(frozen=True, slots=True)
class RiskEvidence:
    basis: str
    observations: int
    max_drawdown_usd: float | None
    max_drawdown_fraction_of_initial_equity: float | None
    cvar_alpha: float
    cvar_tail_observations: int
    cvar_tail_mean_pnl_usd: float | None
    low_sample: bool


@dataclass(frozen=True, slots=True)
class SliceEvidence:
    key: str
    economic_positions: int
    closed_legs: int
    net_pnl: float
    profit_factor: ProfitFactorEvidence


@dataclass(frozen=True, slots=True)
class ParameterSensitivityEvidence:
    variants: tuple[SliceEvidence, ...]
    positive_variant_fraction: float | None
    best_variant_net_pnl: float | None
    worst_variant_net_pnl: float | None
    net_pnl_spread: float | None
    plateau_supported: bool | None
    plateau_note: str


@dataclass(frozen=True, slots=True)
class RemovalEvidence:
    basis: str
    removed_key: str | None
    removed_net_pnl: float | None
    baseline_net_pnl: float
    net_pnl_after_removal: float | None
    positive_after_removal: bool | None


@dataclass(frozen=True, slots=True)
class OosEvidence:
    selection_basis: str
    start_utc: datetime
    closed_legs: int
    economic_positions: int
    position_ids_in_exit_order: tuple[str, ...]
    net_pnl: float
    profit_factor: ProfitFactorEvidence


@dataclass(frozen=True, slots=True)
class FiveToolValidationReport:
    schema_version: int
    accounting_basis: str
    closed_leg_count: int
    economic_position_count: int
    all_closed_legs_profit_factor: ProfitFactorEvidence
    all_economic_positions_profit_factor: ProfitFactorEvidence
    oos: OosEvidence
    pine_dashboard: PineDashboardHeuristic
    costs: CostEvidence
    concentration: ConcentrationEvidence
    risk: RiskEvidence
    regime_evidence: tuple[SliceEvidence, ...]
    instrument_evidence: tuple[SliceEvidence, ...]
    parameter_sensitivity: ParameterSensitivityEvidence
    best_trade_removal: RemovalEvidence
    best_month_removal: RemovalEvidence
    chronological_position_ids: tuple[str, ...]
    low_sample: bool
    warnings: tuple[str, ...]

    @property
    def digest(self) -> str:
        """Content digest of a canonical, NaN-free representation."""

        payload = json.dumps(
            asdict(self),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_json_default,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def aggregate_economic_positions(legs: Sequence[ClosedLeg]) -> tuple[EconomicPosition, ...]:
    """Aggregate multi-leg closures and return deterministic exit-time order."""

    grouped: dict[str, list[ClosedLeg]] = defaultdict(list)
    for leg in legs:
        grouped[leg.position_id].append(leg)

    positions: list[EconomicPosition] = []
    for position_id in sorted(grouped):
        members = grouped[position_id]
        first = members[0]
        if len({member.leg_id for member in members}) != len(members):
            raise ValidationInputError(f"position {position_id!r} repeats a leg id")
        for member in members[1:]:
            metadata = (
                member.side,
                member.instrument,
                member.regime,
                member.parameter_variant,
                member.planned_leg_count,
                member.entry_time_utc,
            )
            expected = (
                first.side,
                first.instrument,
                first.regime,
                first.parameter_variant,
                first.planned_leg_count,
                first.entry_time_utc,
            )
            if metadata != expected:
                raise ValidationInputError(
                    f"position {position_id!r} has inconsistent entry metadata"
                )
        ordered_leg_ids = (LegId.LEG_1, LegId.LEG_2, LegId.LEG_3)
        expected_legs = set(ordered_leg_ids[: first.planned_leg_count])
        observed_legs = {member.leg_id for member in members}
        if observed_legs != expected_legs:
            missing = sorted(leg.value for leg in expected_legs - observed_legs)
            extra = sorted(leg.value for leg in observed_legs - expected_legs)
            raise ValidationInputError(
                f"position {position_id!r} is not fully closed; missing={missing}, extra={extra}"
            )
        positions.append(
            EconomicPosition(
                position_id=position_id,
                side=first.side,
                instrument=first.instrument,
                regime=first.regime,
                parameter_variant=first.parameter_variant,
                entry_time_utc=first.entry_time_utc,
                exit_time_utc=max(member.exit_time_utc for member in members),
                leg_count=len(members),
                gross_pnl=math.fsum(member.gross_pnl for member in members),
                commission_cost=math.fsum(member.commission_cost for member in members),
                slippage_cost=math.fsum(member.slippage_cost for member in members),
                other_cost=math.fsum(member.other_cost for member in members),
                turnover=math.fsum(member.turnover for member in members),
            )
        )
    return tuple(sorted(positions, key=lambda item: (item.exit_time_utc, item.position_id)))


def profit_factor(values: Sequence[float]) -> ProfitFactorEvidence:
    """Return an explicitly tagged PF; infinity/999 sentinels are forbidden."""

    if not values:
        return ProfitFactorEvidence(
            state=ProfitFactorState.UNDEFINED_NO_TRADES,
            value=None,
            gross_profit=0.0,
            gross_loss=0.0,
            observations=0,
        )
    if not all(math.isfinite(value) for value in values):
        raise ValidationInputError("profit-factor observations must be finite")
    gross_profit = math.fsum(value for value in values if value > 0.0)
    gross_loss = -math.fsum(value for value in values if value < 0.0)
    if gross_profit <= 0.0:
        return ProfitFactorEvidence(
            state=ProfitFactorState.UNDEFINED_NO_GAINS,
            value=0.0,
            gross_profit=0.0,
            gross_loss=gross_loss,
            observations=len(values),
        )
    if gross_loss <= 0.0:
        return ProfitFactorEvidence(
            state=ProfitFactorState.UNBOUNDED_NO_LOSSES,
            value=None,
            gross_profit=gross_profit,
            gross_loss=0.0,
            observations=len(values),
        )
    return ProfitFactorEvidence(
        state=ProfitFactorState.FINITE,
        value=gross_profit / gross_loss,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        observations=len(values),
    )


def chronological_oos_legs(
    legs: Sequence[ClosedLeg], *, start_utc: datetime
) -> tuple[ClosedLeg, ...]:
    """Return complete positions opened on/after the frozen OOS boundary."""

    start = _aware_utc(start_utc, "OOS start")
    eligible_position_ids = {
        position.position_id
        for position in aggregate_economic_positions(legs)
        if position.entry_time_utc >= start
    }
    return tuple(
        sorted(
            (leg for leg in legs if leg.position_id in eligible_position_ids),
            key=lambda item: (item.exit_time_utc, item.position_id, item.leg_id),
        )
    )


def pine_equal_count_closed_leg_dashboard(
    legs: Sequence[ClosedLeg], *, segments: int
) -> PineDashboardHeuristic:
    """Mirror Pine's equal-count closed-leg chunks and label the limitation."""

    effective_segments = max(2, min(segments, 8))
    ordered = sorted(legs, key=lambda item: (item.exit_time_utc, item.position_id, item.leg_id))
    buckets: list[list[ClosedLeg]] = [[] for _ in range(effective_segments)]
    count = len(ordered)
    for sequence, leg in enumerate(ordered):
        index = min(
            effective_segments - 1,
            math.floor(sequence * effective_segments / max(count, 1)),
        )
        buckets[index].append(leg)
    chunks = tuple(
        ClosedLegChunk(
            index=index,
            closed_legs=len(bucket),
            net_pnl=math.fsum(leg.net_pnl for leg in bucket),
            profit_factor=profit_factor([leg.net_pnl for leg in bucket]),
        )
        for index, bucket in enumerate(buckets)
    )
    return PineDashboardHeuristic(
        label="heuristic_equal_count_closed_legs_not_walk_forward",
        independent_observations=False,
        segment_basis="closed-leg sequence after exit-time ordering",
        chunks=chunks,
    )


def build_validation_report(
    legs: Sequence[ClosedLeg],
    *,
    oos_start_utc: datetime,
    initial_equity: float | None = None,
    pine_segments: int = 4,
    cvar_alpha: float = 0.05,
    minimum_economic_positions: int = 30,
    parameter_neighbors: Mapping[str, Sequence[str]] | None = None,
) -> FiveToolValidationReport:
    """Build a deterministic report, returning unknowns for unsupported samples."""

    if initial_equity is not None and (not math.isfinite(initial_equity) or initial_equity <= 0):
        raise ValidationInputError("initial_equity must be finite and positive when supplied")
    if not math.isfinite(cvar_alpha) or not 0.0 < cvar_alpha <= 1.0:
        raise ValidationInputError("cvar_alpha must be in (0, 1]")
    if minimum_economic_positions < 1:
        raise ValidationInputError("minimum_economic_positions must be positive")
    start = _aware_utc(oos_start_utc, "OOS start")
    ordered_legs = tuple(
        sorted(legs, key=lambda item: (item.exit_time_utc, item.position_id, item.leg_id))
    )
    positions = aggregate_economic_positions(ordered_legs)
    # Purge boundary-straddling positions.  Without a frozen mark-to-market at the
    # boundary, including their complete P&L would leak pre-OOS economics into OOS.
    oos_positions = tuple(position for position in positions if position.entry_time_utc >= start)
    oos_position_ids = {position.position_id for position in oos_positions}
    oos_legs = tuple(leg for leg in ordered_legs if leg.position_id in oos_position_ids)
    straddling = tuple(
        position
        for position in positions
        if position.entry_time_utc < start <= position.exit_time_utc
    )

    position_values = [position.net_pnl for position in positions]
    oos_values = [position.net_pnl for position in oos_positions]
    warnings: list[str] = []
    if not ordered_legs:
        warnings.append("no closed legs; trade-derived metrics are undefined")
    if len(positions) < minimum_economic_positions:
        warnings.append(
            "economic-position sample is below the configured minimum; no promotion inference"
        )
    if not oos_positions:
        warnings.append("no economic positions were opened in the OOS interval")
    if straddling:
        warnings.append(
            f"purged {len(straddling)} boundary-straddling economic position(s) from OOS"
        )
    if len(ordered_legs) != len(positions):
        warnings.append("closed legs are not independent; use economic_position_count for N")

    parameter_sensitivity = _parameter_sensitivity(positions, parameter_neighbors)
    if parameter_sensitivity.plateau_supported is None:
        warnings.append("parameter plateau is unassessed without a declared neighbor graph")

    return FiveToolValidationReport(
        schema_version=1,
        accounting_basis=(
            "complete economic positions aggregate every planned leg closure sharing position_id"
        ),
        closed_leg_count=len(ordered_legs),
        economic_position_count=len(positions),
        all_closed_legs_profit_factor=profit_factor([leg.net_pnl for leg in ordered_legs]),
        all_economic_positions_profit_factor=profit_factor(position_values),
        oos=OosEvidence(
            selection_basis=(
                "economic position entry_time_utc >= frozen OOS start; "
                "boundary-straddling positions purged"
            ),
            start_utc=start,
            closed_legs=len(oos_legs),
            economic_positions=len(oos_positions),
            position_ids_in_exit_order=tuple(position.position_id for position in oos_positions),
            net_pnl=math.fsum(oos_values),
            profit_factor=profit_factor(oos_values),
        ),
        pine_dashboard=pine_equal_count_closed_leg_dashboard(ordered_legs, segments=pine_segments),
        costs=_cost_evidence(ordered_legs),
        concentration=_concentration(positions),
        risk=_risk_evidence(positions, initial_equity=initial_equity, alpha=cvar_alpha),
        regime_evidence=_slices(positions, ordered_legs, key_name="regime"),
        instrument_evidence=_slices(positions, ordered_legs, key_name="instrument"),
        parameter_sensitivity=parameter_sensitivity,
        best_trade_removal=_best_trade_removal(positions),
        best_month_removal=_best_month_removal(positions),
        chronological_position_ids=tuple(position.position_id for position in positions),
        low_sample=len(positions) < minimum_economic_positions,
        warnings=tuple(warnings),
    )


def _cost_evidence(legs: Sequence[ClosedLeg]) -> CostEvidence:
    gross = math.fsum(leg.gross_pnl for leg in legs)
    commission = math.fsum(leg.commission_cost for leg in legs)
    slippage = math.fsum(leg.slippage_cost for leg in legs)
    other = math.fsum(leg.other_cost for leg in legs)
    total = math.fsum((commission, slippage, other))
    turnover = math.fsum(leg.turnover for leg in legs)
    return CostEvidence(
        gross_pnl=gross,
        commission_cost=commission,
        slippage_cost=slippage,
        other_cost=other,
        total_cost=total,
        net_pnl=gross - total,
        turnover=turnover,
        total_cost_fraction_of_turnover=total / turnover if turnover > 0.0 else None,
    )


def _concentration(positions: Sequence[EconomicPosition]) -> ConcentrationEvidence:
    if not positions:
        return ConcentrationEvidence(None, None, None, None, None, None, None)
    total_abs = math.fsum(abs(position.net_pnl) for position in positions)
    gross_profit = math.fsum(max(position.net_pnl, 0.0) for position in positions)
    dominant = max(positions, key=lambda item: (abs(item.net_pnl), item.position_id))
    by_instrument_abs = _group_abs(positions, lambda item: item.instrument)
    by_month_abs = _group_abs(positions, lambda item: _exit_month(item.exit_time_utc))
    dominant_instrument = max(by_instrument_abs, key=lambda key: (by_instrument_abs[key], key))
    dominant_month = max(by_month_abs, key=lambda key: (by_month_abs[key], key))
    best = max(positions, key=lambda item: (item.net_pnl, item.position_id))
    return ConcentrationEvidence(
        largest_position_abs_pnl_share=abs(dominant.net_pnl) / total_abs
        if total_abs > 0.0
        else None,
        largest_instrument_abs_pnl_share=by_instrument_abs[dominant_instrument] / total_abs
        if total_abs > 0.0
        else None,
        largest_exit_month_abs_pnl_share=by_month_abs[dominant_month] / total_abs
        if total_abs > 0.0
        else None,
        best_position_share_of_gross_profit=best.net_pnl / gross_profit
        if gross_profit > 0.0 and best.net_pnl > 0.0
        else None,
        dominant_position_id=dominant.position_id,
        dominant_instrument=dominant_instrument,
        dominant_exit_month_utc=dominant_month,
    )


def _risk_evidence(
    positions: Sequence[EconomicPosition], *, initial_equity: float | None, alpha: float
) -> RiskEvidence:
    if not positions:
        return RiskEvidence(
            basis="chronological economic-position net P&L",
            observations=0,
            max_drawdown_usd=None,
            max_drawdown_fraction_of_initial_equity=None,
            cvar_alpha=alpha,
            cvar_tail_observations=0,
            cvar_tail_mean_pnl_usd=None,
            low_sample=True,
        )
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    values: list[float] = []
    by_exit: dict[datetime, list[float]] = defaultdict(list)
    for position in positions:
        values.append(position.net_pnl)
        by_exit[position.exit_time_utc].append(position.net_pnl)
    for exit_time in sorted(by_exit):
        cumulative += math.fsum(by_exit[exit_time])
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    tail_count = max(1, math.ceil(len(values) * alpha))
    tail = sorted(values)[:tail_count]
    return RiskEvidence(
        basis="chronological economic-position net P&L",
        observations=len(values),
        max_drawdown_usd=max_drawdown,
        max_drawdown_fraction_of_initial_equity=max_drawdown / initial_equity
        if initial_equity is not None
        else None,
        cvar_alpha=alpha,
        cvar_tail_observations=tail_count,
        cvar_tail_mean_pnl_usd=math.fsum(tail) / tail_count,
        low_sample=len(values) < 20,
    )


def _slices(
    positions: Sequence[EconomicPosition],
    legs: Sequence[ClosedLeg],
    *,
    key_name: str,
) -> tuple[SliceEvidence, ...]:
    position_groups: dict[str, list[EconomicPosition]] = defaultdict(list)
    leg_counts: dict[str, int] = defaultdict(int)
    for position in positions:
        position_groups[str(getattr(position, key_name))].append(position)
    for leg in legs:
        leg_counts[str(getattr(leg, key_name))] += 1
    return tuple(
        SliceEvidence(
            key=key,
            economic_positions=len(position_groups[key]),
            closed_legs=leg_counts[key],
            net_pnl=math.fsum(item.net_pnl for item in position_groups[key]),
            profit_factor=profit_factor([item.net_pnl for item in position_groups[key]]),
        )
        for key in sorted(position_groups)
    )


def _parameter_sensitivity(
    positions: Sequence[EconomicPosition],
    neighbors: Mapping[str, Sequence[str]] | None,
) -> ParameterSensitivityEvidence:
    # A one-leg projection is enough for _slices because only counts and position
    # metadata are read from the leg side.  Build directly to avoid fake leg records.
    grouped: dict[str, list[EconomicPosition]] = defaultdict(list)
    for position in positions:
        grouped[position.parameter_variant].append(position)
    variants = tuple(
        SliceEvidence(
            key=key,
            economic_positions=len(group),
            closed_legs=sum(item.leg_count for item in group),
            net_pnl=math.fsum(item.net_pnl for item in group),
            profit_factor=profit_factor([item.net_pnl for item in group]),
        )
        for key, group in sorted(grouped.items())
    )
    nets = [variant.net_pnl for variant in variants]
    positive_fraction = sum(value > 0.0 for value in nets) / len(nets) if nets else None
    plateau: bool | None = None
    note = "neighbor graph not supplied; isolated-optimum risk remains untested"
    if neighbors is not None and variants:
        known = {variant.key: variant.net_pnl for variant in variants}
        invalid = sorted(set(neighbors) - set(known))
        if invalid:
            raise ValidationInputError(f"parameter neighbor graph has unknown variants: {invalid}")
        best_key = max(known, key=lambda key: (known[key], key))
        adjacent = tuple(neighbors.get(best_key, ()))
        if len(adjacent) != len(set(adjacent)):
            raise ValidationInputError("parameter neighbor graph contains duplicate neighbors")
        if best_key in adjacent:
            raise ValidationInputError("parameter neighbor graph cannot contain self-neighbors")
        if any(key not in known for key in adjacent):
            raise ValidationInputError("parameter neighbor graph references an unknown neighbor")
        positive_neighbors = sum(known[key] > 0.0 for key in adjacent)
        plateau = bool(adjacent) and positive_neighbors / len(adjacent) >= 0.67
        note = (
            "at least 67% of the best variant's declared neighbors are positive"
            if plateau
            else ("best variant lacks a 67% positive declared neighborhood")
        )
    return ParameterSensitivityEvidence(
        variants=variants,
        positive_variant_fraction=positive_fraction,
        best_variant_net_pnl=max(nets) if nets else None,
        worst_variant_net_pnl=min(nets) if nets else None,
        net_pnl_spread=max(nets) - min(nets) if nets else None,
        plateau_supported=plateau,
        plateau_note=note,
    )


def _best_trade_removal(positions: Sequence[EconomicPosition]) -> RemovalEvidence:
    baseline = math.fsum(position.net_pnl for position in positions)
    if not positions:
        return RemovalEvidence("economic_position", None, None, baseline, None, None)
    best = max(positions, key=lambda item: (item.net_pnl, item.position_id))
    stressed = baseline - best.net_pnl
    return RemovalEvidence(
        basis="economic_position",
        removed_key=best.position_id,
        removed_net_pnl=best.net_pnl,
        baseline_net_pnl=baseline,
        net_pnl_after_removal=stressed,
        positive_after_removal=stressed > 0.0,
    )


def _best_month_removal(positions: Sequence[EconomicPosition]) -> RemovalEvidence:
    baseline = math.fsum(position.net_pnl for position in positions)
    if not positions:
        return RemovalEvidence("exit_month_utc", None, None, baseline, None, None)
    by_month = _group_net(positions, lambda item: _exit_month(item.exit_time_utc))
    best_month = max(by_month, key=lambda key: (by_month[key], key))
    stressed = baseline - by_month[best_month]
    return RemovalEvidence(
        basis="exit_month_utc",
        removed_key=best_month,
        removed_net_pnl=by_month[best_month],
        baseline_net_pnl=baseline,
        net_pnl_after_removal=stressed,
        positive_after_removal=stressed > 0.0,
    )


def _group_net(
    positions: Sequence[EconomicPosition], key: Callable[[EconomicPosition], str]
) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for position in positions:
        grouped[key(position)].append(position.net_pnl)
    return {name: math.fsum(values) for name, values in grouped.items()}


def _group_abs(
    positions: Sequence[EconomicPosition], key: Callable[[EconomicPosition], str]
) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for position in positions:
        grouped[key(position)].append(abs(position.net_pnl))
    return {name: math.fsum(values) for name, values in grouped.items()}


def _exit_month(timestamp: datetime) -> str:
    return f"{timestamp.year:04d}-{timestamp.month:02d}"


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationInputError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")
