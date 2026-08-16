"""Job / evidence / response contract for an external model worker.

Chronos does not call a model. A worker outside the broker process reads one
issued bundle and posts one ``ProposedDecision``. The worker may not author
``provenance`` or ``decision_id`` — ingress refuses those fields.

Identity pins on the job are Chronos-owned expectations, not a worker
self-report. A worker that echoes different pins has not changed who Chronos
will stamp.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import AwareDatetime, Field, field_validator, model_validator

from chronos.autonomy.base import AutonomyModel
from chronos.autonomy.evidence import EvidenceBundle, as_model_view

WORKER_REQUEST_SCHEMA = "chronos-worker-request-v1"
REFERENCE_WORKER_PROVIDER = "chronos-reference"
REFERENCE_WORKER_MODEL_ID = "pairing-allow-enter-v1"
REFERENCE_WORKER_MODEL_VERSION = "1"
REFERENCE_WORKER_PROMPT_VERSION = "1"
REFERENCE_WORKER_TOOL_SCHEMA_VERSION = "1"
REFERENCE_WORKER_DECISION_SCHEMA_VERSION = "1"
REFERENCE_WORKER_POLICY_VERSION = "pairing-shadow-v1"


class WorkerIdentityPins(AutonomyModel):
    """Pins Chronos expects to stamp after ingress. The worker does not author these."""

    provider: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    tool_schema_version: str = Field(min_length=1, max_length=64)
    decision_schema_version: str = Field(min_length=1, max_length=64)
    policy_version: str = Field(min_length=1, max_length=64)


REFERENCE_WORKER_PINS = WorkerIdentityPins(
    provider=REFERENCE_WORKER_PROVIDER,
    model_id=REFERENCE_WORKER_MODEL_ID,
    model_version=REFERENCE_WORKER_MODEL_VERSION,
    prompt_version=REFERENCE_WORKER_PROMPT_VERSION,
    tool_schema_version=REFERENCE_WORKER_TOOL_SCHEMA_VERSION,
    decision_schema_version=REFERENCE_WORKER_DECISION_SCHEMA_VERSION,
    policy_version=REFERENCE_WORKER_POLICY_VERSION,
)


class WorkerJob(AutonomyModel):
    """One bounded job: one bundle, one digest, one expiry."""

    schema_version: str = Field(min_length=1, max_length=64)
    job_id: str = Field(min_length=1, max_length=128)
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    bundle_id: str = Field(min_length=1, max_length=128)
    bundle_digest: str = Field(min_length=64, max_length=64)
    bundle_version: str = Field(min_length=1, max_length=32)
    expected_pins: WorkerIdentityPins

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: str) -> str:
        if value != WORKER_REQUEST_SCHEMA:
            raise ValueError(f"unsupported worker request schema: {value}")
        return value

    @field_validator("bundle_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("bundle_digest must be a 64-character lowercase hex digest")
        return normalized

    @model_validator(mode="after")
    def _window(self) -> WorkerJob:
        if self.expires_at <= self.issued_at:
            raise ValueError("worker job expiry must be after issued_at")
        return self


class WorkerRequest(AutonomyModel):
    """What an external worker is given: the job envelope plus a copy of the view."""

    job: WorkerJob
    evidence: dict[str, Any]

    @field_validator("evidence")
    @classmethod
    def _copy_evidence(cls, value: dict[str, Any]) -> dict[str, Any]:
        if "provenance" in value or "decision_id" in value:
            raise ValueError("a worker request must not carry writer-owned decision fields")
        return dict(value)


def build_worker_request(
    bundle: EvidenceBundle,
    *,
    job_id: str,
    issued_at: datetime,
    expires_at: datetime,
    expected_pins: WorkerIdentityPins = REFERENCE_WORKER_PINS,
) -> WorkerRequest:
    """Bind a job to the issued bundle's digest. The worker cannot choose another."""

    digest = bundle.digest()
    job = WorkerJob(
        schema_version=WORKER_REQUEST_SCHEMA,
        job_id=job_id,
        issued_at=issued_at,
        expires_at=expires_at,
        bundle_id=bundle.bundle_id,
        bundle_digest=digest,
        bundle_version=bundle.bundle_version,
        expected_pins=expected_pins,
    )
    return WorkerRequest(job=job, evidence=as_model_view(bundle))


def encode_worker_request(request: WorkerRequest) -> bytes:
    payload = request.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
