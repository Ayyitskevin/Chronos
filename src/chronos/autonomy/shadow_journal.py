"""Append-only SHADOW journal. Records proposals that were not transmitted.

This is Chronos's own training log: bundle digest, Five-Tool intent, pairing
veto, worker proposal, and the honest admission/transmit outcome. It never
imports the order or broker planes and cannot mark ``transmit=True``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator

from chronos.autonomy.base import AutonomyModel
from chronos.autonomy.decision import ProposedDecision
from chronos.autonomy.evidence import EvidenceBundle
from chronos.autonomy.worker_protocol import REFERENCE_WORKER_PINS, WorkerIdentityPins, WorkerJob

SHADOW_JOURNAL_SCHEMA = "chronos-shadow-journal-v1"


class ShadowJournalRecord(AutonomyModel):
    """One SHADOW cycle. Admission is not attempted; transmit is always false."""

    schema_version: str = Field(min_length=1, max_length=64)
    recorded_at: AwareDatetime
    job_id: str = Field(min_length=1, max_length=128)
    bundle_id: str = Field(min_length=1, max_length=128)
    bundle_digest: str = Field(min_length=64, max_length=64)
    five_tool_intent: str | None = None
    veto_status: str | None = None
    proposal_kind: str | None = None
    proposal_direction: str | None = None
    ingress_accepted: bool
    ingress_refusal: str = ""
    admission: Literal["not_attempted"] = "not_attempted"
    transmit: Literal[False] = False
    expected_pins: WorkerIdentityPins = REFERENCE_WORKER_PINS

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: str) -> str:
        if value != SHADOW_JOURNAL_SCHEMA:
            raise ValueError(f"unsupported shadow journal schema: {value}")
        return value

    @field_validator("bundle_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("bundle_digest must be a 64-character lowercase hex digest")
        return normalized


def record_shadow_cycle(
    *,
    bundle: EvidenceBundle,
    job: WorkerJob,
    recorded_at: datetime,
    proposal: ProposedDecision | None,
    ingress_accepted: bool,
    ingress_refusal: str = "",
    expected_pins: WorkerIdentityPins = REFERENCE_WORKER_PINS,
) -> ShadowJournalRecord:
    """Build one journal row. Does not write and does not submit."""

    if job.bundle_id != bundle.bundle_id or job.bundle_digest != bundle.digest():
        raise ValueError("shadow journal job does not match the issued bundle")
    signal = bundle.five_tool_signals[0] if bundle.five_tool_signals else None
    veto = bundle.pairing_vetoes[0] if bundle.pairing_vetoes else None
    return ShadowJournalRecord(
        schema_version=SHADOW_JOURNAL_SCHEMA,
        recorded_at=recorded_at,
        job_id=job.job_id,
        bundle_id=bundle.bundle_id,
        bundle_digest=bundle.digest(),
        five_tool_intent=signal.intent if signal else None,
        veto_status=veto.status if veto else None,
        proposal_kind=proposal.kind.value if proposal is not None else None,
        proposal_direction=proposal.direction.value if proposal is not None else None,
        ingress_accepted=ingress_accepted,
        ingress_refusal=ingress_refusal,
        admission="not_attempted",
        transmit=False,
        expected_pins=expected_pins,
    )


def append_shadow_record(path: Path, record: ShadowJournalRecord) -> None:
    """Append one JSON line. Refuses to follow a symlink (R-21)."""

    line = json.dumps(record.model_dump(mode="json"), sort_keys=True, default=str) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, line.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_shadow_records(path: Path) -> tuple[ShadowJournalRecord, ...]:
    if not path.exists():
        return ()
    rows: list[ShadowJournalRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(ShadowJournalRecord.model_validate(json.loads(line)))
    return tuple(rows)
