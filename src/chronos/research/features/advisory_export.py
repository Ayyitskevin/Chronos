"""Project pairing frames into JSON-ready advisory dicts.

This module must not import ``chronos.autonomy``. Autonomy validates the same
schema string independently. The export is closed-bar signal facts only —
size, stop, equity, and risk-budget keys are stripped, not forwarded.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from chronos.research.features.models import FeatureSnapshot, PairingFrame, VetoDecision
from chronos.research.five_tool.models import FiveToolTrace

ADVISORY_EXPORT_SCHEMA = "chronos-five-tool-advisory-export-v1"

#: Closed-bar Five-Tool facts a model may see. Economic-looking keys stay out
#: even when the host trace carries them.
_FIVE_TOOL_VALUE_ALLOWLIST = frozenset(
    {
        "adx",
        "avwap",
        "avwap_age",
        "avwap_sd",
        "candidate_regime",
        "chop_risk",
        "dwell_percentile",
        "efficiency_ratio",
        "enter_z",
        "exit_z",
        "extension",
        "extension_active",
        "gap_shock",
        "hidden_bear_divergence",
        "hidden_bull_divergence",
        "htf_valid",
        "internal_regime",
        "internal_regime_age",
        "long_score",
        "mansfield",
        "mansfield_rising",
        "markov_gate_p_stay",
        "markov_maturity",
        "markov_p_stay",
        "oscillator",
        "regime",
        "regime_age",
        "regime_flip",
        "regime_z",
        "regular_bear_divergence",
        "regular_bull_divergence",
        "rs_new_high",
        "rs_new_low",
        "short_score",
        "strength",
    }
)


def _json_value(value: object) -> bool | int | float | str | None:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value
    return str(value)


def _datums(
    values: Mapping[str, object] | tuple[tuple[str, object], ...],
) -> list[dict[str, object]]:
    items = values.items() if isinstance(values, Mapping) else values
    return [{"name": name, "value": _json_value(item)} for name, item in items]


def project_five_tool_trace(trace: FiveToolTrace, *, symbol: str) -> dict[str, Any]:
    """Closed-bar Five-Tool facts. Drops size, stop, and equity keys."""

    selected = [
        (name, value) for name, value in trace.features if name in _FIVE_TOOL_VALUE_ALLOWLIST
    ]
    return {
        "advisory": True,
        "symbol": symbol.strip().upper(),
        "timestamp_utc": trace.timestamp_utc.isoformat(),
        "primary_sequence_id": trace.primary_sequence_id,
        "bar_index": trace.bar_index,
        "intent": trace.intent.value,
        "long_setup": trace.long_setup.value,
        "short_setup": trace.short_setup.value,
        "warmup_blockers": list(trace.warmup_blockers),
        "state_digest": trace.state_digest,
        "values": _datums(tuple(selected)),
    }


def project_feature_snapshot(snapshot: FeatureSnapshot) -> dict[str, Any]:
    return {
        "advisory": True,
        "family": snapshot.family.value,
        "timestamp_utc": snapshot.timestamp_utc.isoformat(),
        "primary_sequence_id": snapshot.primary_sequence_id,
        "warmup": snapshot.warmup,
        "missing_required": list(snapshot.missing_required),
        "values": _datums(snapshot.values),
    }


def project_veto(decision: VetoDecision) -> dict[str, Any]:
    return {
        "advisory": True,
        "status": decision.status.value,
        "original_intent": decision.original_intent.value,
        "filtered_intent": decision.filtered_intent.value,
        "reasons": list(decision.reasons),
    }


def project_pairing_frame(
    frame: PairingFrame,
    trace: FiveToolTrace,
    *,
    symbol: str,
) -> dict[str, Any]:
    """One bar's advisory pack as plain JSON-ready data."""

    if trace.timestamp_utc != frame.timestamp_utc:
        raise ValueError("trace timestamp drifted from the pairing frame")
    if trace.primary_sequence_id != frame.primary_sequence_id:
        raise ValueError("trace primary identity drifted from the pairing frame")
    if trace.intent is not frame.original_intent:
        raise ValueError("trace intent drifted from the pairing frame")
    return {
        "schema_version": ADVISORY_EXPORT_SCHEMA,
        "symbol": symbol.strip().upper(),
        "five_tool": project_five_tool_trace(trace, symbol=symbol),
        "snapshots": [project_feature_snapshot(item) for item in frame.snapshots],
        "veto": project_veto(frame.decision),
    }
