"""Brokered research attempts with canonical accounting and retained replay evidence.

This module owns the ordering boundary between three deliberately separate capabilities:

1. the canonical ADR-0013 trial registry records a durable start;
2. a manifest-bound dataset catalog opens the one authorized ordinary partition; and
3. an immutable object store retains the exact input, evaluator outputs, and replay
   envelope before the attempt may be recorded as completed.

The evaluator receives bytes, never a path or reader callback.  That makes the shipped
data access path auditable, but it is not a Python sandbox: an arbitrary third-party
evaluator could still open unrelated data on its own.  Campaign code must therefore use
reviewed evaluators, and the Five-Tool campaign remains blocked until that wiring and its
owner/data locks are resolved.

Research-plane only: no broker, order, mandate, live database, or promotion import.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from chronos.registry.ledger import CANONICAL_REGISTRY_LEDGER_PATH
from chronos.registry.runs import RunStage
from chronos.registry.trials import (
    CanonicalTrialReceipt,
    CanonicalTrialRegistry,
)
from chronos.research.certified_data import (
    CertifiedDataRequest,
    CertifiedDatasetCatalog,
)
from chronos.research.replay_store import (
    CANONICAL_REPLAY_STORE_ROOT,
    ReplayArtifact,
    ReplayEnvelope,
    ReplayObjectRef,
    ReplayObjectStore,
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_ROLE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_UNCLASSIFIED_ERROR = "UnclassifiedEvaluationError"


class BrokeredTrialError(RuntimeError):
    """A brokered attempt could not complete with durable terminal evidence."""


@dataclass(frozen=True, slots=True)
class BrokeredTrialDefinition:
    """Frozen non-data identity of one semantic research trial."""

    campaign_id: str
    campaign_manifest_sha256: str
    cell_id: str
    hypothesis_id: str
    stage: RunStage
    strategy_id: str
    config_digest: str
    code_commit: str
    criteria_digest: str
    evaluator_id: str
    evaluator_digest: str

    def __post_init__(self) -> None:
        for name in (
            "campaign_id",
            "cell_id",
            "hypothesis_id",
            "strategy_id",
            "evaluator_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        for name in (
            "campaign_manifest_sha256",
            "config_digest",
            "criteria_digest",
            "evaluator_digest",
        ):
            _require_sha256(name, getattr(self, name))
        if not isinstance(self.stage, RunStage):
            raise TypeError("stage must be RunStage")
        if not isinstance(self.code_commit, str) or _COMMIT.fullmatch(self.code_commit) is None:
            raise ValueError("code_commit must be a resolved 40-character git SHA")

    def trial_id(self, request: CertifiedDataRequest, *, catalog_sha256: str) -> str:
        """Return the stable semantic id shared by retries of this exact trial."""

        payload = {
            "campaign_id": self.campaign_id,
            "campaign_manifest_sha256": self.campaign_manifest_sha256,
            "cell_id": self.cell_id,
            "hypothesis_id": self.hypothesis_id,
            "stage": self.stage.value,
            "strategy_id": self.strategy_id,
            "config_digest": self.config_digest,
            "code_commit": self.code_commit,
            "criteria_digest": self.criteria_digest,
            "evaluator_id": self.evaluator_id,
            "evaluator_digest": self.evaluator_digest,
            "catalog_sha256": catalog_sha256,
            "dataset_id": request.dataset_id,
            "partition": request.partition,
            "data_version": request.data_version,
            "source_id": request.source_id,
            "source_receipt_sha256": request.source_receipt_sha256,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return "research-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TrialArtifactOutput:
    """One named evaluator artifact to retain byte-for-byte."""

    role: str
    content: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or _ROLE.fullmatch(self.role) is None:
            raise ValueError("artifact role must be a canonical lowercase identifier")
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("artifact content must be non-empty bytes")


@dataclass(frozen=True, slots=True)
class BrokeredTrialEvaluation:
    """Every byte emitted by a deterministic evaluator, with no unretained verdict."""

    outputs: tuple[TrialArtifactOutput, ...]

    def __post_init__(self) -> None:
        if not self.outputs:
            raise ValueError("a completed trial must retain at least one output artifact")
        if any(not isinstance(item, TrialArtifactOutput) for item in self.outputs):
            raise TypeError("outputs must contain only TrialArtifactOutput values")
        roles = [item.role for item in self.outputs]
        if len(roles) != len(set(roles)):
            raise ValueError("output artifact roles must be unique")


@dataclass(frozen=True, slots=True)
class BrokeredTrialResult:
    """Evidence identities returned after objects and a completed terminal are durable."""

    receipt: CanonicalTrialReceipt
    replay_envelope: ReplayObjectRef
    terminal_record_hash: str


class BrokeredResearchTrialRunner:
    """Run ordinary-data research attempts through the canonical evidence boundary."""

    def __init__(
        self,
        *,
        registry: CanonicalTrialRegistry,
        catalog: CertifiedDatasetCatalog,
        replay_store: ReplayObjectStore,
    ) -> None:
        if not isinstance(registry, CanonicalTrialRegistry):
            raise TypeError("registry must be CanonicalTrialRegistry")
        if not isinstance(catalog, CertifiedDatasetCatalog):
            raise TypeError("catalog must be CertifiedDatasetCatalog")
        if not isinstance(replay_store, ReplayObjectStore):
            raise TypeError("replay_store must be ReplayObjectStore")
        registry_workspace = _capability_workspace_root(
            registry.ledger_path,
            canonical_relative_path=CANONICAL_REGISTRY_LEDGER_PATH,
        )
        replay_workspace = _capability_workspace_root(
            replay_store.root,
            canonical_relative_path=CANONICAL_REPLAY_STORE_ROOT,
        )
        if registry_workspace != replay_workspace:
            raise BrokeredTrialError(
                "registry and replay store capabilities belong to different workspaces"
            )
        self._registry = registry
        self._catalog = catalog
        self._replay_store = replay_store

    def run(
        self,
        definition: BrokeredTrialDefinition,
        request: CertifiedDataRequest,
        *,
        evaluator: Callable[[bytes, CanonicalTrialReceipt], BrokeredTrialEvaluation],
    ) -> BrokeredTrialResult:
        """Start, read, evaluate, retain, and terminalize one exact attempt.

        Metadata and holdout scope are checked before a start.  The registry start is
        durable before the catalog opens the partition.  Every exception after that
        point is terminalized as failed when the registry remains writable; the durable
        start still counts even if terminalization itself is interrupted.
        """

        if not isinstance(definition, BrokeredTrialDefinition):
            raise TypeError("definition must be BrokeredTrialDefinition")
        if not isinstance(request, CertifiedDataRequest):
            raise TypeError("request must be CertifiedDataRequest")
        _require_ordinary_stage(definition)
        # Metadata-only resolution rejects undeclared/holdout requests without touching
        # dataset bytes or spending a trial.
        self._catalog.resolve_ordinary(request)
        trial_id = definition.trial_id(
            request,
            catalog_sha256=self._catalog.manifest_sha256,
        )
        receipt = self._registry.start_trial(
            trial_id=trial_id,
            campaign_id=definition.campaign_id,
            campaign_manifest_sha256=definition.campaign_manifest_sha256,
            stage=definition.stage,
            strategy_id=definition.strategy_id,
            config_hash=definition.config_digest,
            code_commit=definition.code_commit,
            data_hashes=_request_data_hashes(
                request,
                catalog_sha256=self._catalog.manifest_sha256,
            ),
            criteria_ref=definition.criteria_digest,
        )

        try:
            data = self._catalog._read_bytes_for_trial(request)
            input_ref = self._replay_store.put_bytes(data.content)
            if input_ref.sha256 != data.content_sha256:
                raise BrokeredTrialError("retained input digest disagrees with certified read")
            evaluation = evaluator(data.content, receipt)
            if not isinstance(evaluation, BrokeredTrialEvaluation):
                raise TypeError("evaluator must return BrokeredTrialEvaluation")
            output_artifacts = tuple(
                ReplayArtifact(
                    role=item.role,
                    object_ref=self._replay_store.put_bytes(item.content),
                )
                for item in evaluation.outputs
            )
            envelope = ReplayEnvelope(
                campaign_id=definition.campaign_id,
                campaign_manifest_sha256=definition.campaign_manifest_sha256,
                trial_id=receipt.trial_id,
                attempt_id=receipt.attempt_id,
                start_sequence=receipt.start_sequence,
                start_record_hash=receipt.start_record_hash,
                code_commit=definition.code_commit,
                config_digest=definition.config_digest,
                criteria_digest=definition.criteria_digest,
                data_catalog_sha256=self._catalog.manifest_sha256,
                dataset_id=request.dataset_id,
                partition=request.partition,
                data_version=request.data_version,
                evaluator_id=definition.evaluator_id,
                evaluator_digest=definition.evaluator_digest,
                inputs=(ReplayArtifact(role="dataset_partition", object_ref=input_ref),),
                outputs=output_artifacts,
            )
            envelope_ref = self._replay_store.put_envelope(envelope)
        except BaseException as error:
            try:
                self._registry.terminalize_failed(
                    receipt,
                    error_type=_bounded_error_type(error),
                )
            except Exception as terminal_error:
                raise BrokeredTrialError(
                    "trial failed and its terminal outcome could not be recorded"
                ) from terminal_error
            raise

        # Do not convert an ambiguous completion-ledger failure into a second terminal:
        # the completed record may have reached durable storage before the exception.
        terminal = self._registry._complete_with_retained_evidence(
            receipt,
            evidence_digest=envelope_ref.sha256,
        )
        return BrokeredTrialResult(
            receipt=receipt,
            replay_envelope=envelope_ref,
            terminal_record_hash=terminal.record_hash,
        )

    def load_completed_evidence(
        self,
        definition: BrokeredTrialDefinition,
        request: CertifiedDataRequest,
        *,
        attempt_id: str,
    ) -> ReplayEnvelope:
        """Restart-load one completion only after registry and envelope identities agree.

        A ``completed`` terminal record is not sufficient evidence by itself.  This
        method verifies the complete registry snapshot, exact semantic trial identity,
        authenticated catalog request, retained envelope digest, and every stored input
        and output object before returning the envelope.
        """

        if not isinstance(definition, BrokeredTrialDefinition):
            raise TypeError("definition must be BrokeredTrialDefinition")
        if not isinstance(request, CertifiedDataRequest):
            raise TypeError("request must be CertifiedDataRequest")
        _require_ordinary_stage(definition)
        metadata = self._catalog.resolve_ordinary(request)
        completed = self._registry.completed_attempt(attempt_id)
        if completed is None:
            raise BrokeredTrialError("attempt has no verified completed terminal")

        expected_trial_id = definition.trial_id(
            request,
            catalog_sha256=self._catalog.manifest_sha256,
        )
        expected_data_hashes = _request_data_hashes(
            request,
            catalog_sha256=self._catalog.manifest_sha256,
        )
        receipt = completed.receipt
        actual_start_identity = (
            receipt.trial_id,
            receipt.campaign_id,
            receipt.campaign_manifest_sha256,
            completed.stage,
            completed.strategy_id,
            completed.config_hash,
            completed.code_commit,
            dict(completed.data_hashes),
            completed.criteria_ref,
        )
        expected_start_identity = (
            expected_trial_id,
            definition.campaign_id,
            definition.campaign_manifest_sha256,
            definition.stage,
            definition.strategy_id,
            definition.config_digest,
            definition.code_commit,
            expected_data_hashes,
            definition.criteria_digest,
        )
        if actual_start_identity != expected_start_identity:
            raise BrokeredTrialError(
                "completed registry identity does not match the requested trial"
            )

        envelope = self._replay_store.load_envelope_by_sha256(completed.evidence_digest)
        actual_envelope_identity = (
            envelope.campaign_id,
            envelope.campaign_manifest_sha256,
            envelope.trial_id,
            envelope.attempt_id,
            envelope.start_sequence,
            envelope.start_record_hash,
            envelope.code_commit,
            envelope.config_digest,
            envelope.criteria_digest,
            envelope.data_catalog_sha256,
            envelope.dataset_id,
            envelope.partition,
            envelope.data_version,
            envelope.evaluator_id,
            envelope.evaluator_digest,
        )
        expected_envelope_identity = (
            definition.campaign_id,
            definition.campaign_manifest_sha256,
            expected_trial_id,
            receipt.attempt_id,
            receipt.start_sequence,
            receipt.start_record_hash,
            definition.code_commit,
            definition.config_digest,
            definition.criteria_digest,
            self._catalog.manifest_sha256,
            request.dataset_id,
            request.partition,
            request.data_version,
            definition.evaluator_id,
            definition.evaluator_digest,
        )
        if actual_envelope_identity != expected_envelope_identity:
            raise BrokeredTrialError(
                "retained envelope identity does not match the completed registry start"
            )
        if len(envelope.inputs) != 1:
            raise BrokeredTrialError("retained envelope must bind exactly one dataset input")
        retained_input = envelope.inputs[0]
        if (
            retained_input.role != "dataset_partition"
            or retained_input.object_ref.sha256 != metadata.sha256
            or retained_input.object_ref.byte_count != metadata.byte_count
        ):
            raise BrokeredTrialError(
                "retained dataset object does not match authenticated catalog metadata"
            )
        return envelope


def _require_sha256(name: str, value: object) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _bounded_error_type(error: BaseException) -> str:
    name = type(error).__name__
    return name if _ERROR_TYPE.fullmatch(name) is not None else _UNCLASSIFIED_ERROR


def _request_data_hashes(
    request: CertifiedDataRequest,
    *,
    catalog_sha256: str,
) -> dict[str, object]:
    return {
        "catalog_sha256": catalog_sha256,
        "dataset_id": request.dataset_id,
        "partition": request.partition,
        "data_version": request.data_version,
        "source_id": request.source_id,
        "source_receipt_sha256": request.source_receipt_sha256,
    }


def _require_ordinary_stage(definition: BrokeredTrialDefinition) -> None:
    if definition.stage is RunStage.HOLDOUT:
        raise BrokeredTrialError(
            "the ordinary-data trial runner cannot claim or consume a holdout stage"
        )


def _capability_workspace_root(path: Path, *, canonical_relative_path: Path) -> Path:
    suffix = canonical_relative_path.parts
    if len(path.parts) >= len(suffix) and path.parts[-len(suffix) :] == suffix:
        return path.parents[len(suffix) - 1]
    # Private test seams deliberately use sibling ledger/store paths under one temp root.
    return path.parent
