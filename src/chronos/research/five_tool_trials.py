"""Fail-closed trial accounting for Five-Tool research campaigns.

This module is intentionally additive.  It does not change the legacy registry,
walk-forward, or campaign semantics.  Five-Tool data access goes through
``FiveToolTrialBroker.run``: the broker durably appends ``trial_started`` before it
invokes the supplied reader, and appends exactly one terminal outcome before a result
is returned (or the original exception is re-raised).

The ledger reuses the existing anchor-verified :class:`RegistryLedger`, but creates a
fresh instance inside every locked critical section.  ``AuditLog`` caches its head at
construction, so sharing a long-lived instance between concurrent writers would be
unsafe.  A process-wide thread lock plus the registry's OS ``flock`` protect the full
verify/append/fsync/verify transaction.

Research only: this module imports no broker, order, mandate, or live persistence code.
It offers no holdout-unlock path; declared holdout identities are refused before the
reader can run.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from chronos.auditlog.log import AuditRecord
from chronos.registry.ledger import RegistryLedger, registry_lock

KIND_TRIAL_STARTED = "trial_started"
KIND_TRIAL_TERMINAL = "trial_terminal"
TRIAL_SCHEMA_VERSION = "chronos-five-tool-trial-v1"
CAMPAIGN_MANIFEST_SCHEMA = "chronos-five-tool-campaign-v1"

_DEFAULT_HOLDOUT_PARTITIONS = frozenset(
    {"holdout", "final", "reserved", "reserved_final", "final_holdout"}
)
_SHA256_HEX_LENGTH = 64
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()

T = TypeVar("T")


class FiveToolTrialError(RuntimeError):
    """A Five-Tool trial could not be recorded or completed safely."""


class HoldoutAccessRefused(FiveToolTrialError):
    """Ordinary Five-Tool research attempted to address a declared holdout."""


class TrialOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TrialDefinition:
    """Semantic identity shared by repeated attempts of one research trial."""

    campaign_id: str
    hypothesis_id: str
    strategy_id: str
    semantic_config: Mapping[str, object] = field(compare=False, repr=False)
    code_commit: str
    criteria_digest: str
    input_contract_digest: str
    _semantic_config_json: str = field(init=False, compare=True, repr=False)

    def __post_init__(self) -> None:
        for name in ("campaign_id", "hypothesis_id", "strategy_id", "code_commit"):
            _require_nonempty(name, str(getattr(self, name)))
        if self.code_commit.casefold() == "unknown":
            raise ValueError("code_commit must be resolved before a trial starts")
        _require_sha256("criteria_digest", self.criteria_digest)
        _require_sha256("input_contract_digest", self.input_contract_digest)
        canonical = _canonical_json(self.semantic_config)
        decoded = json.loads(canonical)
        if not isinstance(decoded, dict):
            raise ValueError("semantic_config must be a JSON object")
        # Freeze the identity against later mutation of the caller-owned mapping.
        object.__setattr__(self, "_semantic_config_json", canonical)

    @property
    def semantic_config_fingerprint(self) -> str:
        return _sha256_text(self._semantic_config_json)


@dataclass(frozen=True, slots=True)
class DataAccessRequest:
    """One content-addressable non-holdout dataset partition request."""

    dataset_id: str
    partition: str
    data_version: str

    def __post_init__(self) -> None:
        _require_nonempty("dataset_id", self.dataset_id)
        _require_nonempty("partition", self.partition)
        _require_nonempty("data_version", self.data_version)


@dataclass(frozen=True, slots=True)
class TrialReceipt:
    """Identity of a durably started attempt passed to the evaluator."""

    trial_id: str
    semantic_config_fingerprint: str
    attempt_id: str
    start_sequence: int
    start_record_hash: str
    dataset_id: str
    partition: str


@dataclass(frozen=True, slots=True)
class RawScoreEvidence:
    """Candidate evidence collected before the campaign's final global N is known.

    ``candidate_label`` is operator metadata only (and excluded from equality).  Rename
    or iteration order therefore cannot change a finalized score input.
    """

    semantic_config_fingerprint: str
    evidence_digest: str
    observed_sharpe: float
    observations: int
    skew: float
    kurtosis: float
    candidate_label: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        _require_sha256("semantic_config_fingerprint", self.semantic_config_fingerprint)
        _require_sha256("evidence_digest", self.evidence_digest)
        if self.observations < 2:
            raise ValueError("observations must be >= 2")
        for name in ("observed_sharpe", "skew", "kurtosis"):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"{name} must be finite")


@dataclass(frozen=True, slots=True)
class FinalNScoreInput:
    """Order-invariant scoring input bound to one reviewed final campaign N."""

    semantic_config_fingerprint: str
    evidence_digest: str
    observed_sharpe: float
    observations: int
    skew: float
    kurtosis: float
    global_trial_count: int
    reviewed_cross_trial_variance: float


def deterministic_trial_id(
    definition: TrialDefinition,
    request: DataAccessRequest,
) -> str:
    """Stable trial identity; repeated attempts deliberately share this value."""

    identity = {
        "campaign_id": definition.campaign_id,
        "hypothesis_id": definition.hypothesis_id,
        "strategy_id": definition.strategy_id,
        "semantic_config_fingerprint": definition.semantic_config_fingerprint,
        "code_commit": definition.code_commit,
        "criteria_digest": definition.criteria_digest,
        "input_contract_digest": definition.input_contract_digest,
        "dataset_id": request.dataset_id,
        "partition": request.partition,
        "data_version": request.data_version,
    }
    return f"5t-{_sha256_text(_canonical_json(identity))}"


class FiveToolTrialBroker:
    """The ordinary, non-holdout Five-Tool data-access and trial-lifecycle API."""

    def __init__(
        self,
        ledger_path: Path,
        *,
        declared_holdout_datasets: Iterable[str] = (),
        declared_holdout_partitions: Iterable[str] = _DEFAULT_HOLDOUT_PARTITIONS,
    ) -> None:
        self._ledger_path = ledger_path
        self._holdout_datasets = frozenset(
            _normalized_identity(value) for value in declared_holdout_datasets
        )
        self._holdout_partitions = frozenset(
            _normalized_identity(value) for value in declared_holdout_partitions
        )
        if not self._holdout_partitions:
            raise ValueError("declared_holdout_partitions must be non-empty")

    @property
    def ledger_path(self) -> Path:
        return self._ledger_path

    @classmethod
    def from_campaign_manifest(
        cls,
        ledger_path: Path,
        manifest: Mapping[str, object],
    ) -> FiveToolTrialBroker:
        """Bind ordinary access directly to every holdout declared by a manifest."""

        validate_campaign_manifest(manifest)
        data = _required_mapping(manifest, "data")
        declared = data.get("declared_holdouts")
        if not isinstance(declared, list):  # already checked; keeps narrowing explicit
            raise ValueError("data.declared_holdouts must be a list")
        datasets: list[str] = []
        partitions: set[str] = set(_DEFAULT_HOLDOUT_PARTITIONS)
        for item in declared:
            if not isinstance(item, dict):
                raise ValueError("each declared holdout must be an object")
            datasets.append(_required_string(item, "dataset_id"))
            partitions.add(_required_string(item, "partition"))
        return cls(
            ledger_path,
            declared_holdout_datasets=datasets,
            declared_holdout_partitions=partitions,
        )

    def run(
        self,
        definition: TrialDefinition,
        request: DataAccessRequest,
        *,
        reader: Callable[[DataAccessRequest], bytes],
        evaluator: Callable[[bytes, TrialReceipt], T],
    ) -> T:
        """Read and evaluate one attempt with durable start-before-read ordering.

        A reader failure and an evaluator failure both append ``failed`` and both count
        because multiplicity is derived from unique starts.  A terminal-ledger failure
        is fail-closed: no result or bytes are returned as a successful trial.
        """

        self._refuse_holdout(request)
        receipt = self._start(definition, request)
        data: bytes | None = None
        try:
            data = reader(request)
            if not isinstance(data, bytes):
                raise TypeError(f"reader must return bytes, got {type(data).__name__}")
            result = evaluator(data, receipt)
        except BaseException as error:
            try:
                self._terminal(
                    receipt,
                    TrialOutcome.FAILED,
                    data_sha256=hashlib.sha256(data).hexdigest() if data is not None else None,
                    error_type=type(error).__name__,
                )
            except Exception as ledger_error:
                raise FiveToolTrialError(
                    "trial failed and its terminal failure could not be recorded"
                ) from ledger_error
            raise
        self._terminal(
            receipt,
            TrialOutcome.COMPLETED,
            data_sha256=hashlib.sha256(data).hexdigest(),
            error_type=None,
        )
        return result

    def _refuse_holdout(self, request: DataAccessRequest) -> None:
        dataset = _normalized_identity(request.dataset_id)
        partition = _normalized_identity(request.partition)
        if dataset in self._holdout_datasets:
            raise HoldoutAccessRefused(
                f"dataset {request.dataset_id!r} is a declared holdout; ordinary research "
                "has no unlock capability"
            )
        if partition in self._holdout_partitions:
            raise HoldoutAccessRefused(
                f"partition {request.partition!r} is a declared holdout; ordinary research "
                "has no unlock capability"
            )

    def _start(self, definition: TrialDefinition, request: DataAccessRequest) -> TrialReceipt:
        trial_id = deterministic_trial_id(definition, request)
        attempt_id = uuid.uuid4().hex

        def validate(ledger: RegistryLedger) -> None:
            if any(
                record.payload.get("attempt_id") == attempt_id
                for record in ledger.records_of(KIND_TRIAL_STARTED)
            ):
                raise FiveToolTrialError("attempt id collision; refusing duplicate start")

        record = _locked_append(
            self._ledger_path,
            KIND_TRIAL_STARTED,
            {
                "schema_version": TRIAL_SCHEMA_VERSION,
                "campaign_id": definition.campaign_id,
                "trial_id": trial_id,
                "attempt_id": attempt_id,
                "hypothesis_id": definition.hypothesis_id,
                "strategy_id": definition.strategy_id,
                "semantic_config_fingerprint": definition.semantic_config_fingerprint,
                "code_commit": definition.code_commit,
                "criteria_digest": definition.criteria_digest,
                "input_contract_digest": definition.input_contract_digest,
                "dataset_id": request.dataset_id,
                "partition": request.partition,
                "data_version": request.data_version,
                "touched_data": True,
            },
            validate=validate,
        )
        return TrialReceipt(
            trial_id=trial_id,
            semantic_config_fingerprint=definition.semantic_config_fingerprint,
            attempt_id=attempt_id,
            start_sequence=record.sequence,
            start_record_hash=record.record_hash,
            dataset_id=request.dataset_id,
            partition=request.partition,
        )

    def _terminal(
        self,
        receipt: TrialReceipt,
        outcome: TrialOutcome,
        *,
        data_sha256: str | None,
        error_type: str | None,
    ) -> AuditRecord:
        def validate(ledger: RegistryLedger) -> None:
            starts = [
                record
                for record in ledger.records_of(KIND_TRIAL_STARTED)
                if record.payload.get("attempt_id") == receipt.attempt_id
            ]
            if len(starts) != 1 or starts[0].record_hash != receipt.start_record_hash:
                raise FiveToolTrialError("terminal record does not match exactly one durable start")
            if any(
                record.payload.get("attempt_id") == receipt.attempt_id
                for record in ledger.records_of(KIND_TRIAL_TERMINAL)
            ):
                raise FiveToolTrialError("attempt already has a terminal outcome")

        return _locked_append(
            self._ledger_path,
            KIND_TRIAL_TERMINAL,
            {
                "schema_version": TRIAL_SCHEMA_VERSION,
                "trial_id": receipt.trial_id,
                "attempt_id": receipt.attempt_id,
                "start_sequence": receipt.start_sequence,
                "start_record_hash": receipt.start_record_hash,
                "outcome": outcome.value,
                "dataset_id": receipt.dataset_id,
                "partition": receipt.partition,
                "data_sha256": data_sha256,
                # Error messages can contain paths/data.  Record the class only.
                "error_type": error_type,
            },
            validate=validate,
        )


def global_trial_multiplicity(ledger_path: Path, *, campaign_id: str | None = None) -> int:
    """Return global multiple-testing N from unique durable start attempts only."""

    records = _verified_records(ledger_path)
    attempts: set[str] = set()
    for record in records:
        if record.kind != KIND_TRIAL_STARTED:
            continue
        if campaign_id is not None and record.payload.get("campaign_id") != campaign_id:
            continue
        attempt_id = record.payload.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise FiveToolTrialError("trial_started record has no valid attempt_id")
        attempts.add(attempt_id)
    return len(attempts)


def finalize_score_inputs(
    raw_evidence: Sequence[RawScoreEvidence],
    *,
    global_trial_count: int,
    reviewed_cross_trial_variance: float,
) -> tuple[FinalNScoreInput, ...]:
    """Bind raw evidence to one final N, sorted by semantic identity.

    This is deliberately a second phase: callers must finish all data-touching attempts,
    derive one global N from the ledger, and review one cross-trial variance before any
    candidate is scored.  Display names and caller iteration order are excluded.
    """

    if global_trial_count < 1:
        raise ValueError("global_trial_count must be >= 1")
    if not math.isfinite(reviewed_cross_trial_variance) or reviewed_cross_trial_variance < 0.0:
        raise ValueError("reviewed_cross_trial_variance must be finite and >= 0")
    by_identity: dict[str, RawScoreEvidence] = {}
    for evidence in raw_evidence:
        if evidence.semantic_config_fingerprint in by_identity:
            raise ValueError(
                "raw evidence contains duplicate semantic identity "
                f"{evidence.semantic_config_fingerprint}"
            )
        by_identity[evidence.semantic_config_fingerprint] = evidence
    return tuple(
        FinalNScoreInput(
            semantic_config_fingerprint=evidence.semantic_config_fingerprint,
            evidence_digest=evidence.evidence_digest,
            observed_sharpe=evidence.observed_sharpe,
            observations=evidence.observations,
            skew=evidence.skew,
            kurtosis=evidence.kurtosis,
            global_trial_count=global_trial_count,
            reviewed_cross_trial_variance=reviewed_cross_trial_variance,
        )
        for _, evidence in sorted(by_identity.items())
    )


def validate_campaign_manifest(manifest: Mapping[str, object]) -> None:
    """Fail-closed structural validation for the preregistered Five-Tool manifest."""

    if manifest.get("schema_version") != CAMPAIGN_MANIFEST_SCHEMA:
        raise ValueError(f"schema_version must be {CAMPAIGN_MANIFEST_SCHEMA!r}")
    if manifest.get("performance_claims") != []:
        raise ValueError("performance_claims must be an empty list")
    if manifest.get("execution_state") != "blocked_until_identity_locks_resolve":
        raise ValueError("execution_state must remain blocked while identity locks are pending")

    strategy = _required_mapping(manifest, "strategy")
    pine = _required_mapping(strategy, "pine_source")
    _require_nonempty("strategy.pine_source.path", _required_string(pine, "path"))
    _require_sha256("strategy.pine_source.sha256", _required_string(pine, "sha256"))
    _validate_artifact_lock(_required_mapping(strategy, "input_contract"), "input_contract")
    _validate_artifact_lock(_required_mapping(strategy, "semantic_config"), "semantic_config")

    data = _required_mapping(manifest, "data")
    primary = data.get("primary_instruments")
    if (
        not isinstance(primary, list)
        or len(primary) < 3
        or not all(isinstance(item, str) and item.strip() for item in primary)
    ):
        raise ValueError("data.primary_instruments must contain at least three names")
    _require_nonempty("data.benchmark", _required_string(data, "benchmark"))
    history_start = _required_string(data, "history_start_utc")
    try:
        parsed_start = datetime.fromisoformat(history_start.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("data.history_start_utc must be ISO-8601") from error
    utc_offset = parsed_start.utcoffset()
    if utc_offset is None or utc_offset.total_seconds() != 0:
        raise ValueError("data.history_start_utc must be UTC")
    declared = data.get("declared_holdouts")
    if not isinstance(declared, list) or not declared:
        raise ValueError("data.declared_holdouts must be a non-empty list")
    for index, holdout in enumerate(declared):
        if not isinstance(holdout, dict):
            raise ValueError(f"data.declared_holdouts[{index}] must be an object")
        _require_nonempty(
            f"data.declared_holdouts[{index}].dataset_id",
            _required_string(holdout, "dataset_id"),
        )
        _require_nonempty(
            f"data.declared_holdouts[{index}].partition",
            _required_string(holdout, "partition"),
        )

    _required_mapping(manifest, "fill_policy")
    costs = _required_mapping(manifest, "costs")
    for key in ("commission_bps_per_fill", "slippage_ticks_per_fill"):
        value = costs.get(key)
        if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
            raise ValueError(f"costs.{key} must be a non-negative number")

    hypotheses = manifest.get("hypothesis_ids")
    if (
        not isinstance(hypotheses, list)
        or not all(isinstance(value, str) and value.strip() for value in hypotheses)
        or len(set(hypotheses)) != 6
    ):
        raise ValueError("hypothesis_ids must contain six unique component hypotheses")
    cells = manifest.get("campaign_cells")
    if not isinstance(cells, list) or not cells:
        raise ValueError("campaign_cells must be a non-empty list")
    cell_hypotheses: set[object] = set()
    for index, cell in enumerate(cells):
        if not isinstance(cell, dict):
            raise ValueError(f"campaign_cells[{index}] must be an object")
        _require_nonempty(f"campaign_cells[{index}].cell_id", _required_string(cell, "cell_id"))
        cell_hypotheses.add(cell.get("hypothesis_id"))
    if not set(hypotheses).issubset(cell_hypotheses):
        raise ValueError("each hypothesis_id must have at least one campaign cell")

    invalidates = manifest.get("identity_changes_that_invalidate_campaign")
    required_invalidations = {
        "pine_source_sha256",
        "input_contract_sha256",
        "semantic_config_sha256",
        "dataset_version_sha256",
        "history_start_utc",
        "benchmark_identity",
        "fill_policy",
        "cost_model",
        "criteria_digest",
        "code_commit",
    }
    if not isinstance(invalidates, list) or not required_invalidations.issubset(invalidates):
        raise ValueError("identity_changes_that_invalidate_campaign is incomplete")


def _validate_artifact_lock(lock: Mapping[str, object], name: str) -> None:
    _require_nonempty(f"{name}.path", _required_string(lock, "path"))
    digest = lock.get("sha256")
    if isinstance(digest, str):
        _require_sha256(f"{name}.sha256", digest)
        if lock.get("status") != "resolved":
            raise ValueError(f"{name}.status must be 'resolved' when sha256 is pinned")
        return
    if digest is not None:
        raise ValueError(f"{name}.sha256 must be a SHA-256 string or null")
    if (
        lock.get("status") != "pending_generation"
        or lock.get("required_before_execution") is not True
    ):
        raise ValueError(
            f"{name} null digest is allowed only for a required pending-generation lock"
        )


def _locked_append(
    ledger_path: Path,
    kind: str,
    payload: dict[str, object],
    *,
    validate: Callable[[RegistryLedger], None],
) -> AuditRecord:
    thread_lock = _thread_lock_for(ledger_path)
    with thread_lock, registry_lock(ledger_path):
        # Fresh recovery under the lock is essential: AuditLog caches sequence/head.
        ledger = RegistryLedger(ledger_path)
        _require_verified(ledger)
        validate(ledger)
        record = ledger.append(kind, payload)
        _fsync_path(ledger.anchor_path)
        _fsync_directory(ledger_path.parent)
        _require_verified(ledger)
        return record


def _verified_records(ledger_path: Path) -> tuple[AuditRecord, ...]:
    thread_lock = _thread_lock_for(ledger_path)
    with thread_lock, registry_lock(ledger_path):
        ledger = RegistryLedger(ledger_path)
        _require_verified(ledger)
        return ledger.records()


def _require_verified(ledger: RegistryLedger) -> None:
    ok, detail = ledger.verify()
    if not ok:
        raise FiveToolTrialError(f"trial ledger failed verification: {detail}")


def _thread_lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve(strict=False))
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


def _fsync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError(f"value is not canonical JSON: {error}") from error


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_sha256(name: str, value: str) -> None:
    if len(value) != _SHA256_HEX_LENGTH or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be a lowercase 64-character SHA-256 digest")


def _require_nonempty(name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must be non-empty")


def _normalized_identity(value: str) -> str:
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    if not normalized:
        raise ValueError("dataset/partition identity must be non-empty")
    return normalized


def _required_mapping(parent: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _required_string(parent: Mapping[str, object], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value
