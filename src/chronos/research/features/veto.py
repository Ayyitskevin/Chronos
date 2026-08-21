"""Named pairing vetoes.  ENTER intents only; exits always pass."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from chronos.research.features.models import (
    FeatureFamily,
    FeatureInputError,
    FeaturePolicy,
    FeatureSnapshot,
    IvState,
    TailState,
    UsdState,
    VetoDecision,
    VetoStatus,
)
from chronos.research.five_tool.models import FiveToolTrace, SignalIntent

_ENTER = {SignalIntent.ENTER_LONG, SignalIntent.ENTER_SHORT}
_STATUS_RANK = {
    VetoStatus.MISSING_COMPANION: 3,
    VetoStatus.WARMUP: 2,
    VetoStatus.VETO: 1,
    VetoStatus.ALLOW: 0,
}


def _snapshot_map(
    snapshots: Sequence[FeatureSnapshot],
) -> dict[FeatureFamily, FeatureSnapshot]:
    mapped: dict[FeatureFamily, FeatureSnapshot] = {}
    for snapshot in snapshots:
        if snapshot.family in mapped:
            raise FeatureInputError(f"duplicate snapshot family {snapshot.family}")
        mapped[snapshot.family] = snapshot
    return mapped


def _tail_reasons(snapshot: FeatureSnapshot) -> tuple[VetoStatus, tuple[str, ...]]:
    if snapshot.missing_required:
        return VetoStatus.MISSING_COMPANION, ("tail_risk:missing_companion",)
    if snapshot.warmup:
        return VetoStatus.WARMUP, ("tail_risk:warmup",)
    state = snapshot.value("TR_STATE")
    if state == TailState.FAT_TAILED.value:
        return VetoStatus.VETO, ("tail_risk:FAT_TAILED",)
    return VetoStatus.ALLOW, ()


def _rvol_reasons(snapshot: FeatureSnapshot) -> tuple[VetoStatus, tuple[str, ...]]:
    if snapshot.missing_required:
        return VetoStatus.MISSING_COMPANION, ("rvol:missing_companion",)
    if snapshot.warmup:
        return VetoStatus.WARMUP, ("rvol:warmup",)
    in_play = snapshot.value("IN_PLAY")
    if in_play is not True:
        return VetoStatus.VETO, ("rvol:not_in_play",)
    return VetoStatus.ALLOW, ()


def _iv_reasons(snapshot: FeatureSnapshot) -> tuple[VetoStatus, tuple[str, ...]]:
    if snapshot.missing_required:
        return VetoStatus.MISSING_COMPANION, ("iv_regime:missing_companion",)
    if snapshot.warmup:
        return VetoStatus.WARMUP, ("iv_regime:warmup",)
    state = snapshot.value("IVP_STATE")
    backwardation = snapshot.value("IVP_BACKWARDATION")
    if state == IvState.STRESS.value:
        return VetoStatus.VETO, ("iv_regime:STRESS",)
    if state == IvState.ELEVATED.value and backwardation is True:
        return VetoStatus.VETO, ("iv_regime:ELEVATED_backwardation",)
    return VetoStatus.ALLOW, ()


def _breadth_reasons(snapshot: FeatureSnapshot) -> tuple[VetoStatus, tuple[str, ...]]:
    if snapshot.missing_required:
        return VetoStatus.MISSING_COMPANION, ("breadth:missing_companion",)
    if snapshot.warmup:
        return VetoStatus.WARMUP, ("breadth:warmup",)
    align = snapshot.value("ALIGN")
    if align == -1:
        return VetoStatus.VETO, ("breadth:ALIGN_divergent",)
    return VetoStatus.ALLOW, ()


def _usd_reasons(snapshot: FeatureSnapshot) -> tuple[VetoStatus, tuple[str, ...]]:
    if snapshot.missing_required:
        return VetoStatus.MISSING_COMPANION, ("usd_regime:missing_companion",)
    if snapshot.warmup:
        return VetoStatus.WARMUP, ("usd_regime:warmup",)
    state = snapshot.value("USD_STATE")
    if state == UsdState.RISING.value:
        return VetoStatus.VETO, ("usd_regime:RISING",)
    return VetoStatus.ALLOW, ()


_FAMILY_REASONERS = {
    FeatureFamily.TAIL_RISK: _tail_reasons,
    FeatureFamily.RVOL: _rvol_reasons,
    FeatureFamily.IV_REGIME: _iv_reasons,
    FeatureFamily.BREADTH: _breadth_reasons,
    FeatureFamily.USD_REGIME: _usd_reasons,
}


def decide_veto(
    intent: SignalIntent,
    snapshots: Sequence[FeatureSnapshot],
    policy: FeaturePolicy,
    *,
    symbol: str,
) -> VetoDecision:
    """Return the research veto for one bar.  Exits are never masked."""

    if intent not in _ENTER:
        return VetoDecision(
            status=VetoStatus.ALLOW,
            original_intent=intent,
            filtered_intent=intent,
            reasons=(),
        )
    mapped = _snapshot_map(snapshots)
    status = VetoStatus.ALLOW
    reasons: list[str] = []
    for family in policy.enabled_families(symbol):
        snapshot = mapped.get(family)
        if snapshot is None:
            status = VetoStatus.MISSING_COMPANION
            reasons.append(f"{family.value}:snapshot_absent")
            continue
        family_status, family_reasons = _FAMILY_REASONERS[family](snapshot)
        if _STATUS_RANK[family_status] > _STATUS_RANK[status]:
            status = family_status
        reasons.extend(family_reasons)
    filtered = intent if status is VetoStatus.ALLOW else SignalIntent.NONE
    return VetoDecision(
        status=status,
        original_intent=intent,
        filtered_intent=filtered,
        reasons=tuple(reasons),
    )


def apply_vetoes(
    traces: Sequence[FiveToolTrace],
    snapshots_by_bar: Sequence[Sequence[FeatureSnapshot]] | Mapping[str, Sequence[FeatureSnapshot]],
    policy: FeaturePolicy,
    *,
    symbol: str,
) -> tuple[VetoDecision, ...]:
    """Apply named vetoes to an immutable Five-Tool opportunity stream."""

    if isinstance(snapshots_by_bar, Mapping):
        rows = []
        for trace in traces:
            rows.append(tuple(snapshots_by_bar.get(trace.primary_sequence_id, ())))
        snapshot_rows: tuple[Sequence[FeatureSnapshot], ...] = tuple(rows)
    else:
        snapshot_rows = tuple(snapshots_by_bar)
        if len(snapshot_rows) != len(traces):
            raise FeatureInputError("snapshot rows must match the Five-Tool trace count")
    return tuple(
        decide_veto(trace.intent, snapshots, policy, symbol=symbol)
        for trace, snapshots in zip(traces, snapshot_rows, strict=True)
    )
