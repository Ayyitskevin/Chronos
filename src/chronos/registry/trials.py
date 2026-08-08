"""Canonical research-trial lifecycle and multiplicity (Vision plan phase 3).

The ADR-0013 ``experiment_run`` records remain supported.  This module adds the
stricter lifecycle needed by brokered research readers: a durable ``trial_started``
record is the multiplicity event, and it exists before a reader may return bytes.
Terminal records describe outcomes but never remove a start from the count, so
reader failures, evaluator failures, retries, and interrupted/orphaned attempts all
remain visible.

The public capability opens one fixed canonical path.  An arbitrary path would make
the purported global count caller-selected; only the explicitly private test seam
accepts a temporary path.  The path is frozen absolute when the capability is built,
so its construction-time working directory must be the trusted Chronos workspace root.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from chronos.auditlog.log import AuditRecord
from chronos.registry.ledger import (
    CANONICAL_REGISTRY_LEDGER_PATH,
    KIND_RUN,
    _RegistryPathCapability,
    verified_registry_records,
    verified_registry_transaction,
)
from chronos.registry.runs import RunStage

KIND_TRIAL_STARTED = "trial_started"
KIND_TRIAL_TERMINAL = "trial_terminal"
TRIAL_SCHEMA_VERSION = "chronos-registry-trial-v1"

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_ATTEMPT_PATTERN = re.compile(r"[0-9a-f]{32}")
_ERROR_TYPE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_GENESIS = "0" * 64

_START_KEYS = frozenset(
    {
        "schema_version",
        "attempt_id",
        "trial_id",
        "campaign_id",
        "campaign_manifest_sha256",
        "stage",
        "strategy_id",
        "config_hash",
        "code_commit",
        "data_hashes",
        "criteria_ref",
        "touched_data",
    }
)
_TERMINAL_KEYS = frozenset(
    {
        "schema_version",
        "attempt_id",
        "trial_id",
        "campaign_id",
        "campaign_manifest_sha256",
        "start_sequence",
        "start_record_hash",
        "outcome",
        "evidence_digest",
        "error_type",
    }
)
_LEGACY_RUN_KEYS = frozenset(
    {
        "experiment_id",
        "stage",
        "strategy_id",
        "config_hash",
        "code_commit",
        "data_hashes",
        "criteria_ref",
        "touched_data",
    }
)


class CanonicalTrialError(RuntimeError):
    """A canonical trial lifecycle operation was refused fail-closed."""


class _CanonicalTrialOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CanonicalTrialReceipt:
    """Opaque binding to one durable canonical ``trial_started`` record."""

    attempt_id: str
    trial_id: str
    campaign_id: str
    campaign_manifest_sha256: str
    start_sequence: int
    start_record_hash: str

    def __post_init__(self) -> None:
        _require_attempt_id(self.attempt_id)
        _require_nonempty("trial_id", self.trial_id)
        _require_nonempty("campaign_id", self.campaign_id)
        _require_sha256("campaign_manifest_sha256", self.campaign_manifest_sha256)
        if (
            isinstance(self.start_sequence, bool)
            or not isinstance(self.start_sequence, int)
            or self.start_sequence < 0
        ):
            raise ValueError("start_sequence must be an integer >= 0")
        _require_sha256("start_record_hash", self.start_record_hash)


@dataclass(frozen=True, slots=True)
class TrialMultiplicitySnapshot:
    """One verified global-N snapshot bound to the complete registry head."""

    count: int
    record_count: int
    head_hash: str


@dataclass(frozen=True, slots=True)
class CompletedTrialAttempt:
    """Verified completed start/terminal identity for retained-evidence replay."""

    receipt: CanonicalTrialReceipt
    stage: RunStage
    strategy_id: str
    config_hash: str
    code_commit: str
    data_hashes: Mapping[str, object]
    criteria_ref: str
    evidence_digest: str
    terminal_record_hash: str


class CanonicalTrialRegistry:
    """Capability for the one canonical ADR-0013 research registry."""

    __slots__ = ("_path_capability",)
    _path_capability: _RegistryPathCapability

    def __init__(self) -> None:
        # The construction-time working directory is a deployment trust boundary: build
        # this capability from the Chronos workspace root.  Freeze its lexical absolute
        # path without resolving symlinks, so a later chdir cannot split the registry.
        self._path_capability = _RegistryPathCapability(CANONICAL_REGISTRY_LEDGER_PATH)

    @classmethod
    def _for_tests(cls, ledger_path: Path) -> CanonicalTrialRegistry:
        """Private temp-path seam; production code must use the canonical path."""

        instance = cls.__new__(cls)
        instance._path_capability = _RegistryPathCapability(ledger_path)
        return instance

    @property
    def ledger_path(self) -> Path:
        """The fixed path owned by this capability (observable, not configurable)."""

        return self._path_capability.path

    def start_trial(
        self,
        *,
        trial_id: str,
        campaign_id: str,
        campaign_manifest_sha256: str,
        stage: RunStage,
        strategy_id: str,
        config_hash: str,
        code_commit: str,
        data_hashes: dict[str, object],
        criteria_ref: str,
        attempt_id: str | None = None,
    ) -> CanonicalTrialReceipt:
        """Durably count one attempt before any research reader may return bytes."""

        payload = _start_payload(
            attempt_id=uuid.uuid4().hex if attempt_id is None else attempt_id,
            trial_id=trial_id,
            campaign_id=campaign_id,
            campaign_manifest_sha256=campaign_manifest_sha256,
            stage=stage,
            strategy_id=strategy_id,
            config_hash=config_hash,
            code_commit=code_commit,
            data_hashes=data_hashes,
            criteria_ref=criteria_ref,
        )
        attempt = str(payload["attempt_id"])
        with verified_registry_transaction(self._path_capability) as ledger:
            records = ledger.records()
            _validate_lifecycle(records)
            if attempt in _all_registered_ids(records):
                raise CanonicalTrialError(
                    f"attempt_id {attempt!r} is already registered; retries require a new ID"
                )
            record = ledger.append(KIND_TRIAL_STARTED, payload)
        return _receipt(record)

    def terminalize_failed(
        self,
        receipt: CanonicalTrialReceipt,
        *,
        error_type: str,
    ) -> AuditRecord:
        """Public fail-closed terminal path; failed starts always remain counted."""

        return self._terminalize(
            receipt,
            outcome=_CanonicalTrialOutcome.FAILED,
            error_type=error_type,
        )

    def _complete_with_retained_evidence(
        self,
        receipt: CanonicalTrialReceipt,
        *,
        evidence_digest: str,
    ) -> AuditRecord:
        """Broker-owned completion seam; callers must first retain replay evidence."""

        return self._terminalize(
            receipt,
            outcome=_CanonicalTrialOutcome.COMPLETED,
            evidence_digest=evidence_digest,
        )

    def _terminalize(
        self,
        receipt: CanonicalTrialReceipt,
        *,
        outcome: _CanonicalTrialOutcome,
        evidence_digest: str | None = None,
        error_type: str | None = None,
    ) -> AuditRecord:
        """Append the sole terminal outcome bound to ``receipt``'s durable start."""

        if not isinstance(receipt, CanonicalTrialReceipt):
            raise TypeError("receipt must be CanonicalTrialReceipt")
        if not isinstance(outcome, _CanonicalTrialOutcome):
            raise TypeError("outcome must be the private canonical trial outcome")
        payload = _terminal_payload(
            receipt,
            outcome=outcome,
            evidence_digest=evidence_digest,
            error_type=error_type,
        )
        with verified_registry_transaction(self._path_capability) as ledger:
            records = ledger.records()
            starts, terminals = _validate_lifecycle(records)
            start = starts.get(receipt.attempt_id)
            if start is None:
                raise CanonicalTrialError("terminal receipt has no canonical durable start")
            _require_receipt_matches_start(receipt, start)
            _validate_terminal_against_start(payload, start)
            if receipt.attempt_id in terminals:
                raise CanonicalTrialError("attempt already has a terminal outcome")
            return ledger.append(KIND_TRIAL_TERMINAL, payload)

    def multiplicity_snapshot(self) -> TrialMultiplicitySnapshot:
        """Count unique v1 starts + legacy data-touching runs from one verified head."""

        records = verified_registry_records(self._path_capability)
        _validate_lifecycle(records)
        identifiers: set[str] = set()
        for record in records:
            if record.kind == KIND_TRIAL_STARTED:
                identifiers.add(_validated_start_id(record))
            elif record.kind == KIND_RUN:
                identifier, touched = _validated_legacy_run(record)
                if touched:
                    identifiers.add(identifier)
        return TrialMultiplicitySnapshot(
            count=len(identifiers),
            record_count=len(records),
            head_hash=records[-1].record_hash if records else _GENESIS,
        )

    def completed_attempt(self, attempt_id: str) -> CompletedTrialAttempt | None:
        """Return one verified completed identity, or ``None`` if not completed."""

        _require_attempt_id(attempt_id)
        records = verified_registry_records(self._path_capability)
        starts, terminals = _validate_lifecycle(records)
        start = starts.get(attempt_id)
        terminal = terminals.get(attempt_id)
        if start is None or terminal is None:
            return None
        if terminal.payload.get("outcome") != _CanonicalTrialOutcome.COMPLETED.value:
            return None
        evidence_digest = terminal.payload.get("evidence_digest")
        assert isinstance(evidence_digest, str)
        payload = start.payload
        data_hashes = payload["data_hashes"]
        assert isinstance(data_hashes, dict)
        return CompletedTrialAttempt(
            receipt=_receipt(start),
            stage=RunStage(str(payload["stage"])),
            strategy_id=str(payload["strategy_id"]),
            config_hash=str(payload["config_hash"]),
            code_commit=str(payload["code_commit"]),
            data_hashes=_freeze_mapping(data_hashes),
            criteria_ref=str(payload["criteria_ref"]),
            evidence_digest=evidence_digest,
            terminal_record_hash=terminal.record_hash,
        )


def _start_payload(
    *,
    attempt_id: str,
    trial_id: str,
    campaign_id: str,
    campaign_manifest_sha256: str,
    stage: RunStage,
    strategy_id: str,
    config_hash: str,
    code_commit: str,
    data_hashes: dict[str, object],
    criteria_ref: str,
) -> dict[str, object]:
    _require_attempt_id(attempt_id)
    _require_nonempty("trial_id", trial_id)
    _require_nonempty("campaign_id", campaign_id)
    _require_sha256("campaign_manifest_sha256", campaign_manifest_sha256)
    if not isinstance(stage, RunStage):
        raise TypeError("stage must be RunStage")
    _require_nonempty("strategy_id", strategy_id)
    _require_sha256("config_hash", config_hash)
    if _COMMIT_PATTERN.fullmatch(code_commit) is None:
        raise ValueError("code_commit must be a full lowercase 40-character Git SHA")
    frozen_hashes = _canonical_object("data_hashes", data_hashes)
    if not frozen_hashes:
        raise ValueError("data_hashes must be non-empty")
    _require_nonempty("criteria_ref", criteria_ref)
    payload: dict[str, object] = {
        "schema_version": TRIAL_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "trial_id": trial_id,
        "campaign_id": campaign_id,
        "campaign_manifest_sha256": campaign_manifest_sha256,
        "stage": stage.value,
        "strategy_id": strategy_id,
        "config_hash": config_hash,
        "code_commit": code_commit,
        "data_hashes": frozen_hashes,
        "criteria_ref": criteria_ref,
        # Conservative by design: a durable start is the multiplicity event even if
        # the reader subsequently fails or the process is interrupted.
        "touched_data": True,
    }
    _validate_start_payload(payload)
    return payload


def _terminal_payload(
    receipt: CanonicalTrialReceipt,
    *,
    outcome: _CanonicalTrialOutcome,
    evidence_digest: str | None,
    error_type: str | None,
) -> dict[str, object]:
    if outcome is _CanonicalTrialOutcome.COMPLETED:
        if not isinstance(evidence_digest, str):
            raise ValueError("completed terminal requires evidence_digest")
        _require_sha256("evidence_digest", evidence_digest)
        if error_type is not None:
            raise ValueError("completed terminal cannot carry error_type")
    else:
        if evidence_digest is not None:
            raise ValueError("failed terminal cannot carry evidence_digest")
        if not isinstance(error_type, str) or _ERROR_TYPE_PATTERN.fullmatch(error_type) is None:
            raise ValueError("failed terminal requires a bounded error_type")
    payload: dict[str, object] = {
        "schema_version": TRIAL_SCHEMA_VERSION,
        "attempt_id": receipt.attempt_id,
        "trial_id": receipt.trial_id,
        "campaign_id": receipt.campaign_id,
        "campaign_manifest_sha256": receipt.campaign_manifest_sha256,
        "start_sequence": receipt.start_sequence,
        "start_record_hash": receipt.start_record_hash,
        "outcome": outcome.value,
        "evidence_digest": evidence_digest,
        "error_type": error_type,
    }
    return payload


def _validate_lifecycle(
    records: tuple[AuditRecord, ...],
) -> tuple[dict[str, AuditRecord], dict[str, AuditRecord]]:
    starts: dict[str, AuditRecord] = {}
    terminals: dict[str, AuditRecord] = {}
    legacy_records: dict[str, AuditRecord] = {}
    for record in records:
        if record.kind == KIND_TRIAL_STARTED:
            attempt_id = _validated_start_id(record)
            if attempt_id in starts:
                raise CanonicalTrialError("registry has duplicate trial_started attempt_id")
            starts[attempt_id] = record
        elif record.kind == KIND_TRIAL_TERMINAL:
            attempt_id = _validated_terminal_id(record)
            if attempt_id in terminals:
                raise CanonicalTrialError("registry has duplicate trial_terminal attempt_id")
            terminals[attempt_id] = record
        elif record.kind == KIND_RUN:
            identifier, _ = _validated_legacy_run(record)
            if identifier in legacy_records:
                raise CanonicalTrialError(
                    "registry has duplicate legacy experiment_run experiment_id"
                )
            legacy_records[identifier] = record
    extra = sorted(set(terminals) - set(starts))
    if extra:
        raise CanonicalTrialError(f"terminal records lack canonical starts: {extra}")
    for attempt_id, terminal in terminals.items():
        start = starts[attempt_id]
        if terminal.sequence <= start.sequence:
            raise CanonicalTrialError("terminal record does not follow its durable start")
        _validate_terminal_against_start(terminal.payload, start)
    for shared_id in sorted(set(starts) & set(legacy_records)):
        _require_cross_kind_mirror(starts[shared_id], legacy_records[shared_id])
    return starts, terminals


def _require_cross_kind_mirror(start: AuditRecord, legacy: AuditRecord) -> None:
    """Allow one migration mirror only when both records describe the same touch."""

    shared_fields = (
        "stage",
        "strategy_id",
        "config_hash",
        "code_commit",
        "data_hashes",
        "criteria_ref",
        "touched_data",
    )
    start_projection = {field: start.payload.get(field) for field in shared_fields}
    legacy_projection = {field: legacy.payload.get(field) for field in shared_fields}
    try:
        start_bytes = json.dumps(
            start_projection,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        legacy_bytes = json.dumps(
            legacy_projection,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CanonicalTrialError(
            "cross-kind attempt/experiment provenance is not canonical JSON"
        ) from error
    if start_bytes != legacy_bytes:
        raise CanonicalTrialError(
            "cross-kind attempt/experiment ID collision has mismatched provenance"
        )


def _validated_start_id(record: AuditRecord) -> str:
    _validate_start_payload(record.payload)
    attempt_id = record.payload["attempt_id"]
    assert isinstance(attempt_id, str)
    return attempt_id


def _validate_start_payload(payload: dict[str, object]) -> None:
    _require_exact_keys(payload, _START_KEYS, "trial_started payload")
    if payload.get("schema_version") != TRIAL_SCHEMA_VERSION:
        raise CanonicalTrialError("trial_started schema is unsupported")
    attempt = payload.get("attempt_id")
    if not isinstance(attempt, str):
        raise CanonicalTrialError("trial_started attempt_id is invalid")
    _lifecycle_value(_require_attempt_id, attempt)
    for key in ("trial_id", "campaign_id", "strategy_id", "criteria_ref"):
        value = payload.get(key)
        if not isinstance(value, str):
            raise CanonicalTrialError(f"trial_started {key} is invalid")
        _lifecycle_value(_require_nonempty, key, value)
    for key in ("campaign_manifest_sha256", "config_hash"):
        value = payload.get(key)
        if not isinstance(value, str):
            raise CanonicalTrialError(f"trial_started {key} is invalid")
        _lifecycle_value(_require_sha256, key, value)
    commit = payload.get("code_commit")
    if not isinstance(commit, str) or _COMMIT_PATTERN.fullmatch(commit) is None:
        raise CanonicalTrialError("trial_started code_commit is invalid")
    raw_stage = payload.get("stage")
    if not isinstance(raw_stage, str):
        raise CanonicalTrialError("trial_started stage is invalid")
    try:
        RunStage(raw_stage)
    except ValueError as error:
        raise CanonicalTrialError("trial_started stage is invalid") from error
    data_hashes = payload.get("data_hashes")
    if not isinstance(data_hashes, dict) or not data_hashes:
        raise CanonicalTrialError("trial_started data_hashes must be a non-empty object")
    try:
        _canonical_object("data_hashes", data_hashes)
    except ValueError as error:
        raise CanonicalTrialError(str(error)) from error
    if payload.get("touched_data") is not True:
        raise CanonicalTrialError("trial_started must conservatively mark touched_data true")


def _validated_terminal_id(record: AuditRecord) -> str:
    payload = record.payload
    _require_exact_keys(payload, _TERMINAL_KEYS, "trial_terminal payload")
    if payload.get("schema_version") != TRIAL_SCHEMA_VERSION:
        raise CanonicalTrialError("trial_terminal schema is unsupported")
    attempt = payload.get("attempt_id")
    if not isinstance(attempt, str):
        raise CanonicalTrialError("trial_terminal attempt_id is invalid")
    _lifecycle_value(_require_attempt_id, attempt)
    return attempt


def _validate_terminal_against_start(payload: dict[str, object], start: AuditRecord) -> None:
    _require_exact_keys(payload, _TERMINAL_KEYS, "trial_terminal payload")
    if payload.get("schema_version") != TRIAL_SCHEMA_VERSION:
        raise CanonicalTrialError("trial_terminal schema is unsupported")
    for key in (
        "attempt_id",
        "trial_id",
        "campaign_id",
        "campaign_manifest_sha256",
    ):
        if payload.get(key) != start.payload.get(key):
            raise CanonicalTrialError(f"trial_terminal {key} disagrees with its start")
    sequence = payload.get("start_sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence != start.sequence:
        raise CanonicalTrialError("trial_terminal start_sequence disagrees with its start")
    if payload.get("start_record_hash") != start.record_hash:
        raise CanonicalTrialError("trial_terminal is not hash-bound to its start")
    raw_outcome = payload.get("outcome")
    if not isinstance(raw_outcome, str):
        raise CanonicalTrialError("trial_terminal outcome is unsupported")
    try:
        outcome = _CanonicalTrialOutcome(raw_outcome)
    except ValueError as error:
        raise CanonicalTrialError("trial_terminal outcome is unsupported") from error
    evidence = payload.get("evidence_digest")
    error_type = payload.get("error_type")
    if outcome is _CanonicalTrialOutcome.COMPLETED:
        if not isinstance(evidence, str):
            raise CanonicalTrialError("completed terminal requires evidence_digest")
        _lifecycle_value(_require_sha256, "evidence_digest", evidence)
        if error_type is not None:
            raise CanonicalTrialError("completed terminal cannot carry error_type")
    else:
        if evidence is not None:
            raise CanonicalTrialError("failed terminal cannot carry evidence_digest")
        if not isinstance(error_type, str) or _ERROR_TYPE_PATTERN.fullmatch(error_type) is None:
            raise CanonicalTrialError("failed terminal requires a bounded error_type")


def _require_receipt_matches_start(receipt: CanonicalTrialReceipt, start: AuditRecord) -> None:
    expected = _receipt(start)
    if receipt != expected:
        raise CanonicalTrialError("terminal receipt disagrees with its canonical start")


def _receipt(start: AuditRecord) -> CanonicalTrialReceipt:
    payload = start.payload
    _validate_start_payload(payload)
    return CanonicalTrialReceipt(
        attempt_id=str(payload["attempt_id"]),
        trial_id=str(payload["trial_id"]),
        campaign_id=str(payload["campaign_id"]),
        campaign_manifest_sha256=str(payload["campaign_manifest_sha256"]),
        start_sequence=start.sequence,
        start_record_hash=start.record_hash,
    )


def _all_registered_ids(records: tuple[AuditRecord, ...]) -> frozenset[str]:
    identifiers: set[str] = set()
    for record in records:
        if record.kind == KIND_TRIAL_STARTED:
            identifiers.add(_validated_start_id(record))
        elif record.kind == KIND_RUN:
            identifier, _ = _validated_legacy_run(record)
            identifiers.add(identifier)
    return frozenset(identifiers)


def _validated_legacy_run(record: AuditRecord) -> tuple[str, bool]:
    payload = record.payload
    _require_exact_keys(payload, _LEGACY_RUN_KEYS, "legacy experiment_run payload")
    identifier = payload.get("experiment_id")
    if not isinstance(identifier, str) or not identifier.strip():
        raise CanonicalTrialError("legacy experiment_run has no valid experiment_id")
    touched = payload.get("touched_data")
    if not isinstance(touched, bool):
        raise CanonicalTrialError("legacy experiment_run touched_data must be boolean")
    return identifier, touched


def _canonical_object(name: str, value: object) -> dict[str, object]:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be canonical JSON: {error}") from error
    if not isinstance(decoded, dict):
        raise ValueError(f"{name} must be an object")
    return decoded


def _freeze_mapping(value: dict[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _require_exact_keys(payload: dict[str, object], expected: frozenset[str], context: str) -> None:
    present = set(payload)
    if present != expected:
        raise CanonicalTrialError(
            f"{context} keys do not match schema; "
            f"missing={sorted(expected - present)}, unknown={sorted(present - expected)}"
        )


def _require_attempt_id(value: str) -> None:
    if _ATTEMPT_PATTERN.fullmatch(value) is None:
        raise ValueError("attempt_id must be canonical 32-character lowercase UUID hex")


def _require_sha256(name: str, value: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase 64-character SHA-256 digest")


def _require_nonempty(name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must be non-empty")


def _lifecycle_value(function: Callable[..., None], *args: str) -> None:
    """Translate public-construction ValueErrors into ledger-integrity failures."""

    try:
        function(*args)
    except ValueError as error:
        raise CanonicalTrialError(str(error)) from error
