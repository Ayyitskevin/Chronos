"""Validate research-exported advisory dicts into autonomy-native facts.

Research projects JSON-ready dicts and must not import this package. This
module accepts those dicts as untrusted input and constructs the typed
facts ``EvidenceBundle`` will digest. The schema string is duplicated on
purpose so neither plane reaches the other.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from chronos.autonomy.evidence import (
    AdvisoryDatum,
    AdvisoryFeatureSnapshotFact,
    AdvisoryFiveToolFact,
    AdvisoryVetoFact,
)

ADVISORY_EXPORT_SCHEMA = "chronos-five-tool-advisory-export-v1"


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _datums(value: object) -> tuple[AdvisoryDatum, ...]:
    if not isinstance(value, list):
        raise ValueError("advisory values must be a list")
    return tuple(AdvisoryDatum.model_validate(item) for item in value)


def five_tool_fact_from_payload(payload: dict[str, Any]) -> AdvisoryFiveToolFact:
    return AdvisoryFiveToolFact(
        advisory=True,
        symbol=str(payload["symbol"]),
        timestamp_utc=_as_aware(payload["timestamp_utc"]),
        primary_sequence_id=str(payload["primary_sequence_id"]),
        bar_index=int(payload["bar_index"]),
        intent=str(payload["intent"]),
        long_setup=str(payload["long_setup"]),
        short_setup=str(payload["short_setup"]),
        warmup_blockers=tuple(str(item) for item in payload.get("warmup_blockers", ())),
        state_digest=str(payload["state_digest"]),
        values=_datums(payload.get("values", [])),
    )


def feature_snapshot_from_payload(payload: dict[str, Any]) -> AdvisoryFeatureSnapshotFact:
    return AdvisoryFeatureSnapshotFact(
        advisory=True,
        family=str(payload["family"]),
        timestamp_utc=_as_aware(payload["timestamp_utc"]),
        primary_sequence_id=str(payload["primary_sequence_id"]),
        warmup=bool(payload["warmup"]),
        missing_required=tuple(str(item) for item in payload.get("missing_required", ())),
        values=_datums(payload.get("values", [])),
    )


def veto_from_payload(payload: dict[str, Any]) -> AdvisoryVetoFact:
    return AdvisoryVetoFact(
        advisory=True,
        status=str(payload["status"]),
        original_intent=str(payload["original_intent"]),
        filtered_intent=str(payload["filtered_intent"]),
        reasons=tuple(str(item) for item in payload.get("reasons", ())),
    )


def advisory_facts_from_export(
    payload: dict[str, Any],
) -> tuple[
    tuple[AdvisoryFiveToolFact, ...],
    tuple[AdvisoryFeatureSnapshotFact, ...],
    tuple[AdvisoryVetoFact, ...],
]:
    """Turn one research export object into typed advisory collections."""

    document = _require_mapping(payload, "advisory export")
    if document.get("schema_version") != ADVISORY_EXPORT_SCHEMA:
        raise ValueError("unsupported advisory export schema")
    signal = five_tool_fact_from_payload(_require_mapping(document.get("five_tool"), "five_tool"))
    raw_snapshots = document.get("snapshots", [])
    if not isinstance(raw_snapshots, list):
        raise ValueError("snapshots must be a list")
    snapshots = tuple(
        feature_snapshot_from_payload(_require_mapping(item, "snapshot")) for item in raw_snapshots
    )
    veto = veto_from_payload(_require_mapping(document.get("veto"), "veto"))
    return (signal,), snapshots, (veto,)


def _as_aware(value: object) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("advisory timestamps must be timezone-aware")
        return value
    if not isinstance(value, str):
        raise ValueError("advisory timestamps must be ISO-8601 strings")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("advisory timestamps must be timezone-aware")
    return parsed
