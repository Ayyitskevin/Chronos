"""Join immutable Five-Tool traces to sidecar snapshots without rewriting them."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from chronos.research.features.models import (
    FeatureInputError,
    FeaturePolicy,
    FeatureSnapshot,
    PairingFrame,
)
from chronos.research.features.veto import apply_vetoes
from chronos.research.five_tool.models import FiveToolTrace


@dataclass(frozen=True, slots=True)
class PairingComposition:
    """Joined pairing evidence.  ``traces`` are the original Five-Tool objects."""

    traces: tuple[FiveToolTrace, ...]
    frames: tuple[PairingFrame, ...]
    policy_digest: str


def compose_pairing_frames(
    traces: Sequence[FiveToolTrace],
    snapshots: Sequence[FeatureSnapshot],
    policy: FeaturePolicy,
    *,
    symbol: str,
) -> PairingComposition:
    """Join on timestamp and primary sequence identity; refuse drift."""

    original = tuple(traces)
    by_identity: dict[tuple[object, str], list[FeatureSnapshot]] = {}
    for snapshot in snapshots:
        key = (snapshot.timestamp_utc, snapshot.primary_sequence_id)
        by_identity.setdefault(key, []).append(snapshot)
    rows: list[tuple[FeatureSnapshot, ...]] = []
    for trace in original:
        key = (trace.timestamp_utc, trace.primary_sequence_id)
        row = tuple(by_identity.get(key, ()))
        for snapshot in row:
            if snapshot.timestamp_utc != trace.timestamp_utc:
                raise FeatureInputError("snapshot timestamp drifted from the Five-Tool trace")
            if snapshot.primary_sequence_id != trace.primary_sequence_id:
                raise FeatureInputError(
                    "snapshot primary identity drifted from the Five-Tool trace"
                )
        rows.append(row)
    decisions = apply_vetoes(original, rows, policy, symbol=symbol)
    frames = tuple(
        PairingFrame(
            timestamp_utc=trace.timestamp_utc,
            primary_sequence_id=trace.primary_sequence_id,
            original_intent=trace.intent,
            snapshots=row,
            decision=decision,
        )
        for trace, row, decision in zip(original, rows, decisions, strict=True)
    )
    return PairingComposition(traces=original, frames=frames, policy_digest=policy.digest)
