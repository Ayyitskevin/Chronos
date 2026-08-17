"""Fail-closed trial accounting for Five-Tool research tests, bound to the ADR-0013 registry.

This module is intentionally additive.  It does not change the canonical ADR-0013
registry, walk-forward, or campaign semantics.  Public v2 construction validates the
checked blocked manifest but cannot authorize data access: certified-reader readiness,
canonical global-registry integration, and replayable evidence artifacts exist as
separate infrastructure but are not authorized or wired to this campaign. A deliberately
private synthetic-test seam exercises lifecycle logic with arbitrary reader/evaluator
callbacks. Those callbacks are not a certified reader or an execution authority.

**Certified provenance (the reader-honesty residual).** ``certified_reader`` in a
registered run's ``data_hashes`` used to be ``False`` by construction.  It is now a real
discriminator: only :class:`chronos.research.five_tool.certified_reader.CertifiedDatasetReader`
— the exact type, never a subclass — earns ``True``, and only after a two-phase proof.
Before registration the reader attests, from its certification manifest alone, the
dataset digest, the manifest digest, and every file digest it is locked to; the campaign
dataset lock and the certified partition list are checked there, so a mismatched reader
refuses before the attempt becomes a trial.  After the read, the receipt of what was
actually opened is compared against that attestation and against the bytes handed over,
and any disagreement fails the trial closed.  An arbitrary callback, a subclass, a
wrapper, or any mixed certified/uncertified access records ``False`` — the claim is never
inferred from good behaviour.

**Canonical ADR-0013 registration (the trial-count-completeness residual).** ADR-0013
§5 derives the multiple-testing N from *registered* runs, and its §11 discloses that the
derivation is only as complete as the set of paths that actually register.  Every
Five-Tool attempt therefore writes an ``experiment_run`` record into the canonical
hash-chained registry (``chronos.registry.runs.register_run``) **before** the durable
local start, which is itself before any read.  No registry — absent, unwired,
unreadable, or chain/anchor-broken — means no trial: ``CanonicalRegistryUnavailable`` is
raised and the reader is never called.  The registry is verified before it is trusted,
the way the holdout guardian verifies before appending.

Ordering is deliberately conservative.  If the local durable start fails *after* the
canonical record lands (a sealed campaign, an attempt-id collision, an I/O fault), the
registry keeps a record for an attempt that never read data.  That over-counts N, which
raises the multiple-testing bar; under-counting is the failure this integration exists
to prevent.  This is the opposite choice from ``research/walkforward.py``, which
registers *last* so a cell that raises mid-statistics registers nothing.  That path is
handed an already-read series, so it cannot register before the read at all; here the
read happens under this module's control, so it can — and a trial that opened data and
then died is exactly the trial an honest N must keep.  Neither ordering is edited by
this module; the divergence is recorded rather than averaged away.

**Replay artifacts (the reproducibility residual).**  The ledger has always recorded
``evidence_artifact_sha256`` and thrown the bytes away, so a completed trial could be
counted, sealed, and cited while being impossible to re-execute.  Every attempt that gets
as far as opening data now persists a content-addressed replay artifact
(:mod:`chronos.research.five_tool.replay`) holding the campaign identity, the engine
identity *including the semantic configuration itself*, the input locks and the digest of
the bytes read, the certified attestation and receipt digests, the durable start, and the
outcome — for a completed attempt the evidence bytes themselves.  The terminal record names
that artifact's digest, and the artifact is written **before** the terminal, so a completed
trial that cannot be replayed cannot exist.  Sealing re-verifies every scored row against
the store in both directions: the named artifact must be present, hash to its own content
address, and describe the attempt the ledger says it describes.  :func:`replay_trial`
re-executes from an artifact and refuses on any byte divergence, naming which axis moved.

Inside the synthetic seam, ``FiveToolTrialBroker.run`` durably appends ``trial_started``
before invoking the supplied reader and appends one terminal outcome before a result is
returned (or the original exception is re-raised).

Both ledgers reuse the anchor-verified :class:`RegistryLedger`, but a fresh instance is
created inside every locked critical section.  ``AuditLog`` caches its head at
construction, so sharing a long-lived instance between concurrent writers would be
unsafe.  A process-wide thread lock plus the registry's OS ``flock`` protect each full
verify/append/fsync/verify transaction.  The canonical-registry transaction completes
and releases before the trial-ledger transaction begins, except at seal time where the
trial-ledger lock is held while the registry is counted; the nesting order is only ever
trial-ledger → registry, so the two locks cannot deadlock.

Research only: this module imports no broker, order, mandate, or live persistence code.
It imports the registry ledger, the run recorder, the certified reader, and the replay
store, and **not** the holdout guardian, so it carries no unlock capability.  Request
metadata bearing a declared holdout identity is refused before any attestation, and the
certified reader independently refuses holdout vocabulary — neither is a structural
holdout guardian, and neither can unmask anything.
"""

from __future__ import annotations

import base64
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

from chronos.auditlog.log import AuditLogCorruptionError, AuditRecord
from chronos.registry.ledger import RegistryLedger, registry_lock
from chronos.registry.runs import RunStage, register_run, trial_count
from chronos.research.five_tool.campaign import (
    ABLATION_POLICY_SCHEMA_VERSION,
    CAMPAIGN_ID,
    CAMPAIGN_SCHEMA_VERSION,
    EVALUATOR_BINDING_SCHEMA_VERSION,
    EXECUTION_BINDINGS_SCHEMA_VERSION,
    SUPERSEDES_CAMPAIGN_ID,
    CampaignBlockerCode,
    _compile_campaign_manifest_for_tests,
    compile_campaign_manifest,
)
from chronos.research.five_tool.certified_reader import CertifiedDatasetReader
from chronos.research.five_tool.contract import (
    default_contract_path,
    default_source_path,
    input_contract_digest,
    semantic_contract_digest,
)
from chronos.research.five_tool.planning import FillPolicy
from chronos.research.five_tool.replay import (
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    REPLAY_ARTIFACT_SCHEMA_VERSION,
    FiveToolReplayPolicy,
    ReplayArtifact,
    ReplayDivergence,
    TerminalPositionPolicy,
    compare_replay_bodies,
    load_replay_artifact,
    require_artifact_root,
    validate_artifact_body,
    write_replay_artifact,
)

KIND_TRIAL_STARTED = "trial_started"
KIND_TRIAL_TERMINAL = "trial_terminal"
KIND_CAMPAIGN_SEALED = "campaign_sealed"
TRIAL_SCHEMA_VERSION = "chronos-five-tool-trial-v2"
CAMPAIGN_MANIFEST_SCHEMA = CAMPAIGN_SCHEMA_VERSION
EXECUTION_BLOCKED = "blocked_until_identity_locks_resolve"
EXECUTION_READY = "ready_for_certified_research"
INTERRUPTED_ATTEMPT_ERROR = "InterruptedAttemptRecovery"

# The capabilities a manifest still cannot substitute for.  Three names have already left
# this tuple, each because a capability landed rather than because the sentence was
# edited: the canonical ADR-0013 registry (every attempt registers before it reads —
# tests/safety/test_five_tool_registry_exercised.py), the certified reader (a read is
# proven rather than promised — tests/safety/test_five_tool_certified_reader_exercised.py),
# and replay artifacts (a completed attempt persists everything needed to re-execute it
# byte-identically — tests/safety/test_five_tool_replay_exercised.py).  The one remaining
# name is an absence a named test independently observes, so this list cannot quietly go
# stale — and the last name is the one no engineering session can remove, because only the
# owner can freeze risk limits, a power calculation, and benchmark economics.
MISSING_CERTIFIED_RESEARCH_CAPABILITIES: tuple[str, ...] = ("owner evidence",)

_DEFAULT_HOLDOUT_PARTITIONS = frozenset(
    {"holdout", "final", "reserved", "reserved_final", "final_holdout"}
)
# Campaign partitions mapped onto ADR-0013's research stages.  ``RunStage.HOLDOUT`` is
# deliberately unreachable from here: a holdout partition is refused before any
# registration happens, and unmasking one is the holdout guardian's job, not this
# module's.  An unmapped partition fails closed rather than being registered as a guess.
_CANONICAL_STAGE_BY_PARTITION: dict[str, RunStage] = {
    "development": RunStage.DEV,
    "dev": RunStage.DEV,
    "validation": RunStage.VALIDATION,
}
# The harness-local canonical registry filename; production wiring passes a real path.
_DEFAULT_REGISTRY_LEDGER_NAME = "registry.jsonl"
# The harness-local replay-artifact store; production wiring passes a real path.
_DEFAULT_ARTIFACT_DIR_NAME = "replay_artifacts"
_SHA256_HEX_LENGTH = 64
_UNCLASSIFIED_CALLBACK_ERROR = "UnclassifiedCallbackError"
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_CERTIFIED_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,255}")
_REFERENCE_ARM_ID = "5t-full-default-reference"
_EXPECTED_CELL_HYPOTHESES = {
    "5t-trend-directional-paired": "H-5T-001-TREND",
    "5t-momentum-score-paired": "H-5T-002-MOMENTUM",
    "5t-vol-scaling-paired": "H-5T-003-VOL-SCALING",
    "5t-rsi-divergence-paired": "H-5T-004-DIVERGENCE",
    "5t-mfi-divergence-paired": "H-5T-004-DIVERGENCE",
    "5t-relative-strength-paired": "H-5T-005-RELATIVE-STRENGTH",
    "5t-regime-filter-paired": "H-5T-006-REGIME-FILTER",
}
_CELL_RESOLUTION_BLOCKER_CODES = frozenset(
    {
        "unresolved_cell_config",
        "missing_comparison_arm",
        "missing_neighbor_axis",
        "unrepresentable_ablation",
        "unbound_shared_signal_stream",
        "unbound_pseudo_event_stream",
    }
)
_EXECUTION_RESOLUTION_BLOCKER_CODES = frozenset(
    {
        "certified_catalog_pending",
        "partition_stage_bindings_pending",
        "evaluator_pending",
        "identity_unresolved",
    }
)
_EXPECTED_CELL_BLOCKER_CODES = {
    "5t-trend-directional-paired": frozenset({"unrepresentable_ablation", "missing_neighbor_axis"}),
    "5t-momentum-score-paired": frozenset({"unrepresentable_ablation", "missing_neighbor_axis"}),
    "5t-vol-scaling-paired": frozenset(
        {
            "unrepresentable_ablation",
            "missing_neighbor_axis",
            "unbound_shared_signal_stream",
        }
    ),
    "5t-rsi-divergence-paired": frozenset(
        {
            "unrepresentable_ablation",
            "missing_neighbor_axis",
            "unbound_pseudo_event_stream",
        }
    ),
    "5t-mfi-divergence-paired": frozenset(
        {
            "unrepresentable_ablation",
            "missing_neighbor_axis",
            "unbound_pseudo_event_stream",
        }
    ),
    "5t-relative-strength-paired": frozenset({"unrepresentable_ablation", "missing_neighbor_axis"}),
    "5t-regime-filter-paired": frozenset(
        {
            "unrepresentable_ablation",
            "missing_neighbor_axis",
            "unbound_shared_signal_stream",
        }
    ),
}
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
        "replay_artifact_sha256",
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


class CanonicalRegistryUnavailable(FiveToolTrialError):
    """The canonical ADR-0013 registry cannot be trusted, so no trial may be counted."""


class DataVersionMismatch(FiveToolTrialError):
    """Reader bytes do not match the exact partition digest authorized by the campaign."""


class ReplayArtifactBindingBroken(FiveToolTrialError):
    """A sealed-in trial record and the artifact it names do not describe one attempt."""


class CertifiedProvenanceUnproven(FiveToolTrialError):
    """A run registered as certified could not prove the bytes came through that path."""


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

    @property
    def semantic_config_json(self) -> str:
        """The frozen canonical config bytes the fingerprint is taken over.

        A replay artifact carries the configuration itself, not only its digest: a
        fingerprint identifies a config to somebody who already has it, which is exactly
        the person who does not need a replay artifact.
        """

        return self._semantic_config_json


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

    This is not a final multiple-testing input.  ``ledger_trial_count`` remains this
    campaign ledger's own attempt count; the canonical cross-campaign N lives in the
    ADR-0013 registry (:func:`registered_trial_count`), and sealing refuses when the
    registry has counted fewer attempts than this ledger recorded.  ``replay_artifact_sha256``
    names the content-addressed artifact that re-executes the row, verified against the
    store at seal time.  Phase 3 still requires owner evidence: frozen risk limits, a power
    calculation, and benchmark economics that no engineering session may choose.
    """

    campaign_id: str
    campaign_manifest_sha256: str
    cell_id: str
    hypothesis_id: str
    trial_id: str
    semantic_config_fingerprint: str
    attempt_id: str
    evidence_digest: str
    replay_artifact_sha256: str
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
    accessible_partitions: frozenset[str]
    request_identities: frozenset[tuple[str, str, str]]
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
        *,
        registry_ledger_path: Path | None = None,
        artifact_root: Path | None = None,
    ) -> None:
        self._ledger_path = ledger_path
        # ``None`` means no canonical ADR-0013 registry is wired, which is itself a
        # refusal reason: an attempt that cannot be counted may not start.
        self._registry_ledger_path = registry_ledger_path
        # ``None`` means no replay-artifact store is wired, which is the same kind of
        # refusal reason: an attempt whose evidence could not be replayed may not start.
        self._artifact_root = artifact_root
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

    @property
    def registry_ledger_path(self) -> Path | None:
        """The canonical ADR-0013 registry this broker counts into, if one is wired."""

        return self._registry_ledger_path

    @property
    def artifact_root(self) -> Path | None:
        """The content-addressed replay-artifact store this broker writes into, if wired."""

        return self._artifact_root

    @classmethod
    def from_campaign_manifest(
        cls,
        ledger_path: Path,
        manifest: Mapping[str, object],
        *,
        registry_ledger_path: Path | None = None,
        artifact_root: Path | None = None,
    ) -> FiveToolTrialBroker:
        """Validate a blocked manifest without granting reader authority.

        ``ready_for_certified_research`` is intentionally rejected in public v2.  A
        manifest is a claim, not the missing owner evidence — and the three capabilities
        that now exist are not substitutes for it either: a certified reader is a way to
        read a certified dataset, not a certified dataset; a registry counts trials, it
        does not authorize them; and a replay artifact proves a result is reproducible,
        never that it is economically meaningful or within limits nobody has frozen.
        """

        binding = _validate_campaign_manifest(manifest)
        if binding is not None:  # pragma: no cover - public ready validation always raises
            raise CampaignExecutionBlocked("public v2 cannot authorize an execution-ready campaign")
        broker = cls(
            ledger_path,
            registry_ledger_path=registry_ledger_path,
            artifact_root=artifact_root,
        )
        broker._bind_declared_holdouts(manifest)
        broker._execution_block_reason = (
            "campaign manifest is blocked_until_identity_locks_resolve; "
            f"no {_missing_capability_phrase()} {_missing_capability_noun()} exists"
        )
        return broker

    @classmethod
    def _from_synthetic_manifest_for_tests(
        cls,
        ledger_path: Path,
        manifest: Mapping[str, object],
        *,
        registry_ledger_path: Path | None = None,
        artifact_root: Path | None = None,
    ) -> FiveToolTrialBroker:
        """Create the private callback-driven harness used only by lifecycle tests.

        This seam deliberately bypasses the unavailable certified-readiness authority.
        It must never be used to make a research or performance claim.  When no
        canonical registry or artifact store is named, the harness uses one beside its
        trial ledger; production wiring passes the real ADR-0013 registry path and
        artifact store explicitly.
        """

        binding = _validate_campaign_manifest(
            manifest,
            _allow_synthetic_ready_for_tests=True,
        )
        if binding is None:
            raise CampaignExecutionBlocked(
                "synthetic lifecycle harness requires a structurally ready test manifest"
            )
        broker = cls(
            ledger_path,
            registry_ledger_path=(
                registry_ledger_path
                if registry_ledger_path is not None
                else ledger_path.parent / _DEFAULT_REGISTRY_LEDGER_NAME
            ),
            artifact_root=(
                artifact_root
                if artifact_root is not None
                else ledger_path.parent / _DEFAULT_ARTIFACT_DIR_NAME
            ),
        )
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

        Provenance is settled before the attempt is registered and proven after the read:
        a certified reader attests its locks up front and its receipt is checked against
        the bytes afterwards, while anything else is registered as uncertified.

        Reproducibility is settled last and before the terminal record.  Any attempt that
        got as far as opening data persists a content-addressed replay artifact holding
        everything needed to re-execute it byte-identically, and the terminal record names
        that artifact's digest.  The ordering is deliberate: if the artifact cannot be
        written, no terminal lands, so a completed trial that cannot be replayed cannot
        exist — no artifact, no sealed trial.
        """

        if self._execution_block_reason is not None:
            raise CampaignExecutionBlocked(self._execution_block_reason)
        binding = self._require_binding()
        self._validate_identity(binding, definition, request)
        self._refuse_holdout(request)
        certified = _certified_reader_or_none(reader)
        # A certified reader locked to a different dataset, digest, or partition refuses
        # here — before registration, because nothing has been read and no trial exists.
        data_evidence = (
            certified.attest(request) if certified is not None else _uncertified_evidence(request)
        )
        reads_before = len(certified.receipts) if certified is not None else 0
        receipt = self._start(binding, definition, request, data_evidence)
        data: bytes | None = None
        proven_payload_sha256: str | None = None
        try:
            data = reader(request)
            if not isinstance(data, bytes):
                raise TypeError(f"reader must return bytes, got {type(data).__name__}")
            actual_data_sha256 = hashlib.sha256(data).hexdigest()
            if actual_data_sha256 != request.data_version:
                raise DataVersionMismatch(
                    "reader bytes do not match the campaign-authorized data_version"
                )
            if certified is not None:
                _require_proven_certified_read(
                    certified,
                    request,
                    data,
                    data_evidence,
                    reads_before=reads_before,
                )
                proven_payload_sha256 = certified.receipts[-1].payload_sha256
            evaluation = evaluator(data, receipt)
            if not isinstance(evaluation, TrialEvaluation):
                raise TypeError("evaluator must return TrialEvaluation")
            if not isinstance(evaluation.evidence, EvaluationEvidence):
                raise TypeError("TrialEvaluation.evidence must be EvaluationEvidence")
            evaluation.evidence._require_valid()
        except BaseException as error:
            try:
                data_sha256 = hashlib.sha256(data).hexdigest() if isinstance(data, bytes) else None
                artifact_sha256 = (
                    self._persist_replay_artifact(
                        definition,
                        receipt,
                        TrialOutcome.FAILED,
                        data_sha256=data_sha256,
                        evidence=None,
                        error_type=_bounded_error_type(error),
                        attested=data_evidence,
                        proven_payload_sha256=proven_payload_sha256,
                    )
                    if data_sha256 is not None
                    else None
                )
                self._terminal(
                    receipt,
                    TrialOutcome.FAILED,
                    data_sha256=data_sha256,
                    evidence=None,
                    error_type=_bounded_error_type(error),
                    replay_artifact_sha256=artifact_sha256,
                )
            except Exception as ledger_error:
                raise FiveToolTrialError(
                    "trial failed and its terminal failure could not be recorded"
                ) from ledger_error
            raise
        completed_data_sha256 = hashlib.sha256(data).hexdigest()
        artifact_sha256 = self._persist_replay_artifact(
            definition,
            receipt,
            TrialOutcome.COMPLETED,
            data_sha256=completed_data_sha256,
            evidence=evaluation.evidence,
            error_type=None,
            attested=data_evidence,
            proven_payload_sha256=proven_payload_sha256,
        )
        self._terminal(
            receipt,
            TrialOutcome.COMPLETED,
            data_sha256=completed_data_sha256,
            evidence=evaluation.evidence,
            error_type=None,
            replay_artifact_sha256=artifact_sha256,
        )
        return evaluation.value

    def _persist_replay_artifact(
        self,
        definition: TrialDefinition,
        receipt: TrialReceipt,
        outcome: TrialOutcome,
        *,
        data_sha256: str | None,
        evidence: EvaluationEvidence | None,
        error_type: str | None,
        attested: Mapping[str, object],
        proven_payload_sha256: str | None,
    ) -> str:
        """Write this attempt's replay artifact and return the digest that binds it."""

        body = _replay_artifact_body(
            definition,
            receipt,
            outcome,
            data_sha256=data_sha256,
            evidence=evidence,
            error_type=error_type,
            attested=attested,
            proven_payload_sha256=proven_payload_sha256,
        )
        return write_replay_artifact(self._artifact_root, body).artifact_sha256

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
            registry_ledger_path=self._registry_ledger_path,
            artifact_root=self._artifact_root,
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
        if _normalized_identity(request.partition) not in binding.accessible_partitions:
            raise CampaignIdentityMismatch("data request partition is not campaign-accessible")
        request_identity = (
            _normalized_identity(request.dataset_id),
            _normalized_identity(request.partition),
            request.data_version,
        )
        if request_identity not in binding.request_identities:
            raise CampaignIdentityMismatch("data request identity disagrees with manifest bindings")

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
        data_evidence: Mapping[str, object],
    ) -> TrialReceipt:
        trial_id = deterministic_trial_id(definition, request)
        attempt_id = uuid.uuid4().hex
        # An attempt whose evidence could never be persisted may not begin: the artifact
        # store is checked before the registry, so an unreplayable attempt does not even
        # inflate N. Like the registry, the store is provisioned, never conjured.
        require_artifact_root(self._artifact_root)
        # ADR-0013 §5: the canonical registry is written first, so an attempt that dies
        # anywhere later is still a counted trial. No registry, no trial.
        self._register_canonical_run(
            definition,
            request,
            trial_id=trial_id,
            attempt_id=attempt_id,
            data_evidence=data_evidence,
        )

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

    def _register_canonical_run(
        self,
        definition: TrialDefinition,
        request: DataAccessRequest,
        *,
        trial_id: str,
        attempt_id: str,
        data_evidence: Mapping[str, object],
    ) -> AuditRecord:
        """Record this attempt in the canonical ADR-0013 registry before it reads.

        ``experiment_id`` joins the two ledgers: it is the deterministic trial identity
        and the unique attempt identity, so a canonical record can always be matched to
        the Five-Tool lifecycle records it authorized.  ``data_evidence`` is the
        provenance the reader could attest *before* reading; a certified attestation is
        re-proven against the receipt afterwards, so a completed certified trial cannot
        exist without the proof.
        """

        registry_path = _require_canonical_registry_path(self._registry_ledger_path)
        stage = _CANONICAL_STAGE_BY_PARTITION.get(_normalized_identity(request.partition))
        if stage is None:
            raise CampaignIdentityMismatch(
                f"partition {request.partition!r} has no canonical ADR-0013 research stage; "
                "an unclassifiable partition cannot be registered"
            )
        thread_lock = _thread_lock_for(registry_path)
        with thread_lock, registry_lock(registry_path):
            ledger = _verified_canonical_registry(registry_path)
            record = register_run(
                ledger,
                stage=stage,
                strategy_id=definition.strategy_id,
                config_hash=definition.semantic_config_fingerprint,
                code_commit=definition.code_commit,
                data_hashes={request.dataset_id: dict(data_evidence)},
                criteria_ref=definition.criteria_digest,
                touched_data=True,
                experiment_id=f"{trial_id}:{attempt_id}",
            )
            _fsync_path(ledger.anchor_path)
            _fsync_directory(registry_path.parent)
            _verified_canonical_registry(registry_path)
            return record

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
        return self._start(binding, definition, request, _uncertified_evidence(request))

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
        replay_artifact_sha256: str | None,
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
            replay_artifact_sha256=replay_artifact_sha256,
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


def _certified_reader_or_none(reader: object) -> CertifiedDatasetReader | None:
    """Return the reader only if it is exactly the certified type, never a subclass.

    A subclass may override ``__call__``, ``attest``, or ``receipts``, so it can produce
    the same bytes while proving nothing.  Concrete-type identity is the fail-closed
    reading of "every byte came through the certified path": a reader that *might* be
    something else is recorded as uncertified, which is the honest answer.
    """

    if isinstance(reader, CertifiedDatasetReader) and type(reader) is CertifiedDatasetReader:
        return reader
    return None


def _uncertified_evidence(request: DataAccessRequest) -> dict[str, object]:
    """Provenance for a reader that cannot prove what it read: the caller's claim only."""

    return {
        "dataset_sha256": request.data_version,
        "partition": request.partition,
        "certified_reader": False,
    }


def _require_proven_certified_read(
    reader: CertifiedDatasetReader,
    request: DataAccessRequest,
    data: bytes,
    attested: Mapping[str, object],
    *,
    reads_before: int,
) -> None:
    """Prove the registered certified attestation describes the read that just happened.

    The registered record was written before the read, so on its own it is a commitment.
    This turns it into evidence: exactly one new receipt, for this request, over the
    attested certification manifest and the attested file set, whose payload digest is
    the digest of the bytes the evaluator is about to see.  Any disagreement raises, the
    attempt takes a ``failed`` terminal, and no completed trial carries the claim.
    """

    receipts = reader.receipts
    if len(receipts) != reads_before + 1:
        raise CertifiedProvenanceUnproven(
            "the certified reader did not record exactly one read for this attempt"
        )
    receipt = receipts[-1]
    if receipt.payload_sha256 != hashlib.sha256(data).hexdigest():
        raise CertifiedProvenanceUnproven(
            "returned bytes are not the certified payload the reader recorded"
        )
    if receipt.dataset_id != request.dataset_id or receipt.partition != request.partition:
        raise CertifiedProvenanceUnproven("certified read receipt is bound to a different request")
    if receipt.dataset_sha256 != request.data_version:
        raise CertifiedProvenanceUnproven(
            "certified read receipt disagrees with the campaign dataset lock"
        )
    if receipt.certification_manifest_sha256 != attested.get("certification_manifest_sha256"):
        raise CertifiedProvenanceUnproven(
            "certified read used a different certification manifest than it attested"
        )
    if receipt.file_digests != attested.get("files"):
        raise CertifiedProvenanceUnproven(
            "certified read touched a different file set than it attested"
        )


def _replay_artifact_body(
    definition: TrialDefinition,
    receipt: TrialReceipt,
    outcome: TrialOutcome,
    *,
    data_sha256: str | None,
    evidence: EvaluationEvidence | None,
    error_type: str | None,
    attested: Mapping[str, object],
    proven_payload_sha256: str | None,
) -> dict[str, object]:
    """Assemble everything needed to re-execute one attempt byte-identically.

    The semantic config is carried in full, not only as a fingerprint: a fingerprint
    identifies a configuration to somebody who already has it, and a replay artifact
    exists for the case where nobody does.  The certified block records only what the
    reader actually proved — an uncertified read carries nulls rather than a claim.
    """

    # Attesting is a commitment; the receipt is the proof.  An attempt whose certified
    # proof never completed records an uncertified block, so an artifact never carries a
    # provenance claim the trial itself refused to accept.
    certified = bool(attested.get("certified_reader")) and proven_payload_sha256 is not None
    files = attested.get("files") if certified else None
    return {
        "schema_version": REPLAY_ARTIFACT_SCHEMA_VERSION,
        "campaign": {
            "campaign_id": receipt.campaign_id,
            "campaign_manifest_sha256": receipt.campaign_manifest_sha256,
            "cell_id": receipt.cell_id,
            "hypothesis_id": receipt.hypothesis_id,
            "strategy_id": definition.strategy_id,
            "trial_id": receipt.trial_id,
            "attempt_id": receipt.attempt_id,
        },
        "engine": {
            "code_commit": definition.code_commit,
            "criteria_digest": definition.criteria_digest,
            "input_contract_digest": definition.input_contract_digest,
            "semantic_config_fingerprint": definition.semantic_config_fingerprint,
            "semantic_config": json.loads(definition.semantic_config_json),
        },
        "inputs": {
            "dataset_id": receipt.dataset_id,
            "partition": receipt.partition,
            "data_version": receipt.data_version,
            "data_sha256": data_sha256,
            "certified_read": {
                "certified_reader": certified,
                "dataset_sha256": attested.get("dataset_sha256"),
                "partition": attested.get("partition"),
                "certification_manifest_sha256": (
                    attested.get("certification_manifest_sha256") if certified else None
                ),
                "files": dict(files) if isinstance(files, Mapping) else None,
                "receipt_payload_sha256": proven_payload_sha256 if certified else None,
            },
        },
        "start": {
            "sequence": receipt.start_sequence,
            "record_hash": receipt.start_record_hash,
        },
        "outputs": {
            "outcome": OUTCOME_COMPLETED if outcome is TrialOutcome.COMPLETED else OUTCOME_FAILED,
            "error_type": error_type,
            "artifact_base64": (
                base64.b64encode(evidence.artifact_bytes).decode("ascii")
                if evidence is not None
                else None
            ),
            "artifact_sha256": evidence.artifact_sha256 if evidence is not None else None,
            "evidence_digest": evidence.evidence_digest if evidence is not None else None,
            "observed_sharpe": evidence.observed_sharpe if evidence is not None else None,
            "observations": evidence.observations if evidence is not None else None,
            "skew": evidence.skew if evidence is not None else None,
            "kurtosis": evidence.kurtosis if evidence is not None else None,
        },
    }


def _terminal_payload(
    receipt: TrialReceipt,
    outcome: TrialOutcome,
    *,
    data_sha256: str | None,
    evidence: EvaluationEvidence | None,
    error_type: str | None,
    replay_artifact_sha256: str | None,
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
        # The content address of the replay artifact this attempt persisted. ``None`` only
        # for an attempt that never opened data, which has nothing to reproduce.
        "replay_artifact_sha256": replay_artifact_sha256,
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
                # An orphan start never opened data, so there is nothing to replay and
                # nothing to persist; the artifact binding is honestly absent.
                replay_artifact_sha256=None,
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

    This is one campaign ledger's own count, not the ADR-0013 global research
    multiplicity; :func:`registered_trial_count` derives that one from the registry.
    """

    return _ledger_trial_multiplicity_from_records(_verified_records(ledger_path))


def registered_trial_count(registry_ledger_path: Path, *, strategy_id: str | None = None) -> int:
    """The canonical ADR-0013 multiple-testing N, derived from registered runs.

    Nothing here is self-reported: the count is every ``experiment_run`` record with
    ``touched_data`` in the verified hash-chained registry, optionally scoped to one
    strategy. An unverifiable registry refuses rather than returning a number.
    """

    path = _require_canonical_registry_path(registry_ledger_path)
    thread_lock = _thread_lock_for(path)
    with thread_lock, registry_lock(path):
        return trial_count(_verified_canonical_registry(path), strategy_id=strategy_id)


@dataclass(frozen=True, slots=True)
class ReplayReport:
    """Proof that one recorded attempt re-executed to exactly the bytes it recorded."""

    artifact_sha256: str
    campaign_id: str
    trial_id: str
    attempt_id: str
    outcome: str
    data_sha256: str | None
    certified_reader: bool


def replay_trial[T](
    artifact_root: Path,
    artifact_sha256: str,
    definition: TrialDefinition,
    request: DataAccessRequest,
    *,
    reader: Callable[[DataAccessRequest], bytes],
    evaluator: Callable[[bytes, TrialReceipt], TrialEvaluation[T]],
) -> ReplayReport:
    """Re-execute one attempt from its replay artifact and refuse on any byte divergence.

    The artifact supplies the reconstruction inputs a caller cannot re-derive — the
    campaign manifest digest and the durable start the attempt was bound to — while the
    definition, request, reader, and evaluator are the *live* world being tested.  That is
    the whole question a replay answers: does re-executing today reproduce what was
    recorded?  Every disagreement is collected and named
    (:class:`chronos.research.five_tool.replay.ReplayDivergence`), so a caller learns which
    axis moved — inputs, configuration, certification, outcome, or outputs — rather than
    only that something did.

    **A replay registers no trial and writes no ledger record.**  It re-reads bytes whose
    digest is already recorded and produces no new hypothesis evidence, so counting it
    would inflate the ADR-0013 multiple-testing N with a reproducibility probe.  That is
    the same stance ``chronos.research.repro``'s produce/replay takes and it is asserted,
    not assumed, in ``tests/safety/test_five_tool_replay_exercised.py``.

    Two deliberate scope limits.  A certified reader that cannot even attest this request
    raises its own refusal rather than being folded into a divergence — the reader's
    message is the more useful one.  And only ``Exception`` is classified into an observed
    failed outcome; ``KeyboardInterrupt`` and ``SystemExit`` propagate, because a replay
    that was cancelled is not a replay that diverged.
    """

    artifact = load_replay_artifact(artifact_root, artifact_sha256)
    recorded = artifact.document
    recorded_campaign = _artifact_block(recorded, "campaign")
    recorded_start = _artifact_block(recorded, "start")
    receipt = TrialReceipt(
        trial_id=deterministic_trial_id(definition, request),
        campaign_id=definition.campaign_id,
        campaign_manifest_sha256=str(recorded_campaign["campaign_manifest_sha256"]),
        cell_id=definition.cell_id,
        hypothesis_id=definition.hypothesis_id,
        semantic_config_fingerprint=definition.semantic_config_fingerprint,
        attempt_id=str(recorded_campaign["attempt_id"]),
        start_sequence=cast(int, recorded_start["sequence"]),
        start_record_hash=str(recorded_start["record_hash"]),
        dataset_id=request.dataset_id,
        partition=request.partition,
        data_version=request.data_version,
    )

    certified = _certified_reader_or_none(reader)
    attested = (
        certified.attest(request) if certified is not None else _uncertified_evidence(request)
    )
    reads_before = len(certified.receipts) if certified is not None else 0
    data: bytes | None = None
    proven_payload_sha256: str | None = None
    evidence: EvaluationEvidence | None = None
    error_type: str | None = None
    outcome = TrialOutcome.COMPLETED
    try:
        data = reader(request)
        if not isinstance(data, bytes):
            raise TypeError(f"reader must return bytes, got {type(data).__name__}")
        if hashlib.sha256(data).hexdigest() != request.data_version:
            raise DataVersionMismatch(
                "replayed bytes do not match the campaign-authorized data_version"
            )
        if certified is not None:
            _require_proven_certified_read(
                certified,
                request,
                data,
                attested,
                reads_before=reads_before,
            )
            proven_payload_sha256 = certified.receipts[-1].payload_sha256
        evaluation = evaluator(data, receipt)
        if not isinstance(evaluation, TrialEvaluation):
            raise TypeError("evaluator must return TrialEvaluation")
        if not isinstance(evaluation.evidence, EvaluationEvidence):
            raise TypeError("TrialEvaluation.evidence must be EvaluationEvidence")
        evaluation.evidence._require_valid()
        evidence = evaluation.evidence
    except Exception as error:
        outcome = TrialOutcome.FAILED
        evidence = None
        error_type = _bounded_error_type(error)

    data_sha256 = hashlib.sha256(data).hexdigest() if isinstance(data, bytes) else None
    observed = validate_artifact_body(
        _replay_artifact_body(
            definition,
            receipt,
            outcome,
            data_sha256=data_sha256,
            evidence=evidence,
            error_type=error_type,
            attested=attested,
            proven_payload_sha256=proven_payload_sha256,
        )
    )
    findings = compare_replay_bodies(recorded, observed)
    if findings:
        raise ReplayDivergence(findings)
    certified_read = _artifact_block(recorded, "inputs")["certified_read"]
    return ReplayReport(
        artifact_sha256=artifact.artifact_sha256,
        campaign_id=str(recorded_campaign["campaign_id"]),
        trial_id=str(recorded_campaign["trial_id"]),
        attempt_id=str(recorded_campaign["attempt_id"]),
        outcome=artifact.outcome,
        data_sha256=data_sha256,
        certified_reader=bool(
            certified_read.get("certified_reader") if isinstance(certified_read, dict) else False
        ),
    )


def _artifact_block(body: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = body.get(name)
    if not isinstance(value, dict):  # pragma: no cover - bodies are validated on load
        raise ReplayArtifactBindingBroken(f"replay artifact {name} block is unusable")
    return value


def _require_canonical_registry_path(registry_ledger_path: Path | None) -> Path:
    """Refuse an unwired or unprovisioned canonical registry: no registry, no trial."""

    if registry_ledger_path is None:
        raise CanonicalRegistryUnavailable(
            "no canonical ADR-0013 registry is wired; a Five-Tool attempt that cannot be "
            "counted may not start"
        )
    if not registry_ledger_path.parent.is_dir():
        raise CanonicalRegistryUnavailable(
            f"canonical ADR-0013 registry root {registry_ledger_path.parent} is absent; "
            "the registry is provisioned deliberately, never conjured by a research run"
        )
    return registry_ledger_path


def _verified_canonical_registry(registry_ledger_path: Path) -> RegistryLedger:
    """Open the canonical registry only if its chain and head anchor still verify."""

    try:
        ledger = RegistryLedger(registry_ledger_path)
        ok, detail = ledger.verify()
    except (AuditLogCorruptionError, OSError, ValueError) as error:
        raise CanonicalRegistryUnavailable(
            f"canonical ADR-0013 registry {registry_ledger_path} is unreadable: "
            f"{type(error).__name__}"
        ) from error
    if not ok:
        raise CanonicalRegistryUnavailable(
            f"canonical ADR-0013 registry {registry_ledger_path} failed verification: {detail}"
        )
    return ledger


def _missing_capability_phrase() -> str:
    """Name every capability a ready manifest still cannot substitute for."""

    names = MISSING_CERTIFIED_RESEARCH_CAPABILITIES
    if not names:  # pragma: no cover - the campaign is blocked on at least one capability
        raise FiveToolTrialError("the missing-capability list may not be empty while blocked")
    if len(names) == 1:
        return names[0]
    if len(names) == 2:  # pragma: no cover - one capability remains
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"  # pragma: no cover - one remains


def _missing_capability_noun() -> str:
    """Agree the refusal's grammar with the list, so the sentence cannot go stale either."""

    return "capability" if len(MISSING_CERTIFIED_RESEARCH_CAPABILITIES) == 1 else "capabilities"


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
    *,
    registry_ledger_path: Path | None,
    artifact_root: Path | None,
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
                artifact_root=artifact_root,
            )

        score_rows = _validated_completed_rows(records, binding)
        # No artifact, no sealed trial: every scored row must still be re-executable from
        # the store, and the artifact must describe the attempt the ledger says it does.
        _require_bound_replay_artifacts(score_rows, artifact_root)
        ledger_trial_count = _ledger_trial_multiplicity_from_records(records)
        if ledger_trial_count < 1:
            raise FiveToolTrialError("cannot seal a campaign without durable attempts")
        # Cross-ledger completeness: the canonical registry must have counted at least
        # every durable attempt this campaign made. A campaign whose attempts were not
        # registered cannot be sealed, so the derived N can never be quietly short.
        registered = registered_trial_count(
            _require_canonical_registry_path(registry_ledger_path),
            strategy_id=binding.strategy_id,
        )
        if registered < ledger_trial_count:
            raise FiveToolTrialError(
                "canonical ADR-0013 registry under-counts this campaign: registered="
                f"{registered} < durable attempts={ledger_trial_count}"
            )
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
    data_version = payload.get("data_version")
    if not isinstance(data_version, str):
        raise FiveToolTrialError("trial start data_version is invalid")
    request_identity = (
        _normalized_identity(binding.dataset_id),
        _normalized_identity(partition),
        data_version,
    )
    if request_identity not in binding.request_identities:
        raise FiveToolTrialError("trial start request identity disagrees with campaign bindings")
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
    replay_artifact_sha256 = payload.get("replay_artifact_sha256")
    if replay_artifact_sha256 is not None:
        if not isinstance(replay_artifact_sha256, str):
            raise FiveToolTrialError("trial terminal replay_artifact_sha256 is invalid")
        _require_lifecycle_sha256("replay_artifact_sha256", replay_artifact_sha256)
    if outcome is TrialOutcome.COMPLETED:
        if data_sha256 != start.payload.get("data_version"):
            raise FiveToolTrialError("terminal bytes do not match the authorized data version")
        if payload.get("error_type") is not None:
            raise FiveToolTrialError("completed terminal cannot carry an error type")
        if replay_artifact_sha256 is None:
            raise FiveToolTrialError(
                "completed terminal has no replay artifact; a result that cannot be "
                "re-executed is not a sealed trial"
            )
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
    # An attempt that never opened data has nothing to reproduce; one that died after
    # opening data must still name the artifact that reproduces that failure. This is the
    # ledger half of "no artifact, no trial" on the aborted-after-read path.
    if (data_sha256 is None) != (replay_artifact_sha256 is None):
        raise FiveToolTrialError(
            "a failed terminal that opened data must name its replay artifact, and one "
            "that did not must name none"
        )
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


def _require_bound_replay_artifacts(
    rows: Sequence[tuple[AuditRecord, AuditRecord]],
    artifact_root: Path | None,
) -> tuple[ReplayArtifact, ...]:
    """Refuse a seal whose scored rows are not backed by the artifacts they name.

    The ledger names a content address; the store either holds those exact bytes or it
    does not.  A missing, tampered, or self-inconsistent artifact refuses in the store's
    own vocabulary, and an artifact that describes a *different* attempt refuses here —
    the binding is checked in both directions so an artifact cannot be swapped after the
    fact for another valid one.
    """

    artifacts: list[ReplayArtifact] = []
    for start, terminal in rows:
        digest = terminal.payload.get("replay_artifact_sha256")
        if not isinstance(digest, str):
            raise ReplayArtifactBindingBroken(
                "a completed trial record names no replay artifact; a result that cannot "
                "be re-executed is not a sealed trial"
            )
        artifact = load_replay_artifact(artifact_root, digest)
        campaign = artifact.document["campaign"]
        outputs = artifact.document["outputs"]
        if not isinstance(campaign, dict) or not isinstance(outputs, dict):
            raise ReplayArtifactBindingBroken(  # pragma: no cover - validated on load
                "replay artifact structure is not usable"
            )
        expected: tuple[tuple[str, object, object], ...] = (
            ("campaign_id", campaign.get("campaign_id"), start.payload.get("campaign_id")),
            ("cell_id", campaign.get("cell_id"), start.payload.get("cell_id")),
            ("trial_id", campaign.get("trial_id"), start.payload.get("trial_id")),
            ("attempt_id", campaign.get("attempt_id"), start.payload.get("attempt_id")),
            (
                "evidence_digest",
                outputs.get("evidence_digest"),
                terminal.payload.get("evidence_digest"),
            ),
            (
                "artifact_sha256",
                outputs.get("artifact_sha256"),
                terminal.payload.get("evidence_artifact_sha256"),
            ),
            ("outcome", outputs.get("outcome"), OUTCOME_COMPLETED),
        )
        for field_name, recorded, ledgered in expected:
            if recorded != ledgered:
                raise ReplayArtifactBindingBroken(
                    f"replay artifact {digest} disagrees with its trial record on "
                    f"{field_name}: artifact={recorded!r}, ledger={ledgered!r}"
                )
        artifacts.append(artifact)
    return tuple(artifacts)


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
    artifact_root: Path | None,
) -> tuple[LedgerLocalScoreInput, ...]:
    rows = _validated_completed_rows(records, binding)
    _require_bound_replay_artifacts(rows, artifact_root)
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
            replay_artifact_sha256=str(terminal.payload["replay_artifact_sha256"]),
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
    """Validate the blocked v2 manifest; public ready authority is not implemented."""

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
            "supersedes_campaign_id",
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
            "reference_arm",
            "hypothesis_ids",
            "campaign_cells",
            "execution_bindings",
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
    if campaign_id != CAMPAIGN_ID:
        raise ValueError(f"campaign_id must exactly identify {CAMPAIGN_ID!r}")
    if manifest.get("supersedes_campaign_id") != SUPERSEDES_CAMPAIGN_ID:
        raise ValueError(f"supersedes_campaign_id must exactly identify {SUPERSEDES_CAMPAIGN_ID!r}")
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
            "EXECUTION_READY is not implemented in public v2: a manifest cannot replace "
            f"the missing {_missing_capability_phrase()} {_missing_capability_noun()}"
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
    _require_certified_identity("data.dataset_version_lock.dataset_id", dataset_id)
    if dataset_lock.get("required_before_execution") is not True:
        raise ValueError("data.dataset_version_lock must be required before execution")
    dataset_release_digest: str | None = None
    raw_dataset_release_digest = dataset_lock.get("sha256")
    if raw_dataset_release_digest is None:
        if dataset_lock.get("status") != "pending_certified_dataset":
            raise ValueError(
                "null dataset release digest requires pending_certified_dataset status"
            )
    elif isinstance(raw_dataset_release_digest, str):
        _require_sha256("data.dataset_version_lock.sha256", raw_dataset_release_digest)
        if dataset_lock.get("status") != "resolved":
            raise ValueError("resolved dataset release digest requires resolved status")
        dataset_release_digest = raw_dataset_release_digest
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
    for index, partition in enumerate(accessible):
        _require_certified_identity(f"data.accessible_partitions[{index}]", str(partition))
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
        _require_certified_identity(
            f"data.declared_holdouts[{index}].dataset_id",
            _required_string(holdout, "dataset_id"),
        )
        _require_certified_identity(
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
    holdout_partitions = {partition for _dataset, partition in holdout_keys}
    if set(normalized_accessible) & holdout_partitions:
        raise ValueError("declared holdout partition aliases cannot be ordinarily accessible")
    ordinary_keys = {
        (_normalized_identity(dataset_id), partition) for partition in normalized_accessible
    }
    if ordinary_keys & set(holdout_keys):
        raise ValueError("declared holdout dataset/partition aliases cannot be accessible")
    contamination = data.get("known_contamination")
    if (
        not isinstance(contamination, list)
        or not contamination
        or not all(isinstance(item, str) and item.strip() for item in contamination)
        or len(set(contamination)) != len(contamination)
    ):
        raise ValueError("data.known_contamination must be a unique non-empty string list")

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
    _require_exact_keys(
        fill_policy,
        {
            "signal_clock",
            "market_entry_eligibility",
            "higher_timeframe",
            "chart_ohlcv_approximation",
            "bar_magnifier",
            "tradingview_fill_parity",
        },
        "fill_policy",
    )
    for name in fill_policy:
        _require_nonempty(f"fill_policy.{name}", _required_string(fill_policy, name))
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

    reference_arm = _required_mapping(manifest, "reference_arm")
    _require_exact_keys(
        reference_arm,
        {"arm_id", "pine_overrides", "research_ablation"},
        "reference_arm",
    )
    if reference_arm.get("arm_id") != _REFERENCE_ARM_ID:
        raise ValueError(f"reference_arm.arm_id must be {_REFERENCE_ARM_ID!r}")
    if reference_arm.get("pine_overrides") != {}:
        raise ValueError("reference_arm.pine_overrides must remain the empty Pine default")
    if reference_arm.get("research_ablation") is not None:
        raise ValueError("reference_arm.research_ablation must remain null")

    hypotheses = manifest.get("hypothesis_ids")
    expected_hypotheses = set(_EXPECTED_CELL_HYPOTHESES.values())
    if (
        not isinstance(hypotheses, list)
        or not all(isinstance(value, str) and value.strip() for value in hypotheses)
        or len(hypotheses) != len(expected_hypotheses)
        or set(hypotheses) != expected_hypotheses
    ):
        raise ValueError("hypothesis_ids must contain the exact six component hypotheses")
    cells = manifest.get("campaign_cells")
    if not isinstance(cells, list) or not cells:
        raise ValueError("campaign_cells must be a non-empty list")
    cell_ids: set[str] = set()
    parsed_cells: list[_CampaignCell] = []
    for index, cell in enumerate(cells):
        if not isinstance(cell, dict):
            raise ValueError(f"campaign_cells[{index}] must be an object")
        _require_exact_keys(
            cell,
            {"cell_id", "hypothesis_id", "role", "ablation_policy"},
            f"campaign_cells[{index}]",
        )
        cell_id = _required_string(cell, "cell_id")
        _require_nonempty(f"campaign_cells[{index}].cell_id", cell_id)
        if cell_id in cell_ids:
            raise ValueError("campaign cell IDs must be unique")
        cell_ids.add(cell_id)
        hypothesis_id = _required_string(cell, "hypothesis_id")
        expected_hypothesis = _EXPECTED_CELL_HYPOTHESES.get(cell_id)
        if expected_hypothesis is None or hypothesis_id != expected_hypothesis:
            raise ValueError("campaign cell ID/hypothesis topology is not the frozen v2 plan")
        _require_nonempty(f"campaign_cells[{index}].role", _required_string(cell, "role"))
        policy = _required_mapping(cell, "ablation_policy")
        _validate_ablation_policy_shape(
            policy,
            context=f"campaign_cells[{index}].ablation_policy",
            expected_pending_codes=_EXPECTED_CELL_BLOCKER_CODES[cell_id],
            allow_resolved_for_synthetic_tests=_allow_synthetic_ready_for_tests,
        )
        parsed_cells.append(
            _CampaignCell(
                cell_id=cell_id,
                hypothesis_id=hypothesis_id,
                semantic_config_json=_canonical_json(policy),
            )
        )
    if cell_ids != set(_EXPECTED_CELL_HYPOTHESES):
        raise ValueError("campaign_cells must contain the exact seven v2 comparisons")

    request_identities = _validate_execution_bindings_shape(
        _required_mapping(manifest, "execution_bindings"),
        dataset_id=dataset_id,
        dataset_release_digest=dataset_release_digest,
        accessible_partitions=frozenset(str(item) for item in accessible),
        declared_holdout_keys=frozenset(holdout_keys),
        declared_holdout_partitions=frozenset(holdout_partitions),
    )

    statistics = _required_mapping(manifest, "statistics")
    _require_exact_keys(
        statistics,
        {
            "sample_floor",
            "instruments_required",
            "materially_different_regimes_required",
            "expectancy_and_benchmark_alpha_95pct_lower_bound",
            "deflated_sharpe_probability_min",
            "fwer_or_fdr_q_max",
            "probability_backtest_overfit_max",
            "parameter_neighbor_pass_fraction_min",
            "best_trade_removal",
            "best_month_removal",
            "drawdown_cvar_concentration_limits",
            "two_phase_scoring",
        },
        "statistics",
    )
    for name, expected_count in {
        "instruments_required": 3,
        "materially_different_regimes_required": 2,
    }.items():
        value = statistics.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value != expected_count:
            raise ValueError(f"statistics.{name} must be exactly {expected_count}")
    for name, expected_threshold in {
        "deflated_sharpe_probability_min": 0.95,
        "fwer_or_fdr_q_max": 0.05,
        "probability_backtest_overfit_max": 0.1,
        "parameter_neighbor_pass_fraction_min": 0.67,
    }.items():
        value = statistics.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or not 0.0 <= value <= 1.0
            or value != expected_threshold
        ):
            raise ValueError(f"statistics.{name} must be exactly {expected_threshold}")
    for name in (
        "sample_floor",
        "expectancy_and_benchmark_alpha_95pct_lower_bound",
        "best_trade_removal",
        "best_month_removal",
        "drawdown_cvar_concentration_limits",
        "two_phase_scoring",
    ):
        _require_nonempty(f"statistics.{name}", _required_string(statistics, name))
    trial_accounting = _required_mapping(manifest, "trial_accounting")
    _require_exact_keys(
        trial_accounting,
        {
            "record_kind_start",
            "start_must_precede_reader",
            "reader_and_evaluator_failures_count",
            "multiplicity",
            "candidate_order_or_display_rename_changes_verdict",
        },
        "trial_accounting",
    )
    required_accounting = {
        "record_kind_start": KIND_TRIAL_STARTED,
        "start_must_precede_reader": True,
        "reader_and_evaluator_failures_count": True,
        "candidate_order_or_display_rename_changes_verdict": False,
    }
    for key, expected_value in required_accounting.items():
        if trial_accounting.get(key) != expected_value:
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
        "campaign_plan_sha256",
        "ablation_policy_sha256",
        "execution_bindings_sha256",
        "certified_catalog_sha256",
        "source_receipt_sha256",
        "evaluator_sha256",
        "criteria_digest",
        "code_commit",
    }
    if (
        not isinstance(invalidates, list)
        or not all(isinstance(item, str) for item in invalidates)
        or len(invalidates) != len(set(invalidates))
        or set(invalidates) != required_invalidations
    ):
        raise ValueError("identity_changes_that_invalidate_campaign must be exact and complete")

    compiler_report = (
        _compile_campaign_manifest_for_tests(manifest)
        if _allow_synthetic_ready_for_tests
        else compile_campaign_manifest(manifest)
    )
    if ready:
        if not compiler_report.ready or compiler_report.plan is None:
            details = "; ".join(
                f"{item.code.value}@{item.location}: {item.message}"
                for item in compiler_report.blockers
            )
            raise ValueError(f"execution-ready campaign does not compile: {details}")
    else:
        expected_pending_codes = {
            CampaignBlockerCode.EXECUTION_STATE,
            CampaignBlockerCode.IDENTITY_UNRESOLVED,
            CampaignBlockerCode.DECLARED_BLOCKER,
            CampaignBlockerCode.ABLATION_POLICY_PENDING,
            CampaignBlockerCode.RESEARCH_ABLATION_PENDING,
            CampaignBlockerCode.UNRESOLVED_CELL_CONFIG,
            CampaignBlockerCode.MISSING_COMPARISON_ARM,
            CampaignBlockerCode.MISSING_NEIGHBOR_AXIS,
            CampaignBlockerCode.UNBOUND_SHARED_SIGNAL_STREAM,
            CampaignBlockerCode.UNBOUND_PSEUDO_EVENT_STREAM,
            CampaignBlockerCode.UNREPRESENTABLE_ABLATION,
            CampaignBlockerCode.EXECUTION_BINDING_PENDING,
            CampaignBlockerCode.CERTIFIED_CATALOG_PENDING,
            CampaignBlockerCode.PARTITION_STAGE_BINDINGS_PENDING,
            CampaignBlockerCode.EVALUATOR_PENDING,
        }
        invalid = tuple(
            blocker
            for blocker in compiler_report.blockers
            if blocker.code not in expected_pending_codes
        )
        if invalid:
            details = "; ".join(
                f"{item.code.value}@{item.location}: {item.message}" for item in invalid
            )
            raise ValueError(f"blocked campaign contains invalid compiler structure: {details}")

    if ready and (code_commit is None or criteria_digest is None or dataset_release_digest is None):
        raise ValueError("execution-ready manifest requires every identity lock to be resolved")
    if not ready:
        return None
    assert (
        code_commit is not None
        and criteria_digest is not None
        and dataset_release_digest is not None
    )
    return _CampaignBinding(
        campaign_id=campaign_id,
        manifest_sha256=_sha256_text(canonical_manifest),
        strategy_id=strategy_id,
        code_commit=code_commit,
        criteria_digest=criteria_digest,
        input_contract_digest=input_contract_digest(),
        dataset_id=dataset_id,
        accessible_partitions=frozenset(normalized_accessible),
        request_identities=request_identities,
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


def _validate_ablation_policy_shape(
    policy: Mapping[str, object],
    *,
    context: str,
    expected_pending_codes: frozenset[str],
    allow_resolved_for_synthetic_tests: bool,
) -> None:
    """Require canonical v2 blockers, except inside the private synthetic test seam."""

    _require_exact_keys(
        policy,
        {
            "schema_version",
            "status",
            "treatment",
            "control",
            "comparison",
            "allowed_differences",
            "held_fixed",
            "neighbor_axes",
            "resolution_blockers",
        },
        context,
    )
    if policy.get("schema_version") != ABLATION_POLICY_SCHEMA_VERSION:
        raise ValueError(f"{context}.schema_version must be {ABLATION_POLICY_SCHEMA_VERSION!r}")
    status = policy.get("status")
    blockers = _validate_resolution_blocker_objects(
        policy.get("resolution_blockers"),
        context=f"{context}.resolution_blockers",
        allowed_codes=_CELL_RESOLUTION_BLOCKER_CODES,
    )
    blocker_codes = tuple(code for code, _message in blockers)
    if len(blocker_codes) != len(set(blocker_codes)):
        raise ValueError(f"{context}.resolution_blockers must use unique codes")
    if status == "pending_resolution":
        if (
            policy.get("treatment") is not None
            or policy.get("control") is not None
            or policy.get("comparison") is not None
            or policy.get("allowed_differences") != []
            or policy.get("held_fixed") is not None
            or policy.get("neighbor_axes") != []
            or not blockers
        ):
            raise ValueError(
                f"{context} pending policy must keep semantic fields null/empty and "
                "enumerate resolution blockers"
            )
        if set(blocker_codes) != expected_pending_codes:
            raise ValueError(f"{context} must retain the exact canonical pending blocker-code set")
        return
    if status != "resolved":
        raise ValueError(f"{context}.status must be pending_resolution or resolved")
    if not allow_resolved_for_synthetic_tests:
        raise ValueError(
            f"{context} cannot resolve under public campaign v2; a new typed campaign "
            "schema is required"
        )
    if blockers:
        raise ValueError(f"{context} resolved policy cannot retain resolution blockers")
    for name in ("treatment", "control", "comparison", "held_fixed"):
        if not isinstance(policy.get(name), dict):
            raise ValueError(f"{context}.{name} must be an object when resolved")
    for name in ("allowed_differences", "neighbor_axes"):
        if not isinstance(policy.get(name), list):
            raise ValueError(f"{context}.{name} must be a list when resolved")


def _validate_execution_bindings_shape(
    bindings: Mapping[str, object],
    *,
    dataset_id: str,
    dataset_release_digest: str | None,
    accessible_partitions: frozenset[str],
    declared_holdout_keys: frozenset[tuple[str, str]],
    declared_holdout_partitions: frozenset[str],
) -> frozenset[tuple[str, str, str]]:
    """Validate pending or test-only resolved broker/data/evaluator bindings."""

    context = "execution_bindings"
    _require_exact_keys(
        bindings,
        {
            "schema_version",
            "status",
            "catalog_manifest_sha256",
            "partition_stage_map",
            "requests",
            "evaluator",
            "resolution_blockers",
        },
        context,
    )
    if bindings.get("schema_version") != EXECUTION_BINDINGS_SCHEMA_VERSION:
        raise ValueError(f"{context}.schema_version must be {EXECUTION_BINDINGS_SCHEMA_VERSION!r}")
    status = bindings.get("status")
    blockers = _validate_resolution_blocker_objects(
        bindings.get("resolution_blockers"),
        context=f"{context}.resolution_blockers",
        allowed_codes=_EXECUTION_RESOLUTION_BLOCKER_CODES,
    )
    if status == "pending_resolution":
        if (
            bindings.get("catalog_manifest_sha256") is not None
            or bindings.get("partition_stage_map") is not None
            or bindings.get("requests") is not None
            or bindings.get("evaluator") is not None
            or not blockers
        ):
            raise ValueError(
                "pending execution_bindings must keep identities null and enumerate blockers"
            )
        return frozenset()
    if status != "resolved":
        raise ValueError("execution_bindings.status must be pending_resolution or resolved")
    if blockers:
        raise ValueError("resolved execution_bindings cannot retain resolution blockers")
    _require_sha256(
        "execution_bindings.catalog_manifest_sha256",
        _required_string(bindings, "catalog_manifest_sha256"),
    )
    raw_stage_map = bindings.get("partition_stage_map")
    if not isinstance(raw_stage_map, dict):
        raise ValueError("resolved execution_bindings.partition_stage_map must be an object")
    stage_map: dict[str, str] = {}
    normalized_stage_partitions: set[str] = set()
    for partition, stage in raw_stage_map.items():
        if not isinstance(partition, str):
            raise ValueError("execution_bindings.partition_stage_map keys must be strings")
        _require_certified_identity(
            f"execution_bindings.partition_stage_map.{partition}", partition
        )
        if not isinstance(stage, str) or stage not in {"dev", "validation"}:
            raise ValueError(
                f"execution_bindings.partition_stage_map.{partition} has unsupported stage"
            )
        normalized = _normalized_identity(partition)
        if normalized in normalized_stage_partitions:
            raise ValueError("execution binding stage-map partitions must be canonically unique")
        normalized_stage_partitions.add(normalized)
        stage_map[partition] = stage

    raw_requests = bindings.get("requests")
    if not isinstance(raw_requests, list) or not raw_requests:
        raise ValueError("resolved execution_bindings.requests must be a list")
    request_ids: set[str] = set()
    semantic_requests: set[tuple[str, str, str]] = set()
    content_identities: set[tuple[str, str]] = set()
    request_partitions: set[str] = set()
    for index, raw_request in enumerate(raw_requests):
        context = f"execution_bindings.requests[{index}]"
        if not isinstance(raw_request, dict):
            raise ValueError(f"{context} must be an object")
        _require_exact_keys(
            raw_request,
            {
                "request_id",
                "dataset_id",
                "partition",
                "data_version",
                "source_id",
                "source_receipt_sha256",
            },
            context,
        )
        request_id = _required_string(raw_request, "request_id")
        _require_nonempty(f"{context}.request_id", request_id)
        if request_id in request_ids:
            raise ValueError("execution binding request IDs must be unique")
        request_ids.add(request_id)

        request_dataset = _required_string(raw_request, "dataset_id")
        request_partition = _required_string(raw_request, "partition")
        source_id = _required_string(raw_request, "source_id")
        _require_certified_identity(f"{context}.dataset_id", request_dataset)
        _require_certified_identity(f"{context}.partition", request_partition)
        _require_certified_identity(f"{context}.source_id", source_id)
        data_version = _required_string(raw_request, "data_version")
        source_receipt = _required_string(raw_request, "source_receipt_sha256")
        _require_sha256(f"{context}.data_version", data_version)
        _require_sha256(f"{context}.source_receipt_sha256", source_receipt)

        normalized_dataset = _normalized_identity(request_dataset)
        normalized_partition = _normalized_identity(request_partition)
        if normalized_partition in declared_holdout_partitions:
            raise ValueError("execution request partition aliases a declared holdout")
        if (normalized_dataset, normalized_partition) in declared_holdout_keys:
            raise ValueError("execution request aliases a declared holdout identity")
        if request_partition not in accessible_partitions:
            raise ValueError("execution request partition is not declared accessible")
        if request_partition not in stage_map:
            raise ValueError("execution request partition has no stage mapping")
        if dataset_release_digest is None:
            raise ValueError("resolved execution bindings require a resolved dataset release")
        if request_dataset != dataset_id:
            raise ValueError("execution request disagrees with the campaign dataset ID")

        semantic_key = (normalized_dataset, normalized_partition, data_version)
        if semantic_key in semantic_requests:
            raise ValueError("execution requests must have unique semantic data identities")
        semantic_requests.add(semantic_key)
        content_identity = (normalized_dataset, data_version)
        if content_identity in content_identities:
            raise ValueError(
                "distinct execution partitions must have distinct dataset/content identities"
            )
        content_identities.add(content_identity)
        request_partitions.add(request_partition)

    if set(stage_map) != request_partitions:
        raise ValueError("partition stage map must exactly cover the certified requests")

    evaluator = bindings.get("evaluator")
    if not isinstance(evaluator, dict):
        raise ValueError("resolved execution_bindings.evaluator must be an object")
    _require_exact_keys(
        evaluator,
        {"schema_version", "evaluator_id", "sha256"},
        "execution_bindings.evaluator",
    )
    if evaluator.get("schema_version") != EVALUATOR_BINDING_SCHEMA_VERSION:
        raise ValueError(
            "execution_bindings.evaluator.schema_version must identify the v1 evaluator"
        )
    _require_nonempty(
        "execution_bindings.evaluator.evaluator_id",
        _required_string(evaluator, "evaluator_id"),
    )
    _require_sha256(
        "execution_bindings.evaluator.sha256",
        _required_string(evaluator, "sha256"),
    )
    return frozenset(semantic_requests)


def _validate_resolution_blocker_objects(
    value: object,
    *,
    context: str,
    allowed_codes: frozenset[str],
) -> tuple[tuple[str, str], ...]:
    """Require closed-code blocker objects; free-text readiness cannot grant authority."""

    if not isinstance(value, list):
        raise ValueError(f"{context} must be a list")
    parsed: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        item_context = f"{context}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{item_context} must be an object")
        _require_exact_keys(item, {"code", "message"}, item_context)
        code = item.get("code")
        message = item.get("message")
        if not isinstance(code, str) or code not in allowed_codes:
            raise ValueError(f"{item_context}.code is unknown or disallowed")
        if not isinstance(message, str) or not message.strip():
            raise ValueError(f"{item_context}.message must be a non-empty string")
        parsed.append((code, message))
    return tuple(parsed)


def _validate_replay_policy_lock(lock: Mapping[str, object]) -> FiveToolReplayPolicy:
    _require_exact_keys(lock, {"canonical", "sha256"}, "replay_policy")
    canonical = _required_mapping(lock, "canonical")
    expected_keys = set(FiveToolReplayPolicy().canonical_payload)
    _require_exact_keys(canonical, expected_keys, "replay_policy.canonical")

    initial_equity = canonical.get("initial_equity")
    commission = canonical.get("commission_bps_per_fill")
    slippage = canonical.get("slippage_ticks_per_fill")
    target_slippage = canonical.get("apply_slippage_to_target_limits")
    if isinstance(initial_equity, bool) or not isinstance(initial_equity, int | float):
        raise ValueError("replay_policy initial_equity must be numeric")
    if isinstance(commission, bool) or not isinstance(commission, int | float):
        raise ValueError("replay_policy commission must be numeric")
    if isinstance(slippage, bool) or not isinstance(slippage, int):
        raise ValueError("replay_policy slippage ticks must be an integer")
    if not isinstance(target_slippage, bool):
        raise ValueError("replay_policy target-limit slippage must be boolean")
    try:
        policy = FiveToolReplayPolicy(
            initial_equity=float(initial_equity),
            parameter_variant=_required_string(canonical, "parameter_variant"),
            fill_policy=FillPolicy(_required_string(canonical, "fill_policy")),
            commission_bps_per_fill=float(commission),
            slippage_ticks_per_fill=slippage,
            apply_slippage_to_target_limits=target_slippage,
            terminal_position_policy=TerminalPositionPolicy(
                _required_string(canonical, "terminal_position_policy")
            ),
        )
    except ValueError as error:
        raise ValueError(f"replay_policy is invalid: {error}") from error
    if _canonical_json(canonical) != _canonical_json(policy.canonical_payload):
        raise ValueError("replay_policy canonical execution semantics do not match the adapter")
    digest = _required_string(lock, "sha256")
    _require_sha256("replay_policy.sha256", digest)
    if digest != policy.digest:
        raise ValueError("replay_policy.sha256 does not match its canonical policy")
    return policy


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


def _require_certified_identity(name: str, value: str) -> None:
    if _CERTIFIED_IDENTITY.fullmatch(value) is None:
        raise ValueError(f"{name} is not a valid certified-data identity")


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
