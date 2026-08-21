"""SHADOW learning cycle: ingress a worker payload and journal it. No transmit.

Omitting ``submit`` on ``run_cycle`` is already SHADOW. This helper is narrower:
it parses bytes through the same ingress, records HOLD and refused OPEN, and
never stamps provenance, admits, sizes, compiles, or hands off an order.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from chronos.autonomy.evidence import EvidenceBundle
from chronos.autonomy.reference_worker import propose_payload
from chronos.autonomy.shadow_journal import (
    ShadowJournalRecord,
    append_shadow_record,
    record_shadow_cycle,
)
from chronos.autonomy.worker_protocol import WorkerJob, WorkerRequest
from chronos.supervisor.ingress import parse_proposal


def journal_worker_payload(
    *,
    bundle: EvidenceBundle,
    job: WorkerJob,
    payload: bytes,
    journal_path: Path,
    recorded_at: datetime,
) -> ShadowJournalRecord:
    """Parse one worker payload and append a not-sent journal row."""

    if recorded_at >= job.expires_at:
        record = record_shadow_cycle(
            bundle=bundle,
            job=job,
            recorded_at=recorded_at,
            proposal=None,
            ingress_accepted=False,
            ingress_refusal="worker job expired before ingress",
            expected_pins=job.expected_pins,
        )
        append_shadow_record(journal_path, record)
        return record
    outcome = parse_proposal(payload)
    record = record_shadow_cycle(
        bundle=bundle,
        job=job,
        recorded_at=recorded_at,
        proposal=outcome.proposal,
        ingress_accepted=outcome.accepted,
        ingress_refusal=outcome.refusal,
        expected_pins=job.expected_pins,
    )
    append_shadow_record(journal_path, record)
    return record


def journal_reference_worker(
    *,
    bundle: EvidenceBundle,
    request: WorkerRequest,
    journal_path: Path,
    recorded_at: datetime,
) -> ShadowJournalRecord:
    """Run the deterministic reference worker through ingress and journal it."""

    if request.job.bundle_digest != bundle.digest():
        raise ValueError("worker request digest does not match the issued bundle")
    return journal_worker_payload(
        bundle=bundle,
        job=request.job,
        payload=propose_payload(bundle),
        journal_path=journal_path,
        recorded_at=recorded_at,
    )
