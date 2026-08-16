"""Pairing on-versus-off replay.  Masks ENTER only; reuses Five-Tool fills.

The opportunity stream is the control engine traces.  Treatment reuses the fill
path with ENTER intents masked; its engine traces may differ because Five-Tool
sizing and halts read equity.  Pairing identity binds the control traces.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from chronos.research.features.compose import PairingComposition, compose_pairing_frames
from chronos.research.features.models import FeatureInputError, FeaturePolicy, FeatureSnapshot
from chronos.research.five_tool.models import FiveToolSettings, FiveToolTrace, SignalIntent
from chronos.research.five_tool.replay import (
    FiveToolReplayPolicy,
    FiveToolReplayResult,
    ReplayBar,
    ReplayInputError,
    replay_five_tool,
)

_ENTER = {SignalIntent.ENTER_LONG, SignalIntent.ENTER_SHORT}


@dataclass(frozen=True, slots=True)
class PairingReplayResult:
    """Control versus treatment fill evidence on one frozen opportunity stream."""

    composition: PairingComposition
    control: FiveToolReplayResult
    treatment: FiveToolReplayResult
    control_enter_count: int
    treatment_enter_count: int

    def __post_init__(self) -> None:
        if self.control.traces != self.composition.traces:
            raise FeatureInputError("pairing composition traces drifted from replay traces")
        if self.control_enter_count < self.treatment_enter_count:
            raise FeatureInputError("treatment cannot add ENTER intents absent from control")


def _override_from_composition(
    composition: PairingComposition,
) -> Callable[[FiveToolTrace], SignalIntent]:
    decisions = {frame.primary_sequence_id: frame.decision for frame in composition.frames}

    def override(trace: FiveToolTrace) -> SignalIntent:
        decision = decisions.get(trace.primary_sequence_id)
        if decision is None:
            raise ReplayInputError("pairing override is missing a veto decision")
        return decision.filtered_intent

    return override


def replay_pairing(
    settings: FiveToolSettings,
    bars: Sequence[ReplayBar],
    snapshots: Sequence[FeatureSnapshot],
    policy: FeaturePolicy,
    *,
    replay_policy: FiveToolReplayPolicy | None = None,
) -> PairingReplayResult:
    """Run control fills, then treatment fills with ENTER intents masked by vetoes."""

    fill_policy = replay_policy or FiveToolReplayPolicy()
    control = replay_five_tool(settings, bars, policy=fill_policy)
    symbols = {bar.input.primary.symbol.strip().upper() for bar in bars}
    if len(symbols) != 1:
        raise FeatureInputError("pairing replay requires exactly one primary symbol")
    composition = compose_pairing_frames(
        control.traces, snapshots, policy, symbol=next(iter(symbols))
    )
    for frame in composition.frames:
        if (
            frame.original_intent not in _ENTER
            and frame.decision.filtered_intent is not frame.original_intent
        ):
            raise FeatureInputError("pairing replay attempted to mask a non-ENTER intent")
    treatment = replay_five_tool(
        settings,
        bars,
        policy=fill_policy,
        intent_override=_override_from_composition(composition),
    )
    return PairingReplayResult(
        composition=composition,
        control=control,
        treatment=treatment,
        control_enter_count=sum(1 for trace in control.traces if trace.intent in _ENTER),
        treatment_enter_count=sum(
            1 for frame in composition.frames if frame.decision.filtered_intent in _ENTER
        ),
    )
