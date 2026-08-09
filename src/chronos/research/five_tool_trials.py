"""Fail-closed, ledger-local trial accounting for Five-Tool research tests.

This module is intentionally additive.  It does not change the canonical ADR-0013
registry, walk-forward, or campaign semantics.  Public v1 construction validates the
checked blocked manifest but cannot authorize data access: certified-reader readiness,
canonical global-registry integration, and replayable evidence artifacts do not exist
in this slice.  A deliberately private synthetic-test seam exercises lifecycle logic
with arbitrary reader/evaluator callbacks.  Those callbacks are not a certified reader
or an execution authority.

Inside that synthetic seam, ``FiveToolTrialBroker.run`` durably appends
``trial_started`` before invoking the supplied reader and appends one terminal outcome
before a result is returned (or the original exception is re-raised).

The ledger reuses the existing anchor-verified :class:`RegistryLedger`, but creates a
fresh instance inside every locked critical section.  ``AuditLog`` caches its head at
construction, so sharing a long-lived instance between concurrent writers would be
unsafe.  A process-wide thread lock plus the registry's OS ``flock`` protect the full
verify/append/fsync/verify transaction.

Research only: this module imports no broker, order, mandate, or live persistence code.
It offers no holdout-unlock path.  Request metadata bearing a declared holdout identity
is refused, but an arbitrary callback is not a structural holdout guardian and cannot
prove what data it touched.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TypeVar, cast

from chronos.auditlog.log import AuditRecord
from chronos.registry.ledger import RegistryLedger, registry_lock
from chronos.research.five_tool.contract import (
    default_contract_path,
    default_source_path,
    input_contract_digest,
    semantic_contract_digest,
)

KIND_TRIAL_STARTED = "trial_started"
KIND_TRIAL_TERMINAL = "trial_terminal"
KIND_CAMPAIGN_SEALED = "campaign_sealed"
TRIAL_SCHEMA_VERSION = "chronos-five-tool-trial-v2"
CAMPAIGN_MANIFEST_SCHEMA = "chronos-five-tool-campaign-v1"
EXECUTION_BLOCKED = "blocked_until_identity_locks_resolve"
EXECUTION_READY = "ready_for_certified_research"
INTERRUPTED_ATTEMPT_ERROR = "InterruptedAttemptRecovery"

_DEFAULT_HOLDOUT_PARTITIONS = frozenset(
    {"holdout", "final", "reserved", "reserved_final", "final_holdout"}
)
_SHA256_HEX_LENGTH = 64
_UNCLASSIFIED_CALLBACK_ERROR = "UnclassifiedCallbackError"
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_START_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "campaign_id",
        "campaign_manifest_sha256",
        "cell_id",
        "trial_id",
        "attempt_id",
        "hypothesis_id",
        "strategy_id",
        "semantic_config_fingerprint",
        "code_commit",
        "criteria_digest",
        "input_contract_digest",
        "dataset_id",
        "partition",
        "data_version",
        "touched_data",
    }
)
_TERMINAL_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "campaign_id",
        "campaign_manifest_sha256",
        "trial_id",
        "cell_id",
        "hypothesis_id",
        "semantic_config_fingerprint",
        "attempt_id",
        "start_sequence",
        "start_record_hash",
        "outcome",
        "dataset_id",
        "partition",
        "data_version",
        "data_sha256",
        "evidence_artifact_sha256",
        "evidence_digest",
        "observed_sharpe",
        "observations",
        "skew",
        "kurtosis",
        "error_type",
    }
)
_SEAL_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "campaign_id",
        "campaign_manifest_sha256",
        "ledger_trial_count",
        "record_count_before_seal",
        "head_before_seal",
        "score_inputs_digest",
        "reviewed_cross_trial_variance",
        "variance_estimator",
        "variance_evidence_digest",
    }
)

T = TypeVar("T")


class FiveToolTrialError(RuntimeError):
    """A Five-Tool trial could not be recorded or completed safely."""


class HoldoutAccessRefused(FiveToolTrialError):
    """Ordinary Five-Tool research attempted to address a declared holdout."""


class CampaignExecutionBlocked(FiveToolTrialError):
    """A preregistered campaign has unresolved locks and cannot touch data."""


class CampaignIdentityMismatch(FiveToolTrialError):
    """A trial definition or data request is outside its frozen campaign."""


class CampaignSealed(FiveToolTrialError):
    """The immutable ledger-local snapshot exists, so no later attempt may start."""


class DataVersionMismatch(FiveToolTrialError):
    """Reader bytes do not match the exact dataset digest authorized by the campaign."""


class TrialOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TrialDefinition:
    """Semantic identity shared by repeated attempts of one research trial."""

    campaign_id: str
    cell_id: str
    hypothesis_id: str
    strategy_id: str
    semantic_config: Mapping[str, object] = field(compare=False, repr=False)
    code_commit: str
    criteria_digest: str
    input_contract_digest: str
    _semantic_config_json: str = field(init=False, compare=True, repr=False)

    def __post_init__(self) -> None:
        for name in ("campaign_id", "cell_id", "hypothesis_id", "strategy_id", "code_commit"):
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
        _require_sha256("data_version", self.data_version)


@dataclass(frozen=True, slots=True)
class TrialReceipt:
    """Identity of a durably started attempt passed to the evaluator."""

    trial_id: str
    campaign_id: str
    campaign_manifest_sha256: str
    cell_id: str
    hypothesis_id: str
    semantic_config_fingerprint: str
    attempt_id: str
    start_sequence: int
    start_record_hash: str
    dataset_id: str
    partition: str
    data_version: str


@dataclass(frozen=True, slots=True)
class EvaluationEvidence:
    """Typed score evidence whose identity is computed, never caller asserted."""

    artifact_bytes: bytes = field(repr=False)
    observed_sharpe: float
    observations: int
    skew: float
    kurtosis: float

    def __post_init__(self) -> None:
        self._require_valid()

    def _require_valid(self) -> None:
        if not isinstance(self.artifact_bytes, bytes) or not self.artifact_bytes:
            raise ValueError("evaluation evidence artifact_bytes must be non-empty bytes")
        if (
            isinstance(self.observations, bool)
            or not isinstance(self.observations, int)
            or self.observations < 2
        ):
            raise ValueError("observations must be an integer >= 2")
        for name in ("observed_sharpe", "skew", "kurtosis"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} must be a finite number")

    @property
    def artifact_sha256(self) -> str:
        return hashlib.sha256(self.artifact_bytes).hexdigest()

    @property
    def evidence_digest(self) -> str:
        return _sha256_text(
            _canonical_json(
                {
                    "artifact_sha256": self.artifact_sha256,
                    "observed_sharpe": self.observed_sharpe,
                    "observations": self.observations,
                    "skew": self.skew,
                    "kurtosis": self.kurtosis,
                }
            )
        )


@dataclass(frozen=True, slots=True)
class TrialEvaluation[T]:
    """Evaluator return value plus typed, content-addressed score evidence."""

    value: T
    evidence: EvaluationEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, EvaluationEvidence):
            raise TypeError("TrialEvaluation.evidence must be EvaluationEvidence")
        self.evidence._require_valid()


@dataclass(frozen=True, slots=True)
class LedgerLocalScoreInput:
    """Order-invariant evidence input bound to one ledger-local attempt count.

    This is not a final multiple-testing input.  Phase 3 additionally requires the
    canonical ADR-0013 global registry, which is absent from this module.
    """

    campaign_id: str
    campaign_manifest_sha256: str
    cell_id: str
    hypothesis_id: str
    trial_id: str
    semantic_config_fingerprint: str
    attempt_id: str
    evidence_digest: str
    observed_sharpe: float
    observations: int
    skew: float
    kurtosis: float
    ledger_trial_count: int
    reviewed_cross_trial_variance: float
    variance_estimator: str
    variance_evidence_digest: str
    ledger_record_count: int
    ledger_head_hash: str


@dataclass(frozen=True, slots=True)
class ReviewedVarianceEvidence:
    """Reviewed cross-trial variance and the content-addressed estimator evidence."""

    value: float
    estimator: str
    evidence_digest: str

    def __post_init__(self) -> None:
        self._require_valid()

    def _require_valid(self) -> None:
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, int | float)
            or not math.isfinite(self.value)
            or self.value < 0.0
        ):
            raise ValueError("reviewed variance must be finite and >= 0")
        if not isinstance(self.estimator, str):
            raise ValueError("variance estimator must be a string")
        _require_nonempty("variance estimator", self.estimator)
        if not isinstance(self.evidence_digest, str):
            raise ValueError("variance evidence digest must be a string")
        _require_sha256("variance evidence digest", self.evidence_digest)


@dataclass(frozen=True, slots=True)
class _CampaignCell:
    cell_id: str
    hypothesis_id: str
    semantic_config_json: str

    @property
    def semantic_config_fingerprint(self) -> str:
        return _sha256_text(self.semantic_config_json)


@dataclass(frozen=True, slots=True)
class _CampaignBinding:
    campaign_id: str
    manifest_sha256: str
    strategy_id: str
    code_commit: str
    criteria_digest: str
    input_contract_digest: str
    dataset_id: str
    data_version: str
    accessible_partitions: frozenset[str]
    cells: tuple[_CampaignCell, ...]


def deterministic_trial_id(
    definition: TrialDefinition,
    request: DataAccessRequest,
) -> str:
    """Stable trial identity; repeated attempts deliberately share this value."""

    identity = {
        "campaign_id": definition.campaign_id,
        "cell_id": definition.cell_id,
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
    """Blocked public shell plus a private synthetic lifecycle-test harness."""

    def __init__(
        self,
        ledger_path: Path,
    ) -> None:
        self._ledger_path = ledger_path
        self._holdout_datasets: frozenset[str] = frozenset()
        self._holdout_partitions = _DEFAULT_HOLDOUT_PARTITIONS
        self._binding: _CampaignBinding | None = None
        self._synthetic_test_harness = False
        self._execution_block_reason: str | None = (
            "direct FiveToolTrialBroker construction has no certified readiness authority"
        )

    @property
    def ledger_path(self) -> Path:
        return self._ledger_path

    @classmethod
    def from_campaign_manifest(
        cls,
        ledger_path: Path,
        manifest: Mapping[str, object],
    ) -> FiveToolTrialBroker:
        """Validate a blocked manifest without granting reader authority.

        ``ready_for_certified_research`` is intentionally rejected in public v1.  A
        manifest is a claim, not the missing certified-reader/global-registry
        capability.
        """

        binding = _validate_campaign_manifest(manifest)
        if binding is not None:  # pragma: no cover - public ready validation always raises
            raise CampaignExecutionBlocked("public v1 cannot authorize an execution-ready campaign")
        broker = cls(ledger_path)
        broker._bind_declared_holdouts(manifest)
        broker._execution_block_reason = (
            "campaign manifest is blocked_until_identity_locks_resolve; "
            "no certified reader or ADR-0013 global-registry capability exists"
        )
        return broker

    @classmethod
    def _from_synthetic_manifest_for_tests(
        cls,
        ledger_path: Path,
        manifest: Mapping[str, object],
    ) -> FiveToolTrialBroker:
        """Create the private callback-driven harness used only by lifecycle tests.

        This seam deliberately bypasses the unavailable certified-readiness authority.
        It must never be used to make a research or performance claim.
        """

        binding = _validate_campaign_manifest(
            manifest,
            _allow_synthetic_ready_for_tests=True,
        )
        if binding is None:
            raise CampaignExecutionBlocked(
                "synthetic lifecycle harness requires a structurally ready test manifest"
            )
        broker = cls(ledger_path)
        broker._bind_declared_holdouts(manifest)
        broker._binding = binding
        broker._synthetic_test_harness = True
        broker._execution_block_reason = None
        return broker

    def _bind_declared_holdouts(self, manifest: Mapping[str, object]) -> None:
        """Copy declared request identities; this is not a structural guardian."""

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
        self._holdout_datasets = frozenset(_normalized_identity(value) for value in datasets)
        self._holdout_partitions = frozenset(_normalized_identity(value) for value in partitions)

    def run(
        self,
        definition: TrialDefinition,
        request: DataAccessRequest,
        *,
        reader: Callable[[DataAccessRequest], bytes],
        evaluator: Callable[[bytes, TrialReceipt], TrialEvaluation[T]],
    ) -> T:
        """Read and evaluate one attempt with durable start-before-read ordering.

        A reader failure and an evaluator failure both append ``failed`` and both count
        because multiplicity is derived from unique starts.  A terminal-ledger failure
        is fail-closed: no result or bytes are returned as a successful trial.
        """

        if self._execution_block_reason is not None:
            raise CampaignExecutionBlocked(self._execution_block_reason)
        binding = self._require_binding()
        self._validate_identity(binding, definition, request)
        self._refuse_holdout(request)
        receipt = self._start(binding, definition, request)
        data: bytes | None = None
        try:
            data = reader(request)
            if not isinstance(data, bytes):
                raise TypeError(f"reader must return bytes, got {type(data).__name__}")
            actual_data_sha256 = hashlib.sha256(data).hexdigest()
            if actual_data_sha256 != request.data_version:
                raise DataVersionMismatch(
                    "reader bytes do not match the campaign-authorized data_version"
                )
            evaluation = evaluator(data, receipt)
            if not isinstance(evaluation, TrialEvaluation):
                raise TypeError("evaluator must return TrialEvaluation")
            if not isinstance(evaluation.evidence, EvaluationEvidence):
                raise TypeError("TrialEvaluation.evidence must be EvaluationEvidence")
            evaluation.evidence._require_valid()
        except BaseException as error:
            try:
                self._terminal(
                    receipt,
                    TrialOutcome.FAILED,
                    data_sha256=(
                        hashlib.sha256(data).hexdigest() if isinstance(data, bytes) else None
                    ),
                    evidence=None,
                    error_type=_bounded_error_type(error),
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
            evidence=evaluation.evidence,
            error_type=None,
        )
        return evaluation.value

    def seal_ledger_local_score_inputs(
        self, reviewed_variance: ReviewedVarianceEvidence
    ) -> tuple[LedgerLocalScoreInput, ...]:
        """Seal synthetic evidence with an explicitly path-local ledger count."""

        if self._execution_block_reason is not None:
            raise CampaignExecutionBlocked(self._execution_block_reason)
        return _seal_and_build_ledger_local_inputs(
            self._ledger_path,
            self._require_binding(),
            reviewed_variance,
        )

    def _require_binding(self) -> _CampaignBinding:
        if self._binding is None:
            raise CampaignExecutionBlocked("campaign has no synthetic lifecycle-test binding")
        return self._binding

    @staticmethod
    def _validate_identity(
        binding: _CampaignBinding,
        definition: TrialDefinition,
        request: DataAccessRequest,
    ) -> None:
        if definition.campaign_id != binding.campaign_id:
            raise CampaignIdentityMismatch("trial campaign_id disagrees with manifest")
        if definition.strategy_id != binding.strategy_id:
            raise CampaignIdentityMismatch("trial strategy_id disagrees with manifest")
        if definition.code_commit != binding.code_commit:
            raise CampaignIdentityMismatch("trial code_commit disagrees with manifest")
        if definition.criteria_digest != binding.criteria_digest:
            raise CampaignIdentityMismatch("trial criteria_digest disagrees with manifest")
        if definition.input_contract_digest != binding.input_contract_digest:
            raise CampaignIdentityMismatch("trial input_contract_digest disagrees with manifest")
        matching_cells = [cell for cell in binding.cells if cell.cell_id == definition.cell_id]
        if len(matching_cells) != 1:
            raise CampaignIdentityMismatch("trial cell_id is not declared by the manifest")
        cell = matching_cells[0]
        if definition.hypothesis_id != cell.hypothesis_id:
            raise CampaignIdentityMismatch("trial hypothesis_id disagrees with its campaign cell")
        if definition.semantic_config_fingerprint != cell.semantic_config_fingerprint:
            raise CampaignIdentityMismatch("trial semantic config disagrees with its campaign cell")
        if request.dataset_id != binding.dataset_id:
            raise CampaignIdentityMismatch("data request dataset_id disagrees with manifest")
        if request.data_version != binding.data_version:
            raise CampaignIdentityMismatch("data request data_version disagrees with manifest")
        if _normalized_identity(request.partition) not in binding.accessible_partitions:
            raise CampaignIdentityMismatch("data request partition is not campaign-accessible")

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

    def _start(
        self,
        binding: _CampaignBinding,
        definition: TrialDefinition,
        request: DataAccessRequest,
    ) -> TrialReceipt:
        trial_id = deterministic_trial_id(definition, request)
        attempt_id = uuid.uuid4().hex

        def validate(ledger: RegistryLedger) -> None:
            if ledger.records_of(KIND_CAMPAIGN_SEALED):
                raise CampaignSealed("campaign ledger is sealed; later attempts are forbidden")
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
                "campaign_manifest_sha256": binding.manifest_sha256,
                "cell_id": definition.cell_id,
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
            campaign_id=definition.campaign_id,
            campaign_manifest_sha256=binding.manifest_sha256,
            cell_id=definition.cell_id,
            hypothesis_id=definition.hypothesis_id,
            semantic_config_fingerprint=definition.semantic_config_fingerprint,
            attempt_id=attempt_id,
            start_sequence=record.sequence,
            start_record_hash=record.record_hash,
            dataset_id=request.dataset_id,
            partition=request.partition,
            data_version=request.data_version,
        )

    def _start_for_interruption_test(
        self,
        definition: TrialDefinition,
        request: DataAccessRequest,
    ) -> TrialReceipt:
        """Durably start without reading, solely to simulate process interruption."""

        if not self._synthetic_test_harness:
            raise CampaignExecutionBlocked("interruption seam is private to lifecycle tests")
        binding = self._require_binding()
        self._validate_identity(binding, definition, request)
        self._refuse_holdout(request)
        return self._start(binding, definition, request)

    def _recover_interrupted_starts_for_tests(self) -> tuple[str, ...]:
        """Terminalize orphan starts as bounded failures in the private test harness."""

        if not self._synthetic_test_harness:
            raise CampaignExecutionBlocked("interruption recovery is private to lifecycle tests")
        return _recover_interrupted_starts(self._ledger_path, self._require_binding())

    def _terminal(
        self,
        receipt: TrialReceipt,
        outcome: TrialOutcome,
        *,
        data_sha256: str | None,
        evidence: EvaluationEvidence | None,
        error_type: str | None,
    ) -> AuditRecord:
        if outcome is TrialOutcome.COMPLETED and not isinstance(evidence, EvaluationEvidence):
            raise FiveToolTrialError("completed terminal requires typed evaluation evidence")
        if outcome is TrialOutcome.FAILED and evidence is not None:
            raise FiveToolTrialError("failed terminal cannot carry completed evaluation evidence")
        if evidence is not None:
            evidence._require_valid()

        payload = _terminal_payload(
            receipt,
            outcome,
            data_sha256=data_sha256,
            evidence=evidence,
            error_type=error_type,
        )
        binding = self._require_binding()

        def validate(ledger: RegistryLedger) -> None:
            starts = [
                record
                for record in ledger.records_of(KIND_TRIAL_STARTED)
                if record.payload.get("attempt_id") == receipt.attempt_id
            ]
            if len(starts) != 1 or starts[0].record_hash != receipt.start_record_hash:
                raise FiveToolTrialError("terminal record does not match exactly one durable start")
            _validate_start_identity(starts[0], binding)
            _validate_terminal_payload_against_start(payload, starts[0])
            if any(
                record.payload.get("attempt_id") == receipt.attempt_id
                for record in ledger.records_of(KIND_TRIAL_TERMINAL)
            ):
                raise FiveToolTrialError("attempt already has a terminal outcome")

        return _locked_append(
            self._ledger_path,
            KIND_TRIAL_TERMINAL,
            payload,
            validate=validate,
        )


def _terminal_payload(
    receipt: TrialReceipt,
    outcome: TrialOutcome,
    *,
    data_sha256: str | None,
    evidence: EvaluationEvidence | None,
    error_type: str | None,
) -> dict[str, object]:
    """Build the one exact terminal schema used by normal and recovery paths."""

    return {
        "schema_version": TRIAL_SCHEMA_VERSION,
        "campaign_id": receipt.campaign_id,
        "campaign_manifest_sha256": receipt.campaign_manifest_sha256,
        "trial_id": receipt.trial_id,
        "cell_id": receipt.cell_id,
        "hypothesis_id": receipt.hypothesis_id,
        "semantic_config_fingerprint": receipt.semantic_config_fingerprint,
        "attempt_id": receipt.attempt_id,
        "start_sequence": receipt.start_sequence,
        "start_record_hash": receipt.start_record_hash,
        "outcome": outcome.value,
        "dataset_id": receipt.dataset_id,
        "partition": receipt.partition,
        "data_version": receipt.data_version,
        "data_sha256": data_sha256,
        "evidence_artifact_sha256": evidence.artifact_sha256 if evidence is not None else None,
        "evidence_digest": evidence.evidence_digest if evidence is not None else None,
        "observed_sharpe": evidence.observed_sharpe if evidence is not None else None,
        "observations": evidence.observations if evidence is not None else None,
        "skew": evidence.skew if evidence is not None else None,
        "kurtosis": evidence.kurtosis if evidence is not None else None,
        # Error messages can contain paths/data. Record a bounded class identifier only.
        "error_type": error_type,
    }


def _bounded_error_type(error: BaseException) -> str:
    """Return a non-sensitive terminal-safe class identifier for any callback error."""

    name = type(error).__name__
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name) is not None:
        return name
    return _UNCLASSIFIED_CALLBACK_ERROR


def _receipt_from_start(start: AuditRecord) -> TrialReceipt:
    """Reconstruct a receipt only after exact start validation has succeeded."""

    payload = start.payload
    return TrialReceipt(
        trial_id=cast(str, payload["trial_id"]),
        campaign_id=cast(str, payload["campaign_id"]),
        campaign_manifest_sha256=cast(str, payload["campaign_manifest_sha256"]),
        cell_id=cast(str, payload["cell_id"]),
        hypothesis_id=cast(str, payload["hypothesis_id"]),
        semantic_config_fingerprint=cast(str, payload["semantic_config_fingerprint"]),
        attempt_id=cast(str, payload["attempt_id"]),
        start_sequence=start.sequence,
        start_record_hash=start.record_hash,
        dataset_id=cast(str, payload["dataset_id"]),
        partition=cast(str, payload["partition"]),
        data_version=cast(str, payload["data_version"]),
    )


def _recover_interrupted_starts(
    ledger_path: Path,
    binding: _CampaignBinding,
) -> tuple[str, ...]:
    """Append bounded FAILED terminals for orphan starts in the private harness."""

    thread_lock = _thread_lock_for(ledger_path)
    with thread_lock, registry_lock(ledger_path):
        ledger = RegistryLedger(ledger_path)
        _require_verified(ledger)
        records = ledger.records()
        if any(record.kind == KIND_CAMPAIGN_SEALED for record in records):
            raise CampaignSealed("sealed synthetic evidence cannot be recovered")
        starts, terminals = _validated_lifecycle_records(
            records,
            binding,
            require_all_terminals=False,
        )
        pending = tuple(
            start
            for attempt_id, start in sorted(
                starts.items(),
                key=lambda item: item[1].sequence,
            )
            if attempt_id not in terminals
        )
        recovered: list[str] = []
        for start in pending:
            receipt = _receipt_from_start(start)
            payload = _terminal_payload(
                receipt,
                TrialOutcome.FAILED,
                data_sha256=None,
                evidence=None,
                error_type=INTERRUPTED_ATTEMPT_ERROR,
            )
            _validate_terminal_payload_against_start(payload, start)
            ledger.append(KIND_TRIAL_TERMINAL, payload)
            recovered.append(receipt.attempt_id)
        if recovered:
            _fsync_path(ledger.anchor_path)
            _fsync_directory(ledger_path.parent)
            _require_verified(ledger)
            _validated_lifecycle_records(
                ledger.records(),
                binding,
                require_all_terminals=True,
            )
        return tuple(recovered)


def ledger_trial_multiplicity(ledger_path: Path) -> int:
    """Return the local ledger's count of unique durable start attempts.

    This is not the ADR-0013 global research multiplicity.
    """

    return _ledger_trial_multiplicity_from_records(_verified_records(ledger_path))


def _ledger_trial_multiplicity_from_records(records: Sequence[AuditRecord]) -> int:
    """Derive a ledger-local count from one verified immutable snapshot."""

    attempts: set[str] = set()
    for record in records:
        if record.kind != KIND_TRIAL_STARTED:
            continue
        attempt_id = record.payload.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise FiveToolTrialError("trial_started record has no valid attempt_id")
        if attempt_id in attempts:
            raise FiveToolTrialError("trial ledger contains a duplicate attempt_id")
        attempts.add(attempt_id)
    return len(attempts)


def seal_ledger_local_score_inputs(
    broker: FiveToolTrialBroker,
    *,
    reviewed_variance: ReviewedVarianceEvidence,
) -> tuple[LedgerLocalScoreInput, ...]:
    """Seal synthetic evidence; this cannot produce Phase-3 final scores."""

    return broker.seal_ledger_local_score_inputs(reviewed_variance)


def _seal_and_build_ledger_local_inputs(
    ledger_path: Path,
    binding: _CampaignBinding,
    reviewed_variance: ReviewedVarianceEvidence,
) -> tuple[LedgerLocalScoreInput, ...]:
    if not isinstance(reviewed_variance, ReviewedVarianceEvidence):
        raise TypeError("reviewed_variance must be ReviewedVarianceEvidence")
    reviewed_variance._require_valid()
    thread_lock = _thread_lock_for(ledger_path)
    with thread_lock, registry_lock(ledger_path):
        ledger = RegistryLedger(ledger_path)
        _require_verified(ledger)
        records = ledger.records()
        seals = [record for record in records if record.kind == KIND_CAMPAIGN_SEALED]
        if seals:
            if len(seals) != 1 or records[-1].record_hash != seals[0].record_hash:
                raise FiveToolTrialError("sealed campaign ledger has records after its seal")
            seal = seals[0]
            _validate_existing_seal(seal, binding, reviewed_variance)
            return _score_inputs_from_records(
                records[:-1],
                binding,
                reviewed_variance,
                seal_record=seal,
                ledger_record_count=len(records),
            )

        score_rows = _validated_completed_rows(records, binding)
        ledger_trial_count = _ledger_trial_multiplicity_from_records(records)
        if ledger_trial_count < 1:
            raise FiveToolTrialError("cannot seal a campaign without durable attempts")
        score_inputs_digest = _score_rows_digest(score_rows)
        prior_head = records[-1].record_hash if records else "0" * _SHA256_HEX_LENGTH
        seal = ledger.append(
            KIND_CAMPAIGN_SEALED,
            {
                "schema_version": TRIAL_SCHEMA_VERSION,
                "campaign_id": binding.campaign_id,
                "campaign_manifest_sha256": binding.manifest_sha256,
                "ledger_trial_count": ledger_trial_count,
                "record_count_before_seal": len(records),
                "head_before_seal": prior_head,
                "score_inputs_digest": score_inputs_digest,
                "reviewed_cross_trial_variance": reviewed_variance.value,
                "variance_estimator": reviewed_variance.estimator,
                "variance_evidence_digest": reviewed_variance.evidence_digest,
            },
        )
        _fsync_path(ledger.anchor_path)
        _fsync_directory(ledger_path.parent)
        _require_verified(ledger)
        return _score_inputs_from_rows(
            score_rows,
            binding,
            reviewed_variance,
            ledger_trial_count=ledger_trial_count,
            seal_record=seal,
            ledger_record_count=len(records) + 1,
        )


def _validate_existing_seal(
    seal: AuditRecord,
    binding: _CampaignBinding,
    reviewed_variance: ReviewedVarianceEvidence,
) -> None:
    payload = seal.payload
    _require_exact_lifecycle_keys(payload, _SEAL_PAYLOAD_KEYS, "campaign_sealed payload")
    ledger_trial_count = payload.get("ledger_trial_count")
    if (
        isinstance(ledger_trial_count, bool)
        or not isinstance(ledger_trial_count, int)
        or ledger_trial_count < 1
    ):
        raise FiveToolTrialError("campaign seal ledger_trial_count must be an integer >= 1")
    record_count_before_seal = payload.get("record_count_before_seal")
    if (
        isinstance(record_count_before_seal, bool)
        or not isinstance(record_count_before_seal, int)
        or record_count_before_seal < 1
    ):
        raise FiveToolTrialError("campaign seal record_count_before_seal must be an integer >= 1")
    variance = payload.get("reviewed_cross_trial_variance")
    if (
        isinstance(variance, bool)
        or not isinstance(variance, int | float)
        or not math.isfinite(variance)
        or variance < 0.0
    ):
        raise FiveToolTrialError(
            "campaign seal reviewed_cross_trial_variance must be finite and >= 0"
        )
    expected = {
        "campaign_id": binding.campaign_id,
        "campaign_manifest_sha256": binding.manifest_sha256,
        "reviewed_cross_trial_variance": reviewed_variance.value,
        "variance_estimator": reviewed_variance.estimator,
        "variance_evidence_digest": reviewed_variance.evidence_digest,
    }
    if payload.get("schema_version") != TRIAL_SCHEMA_VERSION:
        raise FiveToolTrialError("campaign seal schema is unsupported")
    for key, expected_value in expected.items():
        if payload.get(key) != expected_value:
            raise FiveToolTrialError(f"campaign seal disagrees with {key}")
    for key in ("head_before_seal", "score_inputs_digest", "variance_evidence_digest"):
        value = payload.get(key)
        if not isinstance(value, str):
            raise FiveToolTrialError(f"campaign seal has invalid {key}")
        _require_lifecycle_sha256(key, value)


def _validated_completed_rows(
    records: Sequence[AuditRecord], binding: _CampaignBinding
) -> tuple[tuple[AuditRecord, AuditRecord], ...]:
    starts, terminals = _validated_lifecycle_records(
        records,
        binding,
        require_all_terminals=True,
    )

    completed_by_cell: dict[str, tuple[AuditRecord, AuditRecord]] = {}
    for attempt_id, start in starts.items():
        terminal = terminals[attempt_id]
        if terminal.payload["outcome"] != TrialOutcome.COMPLETED.value:
            continue
        cell_id = cast(str, terminal.payload["cell_id"])
        prior = completed_by_cell.get(cell_id)
        if prior is not None:
            if _completed_evidence_identity(prior[1]) != _completed_evidence_identity(terminal):
                raise FiveToolTrialError(
                    f"campaign cell {cell_id!r} has divergent completed results"
                )
            # Byte/evidence-identical retries count in the ledger-local multiplicity,
            # while the earliest completed attempt is the canonical score row.
            continue
        completed_by_cell[cell_id] = (start, terminal)

    expected_cells = {cell.cell_id for cell in binding.cells}
    missing = sorted(expected_cells - set(completed_by_cell))
    unknown_cells = sorted(set(completed_by_cell) - expected_cells)
    if missing or unknown_cells:
        raise FiveToolTrialError(
            f"campaign is incomplete; missing_cells={missing}, unknown_cells={unknown_cells}"
        )
    return tuple(completed_by_cell[cell_id] for cell_id in sorted(expected_cells))


def _validated_lifecycle_records(
    records: Sequence[AuditRecord],
    binding: _CampaignBinding,
    *,
    require_all_terminals: bool,
) -> tuple[dict[str, AuditRecord], dict[str, AuditRecord]]:
    """Parse and semantically validate exact start/terminal lifecycle records."""

    allowed_kinds = {KIND_TRIAL_STARTED, KIND_TRIAL_TERMINAL}
    unknown = sorted({record.kind for record in records} - allowed_kinds)
    if unknown:
        raise FiveToolTrialError(f"campaign ledger contains unsupported records: {unknown}")
    starts: dict[str, AuditRecord] = {}
    terminals: dict[str, AuditRecord] = {}
    for record in records:
        attempt_id = record.payload.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise FiveToolTrialError("campaign record has no valid attempt_id")
        destination = starts if record.kind == KIND_TRIAL_STARTED else terminals
        if attempt_id in destination:
            raise FiveToolTrialError("campaign ledger contains duplicate lifecycle records")
        destination[attempt_id] = record
        if record.kind == KIND_TRIAL_STARTED:
            _validate_start_identity(record, binding)
    extra_terminals = sorted(set(terminals) - set(starts))
    if extra_terminals:
        raise FiveToolTrialError(f"terminal records lack durable starts: {extra_terminals}")
    for attempt_id, terminal in terminals.items():
        start = starts[attempt_id]
        if terminal.sequence <= start.sequence:
            raise FiveToolTrialError("terminal record does not follow its durable start")
        _validate_terminal_payload_against_start(terminal.payload, start)
    if require_all_terminals and set(starts) != set(terminals):
        raise FiveToolTrialError("every started attempt must reach one terminal state before seal")
    return starts, terminals


def _validate_start_identity(start: AuditRecord, binding: _CampaignBinding) -> None:
    payload = start.payload
    _require_exact_lifecycle_keys(payload, _START_PAYLOAD_KEYS, "trial_started payload")
    if payload.get("schema_version") != TRIAL_SCHEMA_VERSION:
        raise FiveToolTrialError("trial start schema is unsupported")
    expected = {
        "campaign_id": binding.campaign_id,
        "campaign_manifest_sha256": binding.manifest_sha256,
        "strategy_id": binding.strategy_id,
        "code_commit": binding.code_commit,
        "criteria_digest": binding.criteria_digest,
        "input_contract_digest": binding.input_contract_digest,
        "dataset_id": binding.dataset_id,
        "data_version": binding.data_version,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise FiveToolTrialError(f"trial start {key} disagrees with campaign binding")
    if payload.get("touched_data") is not True:
        raise FiveToolTrialError("trial start must conservatively mark touched_data true")
    attempt_id = payload.get("attempt_id")
    if not isinstance(attempt_id, str) or re.fullmatch(r"[0-9a-f]{32}", attempt_id) is None:
        raise FiveToolTrialError("trial start attempt_id is not canonical UUID hex")
    for key in (
        "trial_id",
        "campaign_id",
        "cell_id",
        "hypothesis_id",
        "strategy_id",
        "dataset_id",
        "partition",
    ):
        string_value = payload.get(key)
        if not isinstance(string_value, str) or not string_value.strip():
            raise FiveToolTrialError(f"trial start has invalid {key}")
    for key in (
        "campaign_manifest_sha256",
        "semantic_config_fingerprint",
        "criteria_digest",
        "input_contract_digest",
        "data_version",
    ):
        digest_value = payload.get(key)
        if not isinstance(digest_value, str):
            raise FiveToolTrialError(f"trial start has invalid {key}")
        _require_lifecycle_sha256(key, digest_value)
    partition = payload.get("partition")
    if not isinstance(partition, str) or _normalized_identity(partition) not in (
        binding.accessible_partitions
    ):
        raise FiveToolTrialError("trial start partition is outside campaign access")
    cell_id = payload.get("cell_id")
    cells = [cell for cell in binding.cells if cell.cell_id == cell_id]
    if len(cells) != 1:
        raise FiveToolTrialError("trial start cell is not in the campaign")
    cell = cells[0]
    if payload.get("hypothesis_id") != cell.hypothesis_id:
        raise FiveToolTrialError("trial start hypothesis disagrees with campaign cell")
    if payload.get("semantic_config_fingerprint") != cell.semantic_config_fingerprint:
        raise FiveToolTrialError("trial start semantic config disagrees with campaign cell")
    expected_trial_id = "5t-" + _sha256_text(
        _canonical_json(
            {
                "campaign_id": payload["campaign_id"],
                "cell_id": payload["cell_id"],
                "hypothesis_id": payload["hypothesis_id"],
                "strategy_id": payload["strategy_id"],
                "semantic_config_fingerprint": payload["semantic_config_fingerprint"],
                "code_commit": payload["code_commit"],
                "criteria_digest": payload["criteria_digest"],
                "input_contract_digest": payload["input_contract_digest"],
                "dataset_id": payload["dataset_id"],
                "partition": payload["partition"],
                "data_version": payload["data_version"],
            }
        )
    )
    if payload.get("trial_id") != expected_trial_id:
        raise FiveToolTrialError("trial start trial_id is inconsistent with its identity")


def _validate_terminal_payload_against_start(
    payload: Mapping[str, object],
    start: AuditRecord,
) -> None:
    """Require one exact, semantically coherent terminal state."""

    _require_exact_lifecycle_keys(payload, _TERMINAL_PAYLOAD_KEYS, "trial_terminal payload")
    if payload.get("schema_version") != TRIAL_SCHEMA_VERSION:
        raise FiveToolTrialError("trial terminal schema is unsupported")
    start_sequence = payload.get("start_sequence")
    if (
        isinstance(start_sequence, bool)
        or not isinstance(start_sequence, int)
        or start_sequence != start.sequence
    ):
        raise FiveToolTrialError("terminal start_sequence disagrees with its durable start")
    if payload.get("start_record_hash") != start.record_hash:
        raise FiveToolTrialError("terminal evidence is not bound to its durable start")
    for key in (
        "campaign_id",
        "campaign_manifest_sha256",
        "trial_id",
        "cell_id",
        "hypothesis_id",
        "semantic_config_fingerprint",
        "attempt_id",
        "dataset_id",
        "partition",
        "data_version",
    ):
        if payload.get(key) != start.payload.get(key):
            raise FiveToolTrialError(f"terminal {key} disagrees with its durable start")
    raw_outcome = payload.get("outcome")
    if not isinstance(raw_outcome, str):
        raise FiveToolTrialError("trial terminal outcome is unsupported")
    try:
        outcome = TrialOutcome(raw_outcome)
    except ValueError as error:
        raise FiveToolTrialError("trial terminal outcome is unsupported") from error
    data_sha256 = payload.get("data_sha256")
    if data_sha256 is not None:
        if not isinstance(data_sha256, str):
            raise FiveToolTrialError("trial terminal data_sha256 is invalid")
        _require_lifecycle_sha256("data_sha256", data_sha256)
    if outcome is TrialOutcome.COMPLETED:
        if data_sha256 != start.payload.get("data_version"):
            raise FiveToolTrialError("terminal bytes do not match the authorized data version")
        if payload.get("error_type") is not None:
            raise FiveToolTrialError("completed terminal cannot carry an error type")
        _validate_terminal_evidence(payload)
        return

    for key in (
        "evidence_artifact_sha256",
        "evidence_digest",
        "observed_sharpe",
        "observations",
        "skew",
        "kurtosis",
    ):
        if payload.get(key) is not None:
            raise FiveToolTrialError(f"failed terminal cannot carry {key}")
    error_type = payload.get("error_type")
    if (
        not isinstance(error_type, str)
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", error_type) is None
    ):
        raise FiveToolTrialError("failed terminal requires a bounded error type")


def _validate_terminal_evidence(payload: Mapping[str, object]) -> None:
    for key in ("evidence_artifact_sha256", "evidence_digest"):
        value = payload.get(key)
        if not isinstance(value, str):
            raise FiveToolTrialError(f"completed terminal has no typed {key}")
        _require_lifecycle_sha256(key, value)
    observations = payload.get("observations")
    if isinstance(observations, bool) or not isinstance(observations, int) or observations < 2:
        raise FiveToolTrialError("completed terminal has invalid observations")
    for key in ("observed_sharpe", "skew", "kurtosis"):
        value = payload.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            raise FiveToolTrialError(f"completed terminal has invalid {key}")
    expected_digest = _sha256_text(
        _canonical_json(
            {
                "artifact_sha256": payload["evidence_artifact_sha256"],
                "observed_sharpe": payload["observed_sharpe"],
                "observations": observations,
                "skew": payload["skew"],
                "kurtosis": payload["kurtosis"],
            }
        )
    )
    if payload.get("evidence_digest") != expected_digest:
        raise FiveToolTrialError("completed terminal evidence digest is inconsistent")


def _completed_evidence_identity(terminal: AuditRecord) -> tuple[object, ...]:
    payload = terminal.payload
    return (
        payload["data_sha256"],
        payload["evidence_artifact_sha256"],
        payload["evidence_digest"],
        payload["observed_sharpe"],
        payload["observations"],
        payload["skew"],
        payload["kurtosis"],
    )


def _score_rows_digest(rows: Sequence[tuple[AuditRecord, AuditRecord]]) -> str:
    return _sha256_text(
        _canonical_json(
            [
                {
                    "start_record_hash": start.record_hash,
                    "terminal_record_hash": terminal.record_hash,
                    "evidence_digest": terminal.payload["evidence_digest"],
                }
                for start, terminal in rows
            ]
        )
    )


def _score_inputs_from_records(
    records: Sequence[AuditRecord],
    binding: _CampaignBinding,
    reviewed_variance: ReviewedVarianceEvidence,
    *,
    seal_record: AuditRecord,
    ledger_record_count: int,
) -> tuple[LedgerLocalScoreInput, ...]:
    rows = _validated_completed_rows(records, binding)
    if seal_record.payload.get("score_inputs_digest") != _score_rows_digest(rows):
        raise FiveToolTrialError("campaign seal score-input digest is inconsistent")
    ledger_trial_count = _ledger_trial_multiplicity_from_records(records)
    if seal_record.payload.get("ledger_trial_count") != ledger_trial_count:
        raise FiveToolTrialError("campaign seal ledger-local trial count is inconsistent")
    if seal_record.payload.get("record_count_before_seal") != len(records):
        raise FiveToolTrialError("campaign seal record count is inconsistent")
    prior_head = records[-1].record_hash if records else "0" * _SHA256_HEX_LENGTH
    if seal_record.payload.get("head_before_seal") != prior_head:
        raise FiveToolTrialError("campaign seal prior head is inconsistent")
    return _score_inputs_from_rows(
        rows,
        binding,
        reviewed_variance,
        ledger_trial_count=ledger_trial_count,
        seal_record=seal_record,
        ledger_record_count=ledger_record_count,
    )


def _score_inputs_from_rows(
    rows: Sequence[tuple[AuditRecord, AuditRecord]],
    binding: _CampaignBinding,
    reviewed_variance: ReviewedVarianceEvidence,
    *,
    ledger_trial_count: int,
    seal_record: AuditRecord,
    ledger_record_count: int,
) -> tuple[LedgerLocalScoreInput, ...]:
    return tuple(
        LedgerLocalScoreInput(
            campaign_id=binding.campaign_id,
            campaign_manifest_sha256=binding.manifest_sha256,
            cell_id=str(start.payload["cell_id"]),
            hypothesis_id=str(start.payload["hypothesis_id"]),
            trial_id=str(start.payload["trial_id"]),
            semantic_config_fingerprint=str(start.payload["semantic_config_fingerprint"]),
            attempt_id=str(start.payload["attempt_id"]),
            evidence_digest=str(terminal.payload["evidence_digest"]),
            observed_sharpe=float(cast(int | float, terminal.payload["observed_sharpe"])),
            observations=cast(int, terminal.payload["observations"]),
            skew=float(cast(int | float, terminal.payload["skew"])),
            kurtosis=float(cast(int | float, terminal.payload["kurtosis"])),
            ledger_trial_count=ledger_trial_count,
            reviewed_cross_trial_variance=reviewed_variance.value,
            variance_estimator=reviewed_variance.estimator,
            variance_evidence_digest=reviewed_variance.evidence_digest,
            ledger_record_count=ledger_record_count,
            ledger_head_hash=seal_record.record_hash,
        )
        for start, terminal in rows
    )


def validate_campaign_manifest(manifest: Mapping[str, object]) -> None:
    """Validate the blocked v1 manifest; public ready authority is not implemented."""

    _validate_campaign_manifest(manifest)


def campaign_manifest_digest(manifest: Mapping[str, object]) -> str:
    """Digest a publicly valid blocked manifest, including all policies and locks."""

    _validate_campaign_manifest(manifest)
    return _sha256_text(_canonical_json(manifest))


def _validate_campaign_manifest(
    manifest: Mapping[str, object],
    *,
    _allow_synthetic_ready_for_tests: bool = False,
) -> _CampaignBinding | None:
    canonical_manifest = _canonical_json(manifest)
    _require_exact_keys(
        manifest,
        {
            "schema_version",
            "campaign_id",
            "created_at_utc",
            "purpose",
            "execution_state",
            "performance_claims",
            "promotion_authority",
            "code_commit_lock",
            "strategy",
            "data",
            "fill_policy",
            "costs",
            "hypothesis_ids",
            "campaign_cells",
            "statistics",
            "trial_accounting",
            "criteria_document",
            "criteria_lock",
            "identity_changes_that_invalidate_campaign",
            "blocked_before_first_data_read",
        },
        "campaign manifest",
    )
    if manifest.get("schema_version") != CAMPAIGN_MANIFEST_SCHEMA:
        raise ValueError(f"schema_version must be {CAMPAIGN_MANIFEST_SCHEMA!r}")
    campaign_id = _required_string(manifest, "campaign_id")
    _require_nonempty("campaign_id", campaign_id)
    _require_utc_timestamp("created_at_utc", _required_string(manifest, "created_at_utc"))
    _require_nonempty("purpose", _required_string(manifest, "purpose"))
    if manifest.get("performance_claims") != []:
        raise ValueError("performance_claims must be an empty list")
    if manifest.get("promotion_authority") != "none":
        raise ValueError("promotion_authority must remain 'none' for research-only work")
    execution_state = manifest.get("execution_state")
    if execution_state not in {EXECUTION_BLOCKED, EXECUTION_READY}:
        raise ValueError("execution_state is unsupported")
    ready = execution_state == EXECUTION_READY
    if ready and not _allow_synthetic_ready_for_tests:
        raise CampaignExecutionBlocked(
            "EXECUTION_READY is not implemented in public v1: a manifest cannot replace "
            "the missing certified reader, replay artifacts, owner evidence, and ADR-0013 "
            "global-registry capability"
        )

    strategy = _required_mapping(manifest, "strategy")
    _require_exact_keys(
        strategy,
        {
            "strategy_id",
            "version",
            "scope",
            "pine_source",
            "input_contract",
            "semantic_config",
        },
        "strategy",
    )
    strategy_id = _required_string(strategy, "strategy_id")
    if strategy_id != "five_tool_confluence_v3_6":
        raise ValueError("strategy.strategy_id must identify Five-Tool v3.6")
    if strategy.get("version") != "3.6":
        raise ValueError("strategy.version must be '3.6'")
    if strategy.get("scope") != "research-only":
        raise ValueError("strategy.scope must remain research-only")
    pine = _required_mapping(strategy, "pine_source")
    _require_exact_keys(pine, {"path", "sha256"}, "strategy.pine_source")
    pine_path = _required_string(pine, "path")
    _require_nonempty("strategy.pine_source.path", pine_path)
    pine_digest = _required_string(pine, "sha256")
    _require_sha256("strategy.pine_source.sha256", pine_digest)
    repository_root = default_contract_path().parents[1]
    expected_source_path = default_source_path().relative_to(repository_root).as_posix()
    if pine_path != expected_source_path:
        raise ValueError("strategy.pine_source.path does not identify catalog 00")
    if hashlib.sha256(default_source_path().read_bytes()).hexdigest() != pine_digest:
        raise ValueError("strategy.pine_source.sha256 does not match catalog 00 bytes")
    input_lock = _required_mapping(strategy, "input_contract")
    semantic_lock = _required_mapping(strategy, "semantic_config")
    _validate_resolved_artifact_lock(input_lock, "input_contract")
    _validate_resolved_artifact_lock(semantic_lock, "semantic_config")
    expected_contract_path = default_contract_path().relative_to(repository_root).as_posix()
    if _required_string(input_lock, "path") != expected_contract_path:
        raise ValueError("input_contract.path does not identify the frozen contract")
    if _required_string(semantic_lock, "path") != expected_contract_path:
        raise ValueError("semantic_config.path does not identify the frozen contract")
    if input_lock.get("sha256") != input_contract_digest():
        raise ValueError("input_contract.sha256 does not match the runtime contract digest")
    if semantic_lock.get("sha256") != semantic_contract_digest():
        raise ValueError("semantic_config.sha256 does not match declared semantic identity")

    code_lock = _required_mapping(manifest, "code_commit_lock")
    _require_exact_keys(
        code_lock,
        {"git_commit", "status", "required_before_execution"},
        "code_commit_lock",
    )
    if code_lock.get("required_before_execution") is not True:
        raise ValueError("code_commit_lock must be required before execution")
    commit_value = code_lock.get("git_commit")
    code_commit: str | None = None
    if commit_value is None:
        if code_lock.get("status") != "pending_resolution":
            raise ValueError("null code commit requires pending_resolution status")
    elif isinstance(commit_value, str) and re.fullmatch(r"[0-9a-f]{40}", commit_value):
        if code_lock.get("status") != "resolved":
            raise ValueError("pinned code commit requires resolved status")
        code_commit = commit_value
    else:
        raise ValueError("code_commit_lock.git_commit must be a full lowercase Git SHA or null")

    data = _required_mapping(manifest, "data")
    _require_exact_keys(
        data,
        {
            "primary_instruments",
            "benchmark",
            "timeframe",
            "history_start_utc",
            "accessible_partitions",
            "dataset_version_lock",
            "declared_holdouts",
            "known_contamination",
        },
        "data",
    )
    dataset_lock = _required_mapping(data, "dataset_version_lock")
    _require_exact_keys(
        dataset_lock,
        {"dataset_id", "sha256", "status", "required_before_execution"},
        "data.dataset_version_lock",
    )
    dataset_id = _required_string(dataset_lock, "dataset_id")
    _require_nonempty("data.dataset_version_lock.dataset_id", dataset_id)
    if dataset_lock.get("required_before_execution") is not True:
        raise ValueError("data.dataset_version_lock must be required before execution")
    dataset_digest: str | None = None
    raw_dataset_digest = dataset_lock.get("sha256")
    if raw_dataset_digest is None:
        if dataset_lock.get("status") != "pending_certified_dataset":
            raise ValueError(
                "null dataset version digest requires pending_certified_dataset status"
            )
    elif isinstance(raw_dataset_digest, str):
        _require_sha256("data.dataset_version_lock.sha256", raw_dataset_digest)
        if dataset_lock.get("status") != "resolved":
            raise ValueError("resolved dataset version digest requires resolved status")
        dataset_digest = raw_dataset_digest
    else:
        raise ValueError("data.dataset_version_lock.sha256 must be SHA-256 or null")
    primary = data.get("primary_instruments")
    if (
        not isinstance(primary, list)
        or len(primary) < 3
        or not all(isinstance(item, str) and item.strip() for item in primary)
    ):
        raise ValueError("data.primary_instruments must contain at least three names")
    instrument_names = [str(item).strip() for item in primary]
    if any(item != item.upper() for item in instrument_names) or len(set(instrument_names)) != len(
        instrument_names
    ):
        raise ValueError("data.primary_instruments must be unique canonical symbols")
    benchmark = _required_string(data, "benchmark")
    if benchmark not in instrument_names:
        raise ValueError("data.benchmark must be one of the primary instruments")
    _require_nonempty("data.timeframe", _required_string(data, "timeframe"))
    _require_utc_timestamp("data.history_start_utc", _required_string(data, "history_start_utc"))
    accessible = data.get("accessible_partitions")
    if (
        not isinstance(accessible, list)
        or not accessible
        or not all(isinstance(item, str) and item.strip() for item in accessible)
    ):
        raise ValueError("data.accessible_partitions must be a non-empty string list")
    normalized_accessible = [_normalized_identity(str(item)) for item in accessible]
    if len(set(normalized_accessible)) != len(normalized_accessible):
        raise ValueError("data.accessible_partitions must be unique")
    if set(normalized_accessible) & _DEFAULT_HOLDOUT_PARTITIONS:
        raise ValueError("holdout partitions cannot be ordinarily accessible")
    declared = data.get("declared_holdouts")
    if not isinstance(declared, list) or not declared:
        raise ValueError("data.declared_holdouts must be a non-empty list")
    for index, holdout in enumerate(declared):
        if not isinstance(holdout, dict):
            raise ValueError(f"data.declared_holdouts[{index}] must be an object")
        _require_exact_keys(
            holdout,
            {"dataset_id", "partition", "start_utc", "status", "ordinary_research_access"},
            f"data.declared_holdouts[{index}]",
        )
        _require_nonempty(
            f"data.declared_holdouts[{index}].dataset_id",
            _required_string(holdout, "dataset_id"),
        )
        _require_nonempty(
            f"data.declared_holdouts[{index}].partition",
            _required_string(holdout, "partition"),
        )
        _require_utc_timestamp(
            f"data.declared_holdouts[{index}].start_utc",
            _required_string(holdout, "start_utc"),
        )
        if holdout.get("ordinary_research_access") != "forbidden":
            raise ValueError("declared holdouts must forbid ordinary research access")
        _require_nonempty(
            f"data.declared_holdouts[{index}].status", _required_string(holdout, "status")
        )
    holdout_keys = [
        (
            _normalized_identity(_required_string(item, "dataset_id")),
            _normalized_identity(_required_string(item, "partition")),
        )
        for item in declared
        if isinstance(item, dict)
    ]
    if len(set(holdout_keys)) != len(holdout_keys):
        raise ValueError("data.declared_holdouts must be unique")
    contamination = data.get("known_contamination")
    if (
        not isinstance(contamination, list)
        or not contamination
        or not all(isinstance(item, str) and item.strip() for item in contamination)
    ):
        raise ValueError("data.known_contamination must be a non-empty string list")

    blockers = manifest.get("blocked_before_first_data_read")
    if not isinstance(blockers, list) or not all(
        isinstance(item, str) and item.strip() for item in blockers
    ):
        raise ValueError("blocked_before_first_data_read must be a string list")
    if ready and blockers:
        raise ValueError("execution-ready manifest cannot retain unresolved blockers")
    if not ready and not blockers:
        raise ValueError("blocked manifest must enumerate blockers before first data read")

    fill_policy = _required_mapping(manifest, "fill_policy")
    if not fill_policy or not all(
        isinstance(value, str) and value.strip() for value in fill_policy.values()
    ):
        raise ValueError("fill_policy must be non-empty and fully specified")
    costs = _required_mapping(manifest, "costs")
    _require_exact_keys(
        costs,
        {
            "commission_bps_per_fill",
            "slippage_ticks_per_fill",
            "spread_policy",
            "funding_borrow_model_data_costs",
            "stress",
        },
        "costs",
    )
    for key in ("commission_bps_per_fill", "slippage_ticks_per_fill"):
        value = costs.get(key)
        _require_finite_nonnegative(f"costs.{key}", value)
    _require_nonempty("costs.spread_policy", _required_string(costs, "spread_policy"))
    _require_nonempty(
        "costs.funding_borrow_model_data_costs",
        _required_string(costs, "funding_borrow_model_data_costs"),
    )
    stress = _required_mapping(costs, "stress")
    _require_exact_keys(
        stress,
        {"commission_bps_per_fill", "slippage_ticks_per_fill", "require_positive_after_stress"},
        "costs.stress",
    )
    for key in ("commission_bps_per_fill", "slippage_ticks_per_fill"):
        _require_finite_nonnegative(f"costs.stress.{key}", stress.get(key))
    if stress.get("require_positive_after_stress") is not True:
        raise ValueError("costs.stress.require_positive_after_stress must be true")

    hypotheses = manifest.get("hypothesis_ids")
    if (
        not isinstance(hypotheses, list)
        or not all(isinstance(value, str) and value.strip() for value in hypotheses)
        or len(hypotheses) != 6
        or len(set(hypotheses)) != 6
    ):
        raise ValueError("hypothesis_ids must contain six unique component hypotheses")
    cells = manifest.get("campaign_cells")
    if not isinstance(cells, list) or not cells:
        raise ValueError("campaign_cells must be a non-empty list")
    cell_hypotheses: set[str] = set()
    cell_ids: set[str] = set()
    parsed_cells: list[_CampaignCell] = []
    for index, cell in enumerate(cells):
        if not isinstance(cell, dict):
            raise ValueError(f"campaign_cells[{index}] must be an object")
        _require_exact_keys(
            cell,
            {"cell_id", "hypothesis_id", "role", "config_overlay"},
            f"campaign_cells[{index}]",
        )
        cell_id = _required_string(cell, "cell_id")
        _require_nonempty(f"campaign_cells[{index}].cell_id", cell_id)
        if cell_id in cell_ids:
            raise ValueError("campaign cell IDs must be unique")
        cell_ids.add(cell_id)
        hypothesis_id = _required_string(cell, "hypothesis_id")
        if hypothesis_id not in hypotheses:
            raise ValueError("campaign cell references an undeclared hypothesis")
        cell_hypotheses.add(hypothesis_id)
        _require_nonempty(f"campaign_cells[{index}].role", _required_string(cell, "role"))
        overlay = _required_mapping(cell, "config_overlay")
        parsed_cells.append(
            _CampaignCell(
                cell_id=cell_id,
                hypothesis_id=hypothesis_id,
                semantic_config_json=_canonical_json(overlay),
            )
        )
    if not set(hypotheses).issubset(cell_hypotheses):
        raise ValueError("each hypothesis_id must have at least one campaign cell")

    statistics = _required_mapping(manifest, "statistics")
    if not statistics:
        raise ValueError("statistics policy must be non-empty")
    trial_accounting = _required_mapping(manifest, "trial_accounting")
    if not trial_accounting:
        raise ValueError("trial_accounting policy must be non-empty")
    required_accounting = {
        "record_kind_start": KIND_TRIAL_STARTED,
        "start_must_precede_reader": True,
        "reader_and_evaluator_failures_count": True,
        "candidate_order_or_display_rename_changes_verdict": False,
    }
    for key, expected in required_accounting.items():
        if trial_accounting.get(key) != expected:
            raise ValueError(f"trial_accounting.{key} does not preserve the control")
    multiplicity_policy = trial_accounting.get("multiplicity")
    if not isinstance(multiplicity_policy, str) or not multiplicity_policy.strip():
        raise ValueError("trial_accounting.multiplicity must be non-empty")

    criteria_document = _required_string(manifest, "criteria_document")
    criteria_lock = _required_mapping(manifest, "criteria_lock")
    _require_exact_keys(
        criteria_lock,
        {"path", "sha256", "status", "required_before_execution"},
        "criteria_lock",
    )
    if criteria_lock.get("path") != criteria_document:
        raise ValueError("criteria_lock.path must match criteria_document")
    if criteria_lock.get("required_before_execution") is not True:
        raise ValueError("criteria_lock must be required before execution")
    criteria_digest: str | None = None
    raw_criteria_digest = criteria_lock.get("sha256")
    repository_root = default_contract_path().parents[1]
    criteria_path = repository_root / criteria_document
    if raw_criteria_digest is None:
        if criteria_lock.get("status") != "pending_resolution":
            raise ValueError("null criteria digest requires pending_resolution status")
    elif isinstance(raw_criteria_digest, str):
        _require_sha256("criteria_lock.sha256", raw_criteria_digest)
        if criteria_lock.get("status") != "resolved":
            raise ValueError("pinned criteria digest requires resolved status")
        if hashlib.sha256(criteria_path.read_bytes()).hexdigest() != raw_criteria_digest:
            raise ValueError("criteria_lock.sha256 does not match criteria document bytes")
        criteria_digest = raw_criteria_digest
    else:
        raise ValueError("criteria_lock.sha256 must be SHA-256 or null")

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
    if (
        not isinstance(invalidates, list)
        or len(invalidates) != len(set(invalidates))
        or set(invalidates) != required_invalidations
    ):
        raise ValueError("identity_changes_that_invalidate_campaign must be exact and complete")

    if ready and (code_commit is None or criteria_digest is None or dataset_digest is None):
        raise ValueError("execution-ready manifest requires every identity lock to be resolved")
    if not ready:
        return None
    assert code_commit is not None and criteria_digest is not None and dataset_digest is not None
    return _CampaignBinding(
        campaign_id=campaign_id,
        manifest_sha256=_sha256_text(canonical_manifest),
        strategy_id=strategy_id,
        code_commit=code_commit,
        criteria_digest=criteria_digest,
        input_contract_digest=input_contract_digest(),
        dataset_id=dataset_id,
        data_version=dataset_digest,
        accessible_partitions=frozenset(normalized_accessible),
        cells=tuple(parsed_cells),
    )


def _validate_resolved_artifact_lock(lock: Mapping[str, object], name: str) -> None:
    _require_exact_keys(
        lock,
        {"path", "sha256", "digest_scope", "status", "required_before_execution"},
        name,
    )
    _require_nonempty(f"{name}.path", _required_string(lock, "path"))
    _require_nonempty(f"{name}.digest_scope", _required_string(lock, "digest_scope"))
    if lock.get("required_before_execution") is not True:
        raise ValueError(f"{name} must be required before execution")
    digest = lock.get("sha256")
    if not isinstance(digest, str):
        raise ValueError(f"{name}.sha256 must be resolved")
    _require_sha256(f"{name}.sha256", digest)
    if lock.get("status") != "resolved":
        raise ValueError(f"{name}.status must be 'resolved'")


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


def _require_exact_lifecycle_keys(
    mapping: Mapping[str, object],
    expected: frozenset[str],
    context: str,
) -> None:
    present = set(mapping)
    if present != expected:
        raise FiveToolTrialError(
            f"{context} keys do not match schema; "
            f"missing={sorted(expected - present)}, unknown={sorted(present - expected)}"
        )


def _require_lifecycle_sha256(name: str, value: str) -> None:
    try:
        _require_sha256(name, value)
    except ValueError as error:
        raise FiveToolTrialError(str(error)) from error


def _require_exact_keys(mapping: Mapping[str, object], expected: set[str], context: str) -> None:
    present = set(mapping)
    if present != expected:
        raise ValueError(
            f"{context} keys do not match schema; "
            f"missing={sorted(expected - present)}, unknown={sorted(present - expected)}"
        )


def _require_utc_timestamp(name: str, value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be ISO-8601") from error
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{name} must be UTC")


def _require_finite_nonnegative(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
        or value < 0
    ):
        raise ValueError(f"{name} must be a finite non-negative number")


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
