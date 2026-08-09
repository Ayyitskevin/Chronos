"""Pure Five-Tool campaign compilation and fail-closed readiness reporting.

The compiler accepts an already-decoded JSON manifest and produces immutable metadata
only. Constructing :class:`FiveToolSettings` performs deterministic reads of the frozen,
source-bound Pine contract; those configuration reads are the sole I/O boundary. The
compiler opens no campaign dataset, registry, replay store, runner, or holdout capability
and performs no clock, environment, git, network, order, promotion, or write operation.
A ready plan is campaign-atomic: any unresolved identity, invalid Pine override,
semantic ablation, or execution-binding blocker suppresses the entire executable plan.
The public v2 compiler categorically blocks resolved cells because the seven scientific
comparisons do not yet have reviewed typed semantics. A private test-only seam exercises
generic deterministic plan construction without granting public execution readiness.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
from typing import cast

from chronos.research.five_tool.contract import load_contract, semantic_contract_digest
from chronos.research.five_tool.models import (
    FiveToolInputError,
    FiveToolSettings,
    InputValue,
)
from chronos.research.five_tool.planning import FillPolicy
from chronos.research.five_tool.replay import (
    FiveToolReplayPolicy,
    ReplayInputError,
    TerminalPositionPolicy,
)

CAMPAIGN_SCHEMA_VERSION = "chronos-five-tool-campaign-v2"
CAMPAIGN_ID = "five-tool-v3.6-preregistered-002"
SUPERSEDES_CAMPAIGN_ID = "five-tool-v3.6-preregistered-001"
ABLATION_POLICY_SCHEMA_VERSION = "chronos-five-tool-ablation-policy-v1"
RESEARCH_ABLATION_SCHEMA_VERSION = "chronos-five-tool-research-ablation-v1"
EXECUTION_BINDINGS_SCHEMA_VERSION = "chronos-five-tool-execution-bindings-v1"
EVALUATOR_BINDING_SCHEMA_VERSION = "chronos-five-tool-evaluator-v1"
COMPILED_CAMPAIGN_SCHEMA_VERSION = "chronos-five-tool-compiled-campaign-v1"
COMPILED_CELL_SCHEMA_VERSION = "chronos-five-tool-compiled-cell-v1"
COMPILED_ARM_SCHEMA_VERSION = "chronos-five-tool-compiled-arm-v1"
EXECUTION_READY = "ready_for_certified_research"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,255}")
_STAGES = frozenset({"dev", "validation"})
_ORDINARY_HOLDOUT_PARTITIONS = frozenset(
    {"holdout", "final", "reserved", "reserved_final", "final_holdout"}
)
_HELD_SETTINGS_FIELDS = (
    "history_start_utc",
    "contract_digest",
    "exchange_timezone",
    "point_value",
    "minimum_tick",
)
_ROOT_KEYS = {
    "blocked_before_first_data_read",
    "campaign_cells",
    "campaign_id",
    "code_commit_lock",
    "costs",
    "created_at_utc",
    "criteria_document",
    "criteria_lock",
    "data",
    "execution_bindings",
    "execution_state",
    "fill_policy",
    "hypothesis_ids",
    "identity_changes_that_invalidate_campaign",
    "performance_claims",
    "promotion_authority",
    "purpose",
    "reference_arm",
    "replay_policy",
    "schema_version",
    "statistics",
    "strategy",
    "supersedes_campaign_id",
    "trial_accounting",
}
_DATA_KEYS = {
    "primary_instruments",
    "benchmark",
    "timeframe",
    "history_start_utc",
    "accessible_partitions",
    "dataset_version_lock",
    "declared_holdouts",
    "known_contamination",
}
_FILL_POLICY_KEYS = {
    "signal_clock",
    "market_entry_eligibility",
    "higher_timeframe",
    "chart_ohlcv_approximation",
    "bar_magnifier",
    "tradingview_fill_parity",
}
_STATISTICS_KEYS = {
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
}
_TRIAL_ACCOUNTING_KEYS = {
    "record_kind_start",
    "start_must_precede_reader",
    "reader_and_evaluator_failures_count",
    "multiplicity",
    "candidate_order_or_display_rename_changes_verdict",
}
_EXPECTED_CELL_HYPOTHESES = {
    "5t-trend-directional-paired": "H-5T-001-TREND",
    "5t-momentum-score-paired": "H-5T-002-MOMENTUM",
    "5t-vol-scaling-paired": "H-5T-003-VOL-SCALING",
    "5t-rsi-divergence-paired": "H-5T-004-DIVERGENCE",
    "5t-mfi-divergence-paired": "H-5T-004-DIVERGENCE",
    "5t-relative-strength-paired": "H-5T-005-RELATIVE-STRENGTH",
    "5t-regime-filter-paired": "H-5T-006-REGIME-FILTER",
}
_INVALIDATING_IDENTITIES = {
    "pine_source_sha256",
    "input_contract_sha256",
    "semantic_config_sha256",
    "dataset_version_sha256",
    "history_start_utc",
    "benchmark_identity",
    "fill_policy",
    "replay_policy_sha256",
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


class AblationPolicyStatus(StrEnum):
    PENDING_RESOLUTION = "pending_resolution"
    RESOLVED = "resolved"


class ComparisonDirection(StrEnum):
    GREATER_THAN = "greater_than"
    LESS_THAN = "less_than"
    TWO_SIDED = "two_sided"


class CampaignBlockerCode(StrEnum):
    MANIFEST_SCHEMA = "manifest_schema"
    EXECUTION_STATE = "execution_state"
    IDENTITY_UNRESOLVED = "identity_unresolved"
    DECLARED_BLOCKER = "declared_blocker"
    REPLAY_POLICY_INVALID = "replay_policy_invalid"
    REFERENCE_ARM_INVALID = "reference_arm_invalid"
    ABLATION_POLICY_PENDING = "ablation_policy_pending"
    ABLATION_POLICY_INVALID = "ablation_policy_invalid"
    PINE_OVERRIDE_INVALID = "pine_override_invalid"
    DIFFERENCE_POLICY_INVALID = "difference_policy_invalid"
    NEIGHBOR_AXIS_INVALID = "neighbor_axis_invalid"
    DUPLICATE_EXECUTION_KEY = "duplicate_execution_key"
    RESEARCH_ABLATION_PENDING = "research_ablation_pending"
    UNRESOLVED_CELL_CONFIG = "unresolved_cell_config"
    MISSING_COMPARISON_ARM = "missing_comparison_arm"
    MISSING_NEIGHBOR_AXIS = "missing_neighbor_axis"
    UNBOUND_SHARED_SIGNAL_STREAM = "unbound_shared_signal_stream"
    UNBOUND_PSEUDO_EVENT_STREAM = "unbound_pseudo_event_stream"
    UNREPRESENTABLE_ABLATION = "unrepresentable_ablation"
    EXECUTION_BINDING_PENDING = "execution_binding_pending"
    EXECUTION_BINDING_INVALID = "execution_binding_invalid"
    CERTIFIED_CATALOG_PENDING = "certified_catalog_pending"
    PARTITION_STAGE_BINDINGS_PENDING = "partition_stage_bindings_pending"
    EVALUATOR_PENDING = "evaluator_pending"


_CELL_RESOLUTION_CODES = frozenset(
    {
        CampaignBlockerCode.UNRESOLVED_CELL_CONFIG,
        CampaignBlockerCode.MISSING_COMPARISON_ARM,
        CampaignBlockerCode.MISSING_NEIGHBOR_AXIS,
        CampaignBlockerCode.UNREPRESENTABLE_ABLATION,
        CampaignBlockerCode.UNBOUND_SHARED_SIGNAL_STREAM,
        CampaignBlockerCode.UNBOUND_PSEUDO_EVENT_STREAM,
    }
)
_EXECUTION_RESOLUTION_CODES = frozenset(
    {
        CampaignBlockerCode.CERTIFIED_CATALOG_PENDING,
        CampaignBlockerCode.PARTITION_STAGE_BINDINGS_PENDING,
        CampaignBlockerCode.EVALUATOR_PENDING,
        CampaignBlockerCode.IDENTITY_UNRESOLVED,
    }
)
_REQUIRED_CELL_PENDING_CODES = {
    "5t-trend-directional-paired": frozenset(
        {CampaignBlockerCode.UNREPRESENTABLE_ABLATION, CampaignBlockerCode.MISSING_NEIGHBOR_AXIS}
    ),
    "5t-momentum-score-paired": frozenset(
        {CampaignBlockerCode.UNREPRESENTABLE_ABLATION, CampaignBlockerCode.MISSING_NEIGHBOR_AXIS}
    ),
    "5t-vol-scaling-paired": frozenset(
        {
            CampaignBlockerCode.UNREPRESENTABLE_ABLATION,
            CampaignBlockerCode.UNBOUND_SHARED_SIGNAL_STREAM,
            CampaignBlockerCode.MISSING_NEIGHBOR_AXIS,
        }
    ),
    "5t-rsi-divergence-paired": frozenset(
        {
            CampaignBlockerCode.UNREPRESENTABLE_ABLATION,
            CampaignBlockerCode.UNBOUND_PSEUDO_EVENT_STREAM,
            CampaignBlockerCode.MISSING_NEIGHBOR_AXIS,
        }
    ),
    "5t-mfi-divergence-paired": frozenset(
        {
            CampaignBlockerCode.UNREPRESENTABLE_ABLATION,
            CampaignBlockerCode.UNBOUND_PSEUDO_EVENT_STREAM,
            CampaignBlockerCode.MISSING_NEIGHBOR_AXIS,
        }
    ),
    "5t-relative-strength-paired": frozenset(
        {CampaignBlockerCode.UNREPRESENTABLE_ABLATION, CampaignBlockerCode.MISSING_NEIGHBOR_AXIS}
    ),
    "5t-regime-filter-paired": frozenset(
        {
            CampaignBlockerCode.UNREPRESENTABLE_ABLATION,
            CampaignBlockerCode.UNBOUND_SHARED_SIGNAL_STREAM,
            CampaignBlockerCode.MISSING_NEIGHBOR_AXIS,
        }
    ),
}


@dataclass(frozen=True, slots=True)
class CampaignBlocker:
    code: CampaignBlockerCode
    location: str
    message: str


@dataclass(frozen=True, slots=True)
class ResearchAblationLock:
    schema_version: str
    policy_id: str
    sha256: str

    @property
    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class CampaignComparison:
    kind: str
    treatment_arm_id: str
    control_arm_id: str
    estimand: str
    direction: ComparisonDirection


@dataclass(frozen=True, slots=True)
class HeldFixedPolicy:
    pine_inputs: str
    settings_fields: tuple[str, ...]
    replay_policy: bool


@dataclass(frozen=True, slots=True)
class CompiledCampaignArm:
    arm_id: str
    pine_overrides: tuple[tuple[str, InputValue], ...]
    research_ablation: ResearchAblationLock | None
    effective_settings: FiveToolSettings
    effective_settings_sha256: str
    config_bytes: bytes
    config_sha256: str


@dataclass(frozen=True, slots=True)
class CompiledCampaignNeighbor:
    axis_selector: str
    axis_value: int | float
    base_arm_id: str
    arm: CompiledCampaignArm


@dataclass(frozen=True, slots=True)
class CompiledCampaignCell:
    cell_id: str
    hypothesis_id: str
    role: str
    treatment: CompiledCampaignArm
    control: CompiledCampaignArm
    comparison: CampaignComparison
    allowed_differences: tuple[str, ...]
    held_fixed: HeldFixedPolicy
    neighbors: tuple[CompiledCampaignNeighbor, ...]
    ablation_policy_sha256: str
    replay_policy_sha256: str
    execution_sha256: str


@dataclass(frozen=True, slots=True)
class ExecutionRequestBinding:
    request_id: str
    dataset_id: str
    partition: str
    data_version: str
    source_id: str
    source_receipt_sha256: str


@dataclass(frozen=True, slots=True)
class EvaluatorBinding:
    schema_version: str
    evaluator_id: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ExecutionBindings:
    schema_version: str
    catalog_manifest_sha256: str
    partition_stage_map: tuple[tuple[str, str], ...]
    requests: tuple[ExecutionRequestBinding, ...]
    evaluator: EvaluatorBinding
    sha256: str

    def stage_for(self, partition: str) -> str:
        for candidate, stage in self.partition_stage_map:
            if candidate == partition:
                return stage
        raise KeyError(partition)


@dataclass(frozen=True, slots=True)
class CompiledTrialBlueprint:
    """Metadata-only trial input carrying exact-manifest and semantic campaign identities.

    The dual identities do not claim current trial-ID or verdict invariance. Future runner
    wiring must bind both the exact audit document and the normalized semantic plan.
    """

    campaign_id: str
    campaign_manifest_sha256: str
    campaign_sha256: str
    cell_id: str
    hypothesis_id: str
    arm_id: str
    stage: str
    strategy_id: str
    config_bytes: bytes
    config_digest: str
    code_commit: str
    criteria_digest: str
    evaluator_id: str
    evaluator_digest: str
    request: ExecutionRequestBinding


@dataclass(frozen=True, slots=True)
class CompiledCampaignPlan:
    """Campaign-atomic metadata plan; its reference arm is identity-only, never a trial."""

    schema_version: str
    campaign_id: str
    manifest_sha256: str
    strategy_id: str
    replay_policy: FiveToolReplayPolicy
    replay_policy_sha256: str
    reference_arm: CompiledCampaignArm
    cells: tuple[CompiledCampaignCell, ...]
    execution_bindings: ExecutionBindings
    trials: tuple[CompiledTrialBlueprint, ...]
    campaign_policy_sha256: str
    campaign_sha256: str


@dataclass(frozen=True, slots=True)
class CampaignReadinessReport:
    manifest_sha256: str | None
    blockers: tuple[CampaignBlocker, ...]
    plan: CompiledCampaignPlan | None

    @property
    def ready(self) -> bool:
        return not self.blockers and self.plan is not None


@dataclass(frozen=True, slots=True)
class _ArmDraft:
    arm_id: str
    pine_overrides: tuple[tuple[str, InputValue], ...]
    research_ablation: ResearchAblationLock | None
    effective_settings: FiveToolSettings


@dataclass(frozen=True, slots=True)
class _GlobalIdentity:
    campaign_id: str
    strategy_id: str
    pine_source_digest: str | None
    input_contract_digest: str | None
    semantic_config_digest: str | None
    code_commit: str | None
    criteria_digest: str | None
    dataset_id: str | None
    dataset_release_digest: str | None
    primary_instruments: tuple[str, ...]
    benchmark: str
    timeframe: str
    accessible_partitions: frozenset[str]
    holdout_identities: frozenset[tuple[str, str]]
    holdout_partitions: frozenset[str]


class _CompileFailure(ValueError):
    def __init__(self, code: CampaignBlockerCode, location: str, message: str) -> None:
        super().__init__(message)
        self.blocker = CampaignBlocker(code, location, message)


def compile_campaign_manifest(manifest: Mapping[str, object]) -> CampaignReadinessReport:
    """Report v2 readiness; unresolved canonical cell semantics categorically suppress plans."""

    return _compile_campaign_manifest(manifest, allow_synthetic_resolved_cells=False)


def _compile_campaign_manifest_for_tests(
    manifest: Mapping[str, object],
) -> CampaignReadinessReport:
    """Exercise deterministic plan compilation without waiving any non-semantic safety gate."""

    return _compile_campaign_manifest(manifest, allow_synthetic_resolved_cells=True)


def _compile_campaign_manifest(
    manifest: Mapping[str, object],
    *,
    allow_synthetic_resolved_cells: bool,
) -> CampaignReadinessReport:

    blockers: list[CampaignBlocker] = []
    try:
        document, manifest_sha256 = _canonical_manifest(manifest)
        _require_literal(
            document.get("schema_version"),
            CAMPAIGN_SCHEMA_VERSION,
            "schema_version",
        )
        _exact_keys(document, _ROOT_KEYS, "$", CampaignBlockerCode.MANIFEST_SCHEMA)
        _require_literal(document.get("campaign_id"), CAMPAIGN_ID, "campaign_id")
        _require_literal(
            document.get("supersedes_campaign_id"),
            SUPERSEDES_CAMPAIGN_ID,
            "supersedes_campaign_id",
        )
        _validate_campaign_metadata(document)
    except _CompileFailure as error:
        return CampaignReadinessReport(None, (error.blocker,), None)

    identity: _GlobalIdentity | None = None
    history_start: datetime | None = None
    replay_policy: FiveToolReplayPolicy | None = None
    replay_digest: str | None = None
    campaign_policy_digest: str | None = None
    reference: _ArmDraft | None = None
    execution_bindings: ExecutionBindings | None = None
    cells: tuple[CompiledCampaignCell, ...] = ()

    try:
        identity = _parse_global_identity(document, blockers)
        history_start = _history_start(document)
    except _CompileFailure as error:
        blockers.append(error.blocker)

    try:
        replay_policy, replay_digest = _parse_replay_policy(document.get("replay_policy"))
    except _CompileFailure as error:
        blockers.append(error.blocker)
    if replay_policy is not None:
        try:
            campaign_policy_digest = _validate_execution_metadata(document, replay_policy)
        except _CompileFailure as error:
            blockers.append(error.blocker)

    base_settings: FiveToolSettings | None = None
    if history_start is not None:
        try:
            base_settings = FiveToolSettings.defaults(history_start_utc=history_start)
        except (FiveToolInputError, OSError, ValueError) as error:
            blockers.append(
                CampaignBlocker(
                    CampaignBlockerCode.PINE_OVERRIDE_INVALID,
                    "reference_arm.pine_overrides",
                    f"Five-Tool base settings could not be constructed: {error}",
                )
            )

    if identity is not None and base_settings is not None:
        try:
            _validate_source_bound_identity(identity, base_settings, blockers)
        except (OSError, ValueError) as error:
            blockers.append(
                CampaignBlocker(
                    CampaignBlockerCode.IDENTITY_UNRESOLVED,
                    "strategy",
                    f"source-bound Five-Tool identities could not be verified: {error}",
                )
            )

    if base_settings is not None:
        try:
            reference = _parse_arm(
                document.get("reference_arm"),
                location="reference_arm",
                base_settings=base_settings,
            )
            if reference.research_ablation is not None:
                blockers.append(
                    CampaignBlocker(
                        CampaignBlockerCode.UNREPRESENTABLE_ABLATION,
                        "reference_arm.research_ablation",
                        "the campaign compiler cannot execute a non-Pine research ablation",
                    )
                )
        except _CompileFailure as error:
            blockers.append(error.blocker)

    hypotheses: frozenset[str] = frozenset()
    try:
        hypotheses = _hypotheses(document.get("hypothesis_ids"))
    except _CompileFailure as error:
        blockers.append(error.blocker)

    if reference is not None and replay_digest is not None:
        try:
            cells = _parse_cells(
                document.get("campaign_cells"),
                hypotheses=hypotheses,
                reference=reference,
                replay_digest=replay_digest,
                blockers=blockers,
                allow_synthetic_resolved_cells=allow_synthetic_resolved_cells,
            )
        except _CompileFailure as error:
            blockers.append(error.blocker)

    try:
        execution_bindings = _parse_execution_bindings(
            document.get("execution_bindings"),
            dataset_id=identity.dataset_id if identity is not None else None,
            dataset_release_digest=(
                identity.dataset_release_digest if identity is not None else None
            ),
            accessible_partitions=(
                identity.accessible_partitions if identity is not None else frozenset()
            ),
            holdout_identities=(
                identity.holdout_identities if identity is not None else frozenset()
            ),
            holdout_partitions=(
                identity.holdout_partitions if identity is not None else frozenset()
            ),
            blockers=blockers,
        )
    except _CompileFailure as error:
        blockers.append(error.blocker)

    if blockers:
        return CampaignReadinessReport(
            manifest_sha256,
            tuple(_deduplicate_blockers(blockers)),
            None,
        )

    assert identity is not None
    assert identity.code_commit is not None
    assert identity.criteria_digest is not None
    assert replay_policy is not None and replay_digest is not None
    assert campaign_policy_digest is not None
    assert reference is not None
    assert execution_bindings is not None
    compiled_reference = _compile_arm(reference, replay_digest)
    campaign_sha256 = _canonical_digest(
        {
            "schema_version": COMPILED_CAMPAIGN_SCHEMA_VERSION,
            "campaign_id": identity.campaign_id,
            "strategy_id": identity.strategy_id,
            "pine_source_sha256": identity.pine_source_digest,
            "input_contract_sha256": identity.input_contract_digest,
            "semantic_config_sha256": identity.semantic_config_digest,
            "code_commit": identity.code_commit,
            "criteria_sha256": identity.criteria_digest,
            "dataset_id": identity.dataset_id,
            "dataset_release_sha256": identity.dataset_release_digest,
            "primary_instruments": identity.primary_instruments,
            "benchmark": identity.benchmark,
            "timeframe": identity.timeframe,
            "reference_config_sha256": compiled_reference.config_sha256,
            "cell_execution_sha256": [cell.execution_sha256 for cell in cells],
            "replay_policy_sha256": replay_digest,
            "campaign_policy_sha256": campaign_policy_digest,
            "execution_bindings_sha256": execution_bindings.sha256,
        }
    )
    trials = _trial_blueprints(
        identity=identity,
        manifest_sha256=manifest_sha256,
        campaign_sha256=campaign_sha256,
        cells=cells,
        bindings=execution_bindings,
    )
    plan = CompiledCampaignPlan(
        schema_version=COMPILED_CAMPAIGN_SCHEMA_VERSION,
        campaign_id=identity.campaign_id,
        manifest_sha256=manifest_sha256,
        strategy_id=identity.strategy_id,
        replay_policy=replay_policy,
        replay_policy_sha256=replay_digest,
        reference_arm=compiled_reference,
        cells=cells,
        execution_bindings=execution_bindings,
        trials=trials,
        campaign_policy_sha256=campaign_policy_digest,
        campaign_sha256=campaign_sha256,
    )
    return CampaignReadinessReport(manifest_sha256, (), plan)


def _canonical_manifest(manifest: Mapping[str, object]) -> tuple[dict[str, object], str]:
    if not isinstance(manifest, Mapping):
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "$",
            "campaign manifest must be a mapping",
        )
    document = dict(manifest)
    try:
        _require_json_value(document, location="$")
        canonical = _canonical_json(document)
        decoded = json.loads(canonical)
    except (TypeError, ValueError) as error:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "$",
            f"campaign manifest is not canonical JSON: {error}",
        ) from error
    assert isinstance(decoded, dict)
    return cast(dict[str, object], decoded), hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_global_identity(
    document: dict[str, object], blockers: list[CampaignBlocker]
) -> _GlobalIdentity:
    campaign_id = _nonempty_string(document.get("campaign_id"), "campaign_id")
    state = _nonempty_string(document.get("execution_state"), "execution_state")
    if state != EXECUTION_READY:
        blockers.append(
            CampaignBlocker(
                CampaignBlockerCode.EXECUTION_STATE,
                "execution_state",
                f"campaign execution state is {state!r}, not {EXECUTION_READY!r}",
            )
        )

    declared = _array(
        document.get("blocked_before_first_data_read"), "blocked_before_first_data_read"
    )
    for index, item in enumerate(declared):
        message = _nonempty_string(item, f"blocked_before_first_data_read[{index}]")
        blockers.append(
            CampaignBlocker(
                CampaignBlockerCode.DECLARED_BLOCKER,
                f"blocked_before_first_data_read[{index}]",
                message,
            )
        )

    strategy = _object(document.get("strategy"), "strategy")
    _exact_keys(
        strategy,
        {"strategy_id", "version", "scope", "pine_source", "input_contract", "semantic_config"},
        "strategy",
        CampaignBlockerCode.MANIFEST_SCHEMA,
    )
    strategy_id = _nonempty_string(strategy.get("strategy_id"), "strategy.strategy_id")
    _require_literal(strategy_id, "five_tool_confluence_v3_6", "strategy.strategy_id")
    _require_literal(strategy.get("version"), "3.6", "strategy.version")
    _require_literal(strategy.get("scope"), "research-only", "strategy.scope")
    strategy_digests: dict[str, str | None] = {}
    for name in ("pine_source", "input_contract", "semantic_config"):
        lock = _object(strategy.get(name), f"strategy.{name}")
        if name == "pine_source":
            _exact_keys(
                lock,
                {"path", "sha256"},
                f"strategy.{name}",
                CampaignBlockerCode.MANIFEST_SCHEMA,
            )
            _require_literal(
                lock.get("path"),
                "research/pine/00_five_tool_confluence_aio.pine",
                "strategy.pine_source.path",
            )
        else:
            _exact_keys(
                lock,
                {"path", "sha256", "digest_scope", "status", "required_before_execution"},
                f"strategy.{name}",
                CampaignBlockerCode.MANIFEST_SCHEMA,
            )
            _require_literal(
                lock.get("path"),
                "specs/five_tool_confluence_v3_6.yaml",
                f"strategy.{name}.path",
            )
            _require_literal(lock.get("status"), "resolved", f"strategy.{name}.status")
            _nonempty_string(lock.get("digest_scope"), f"strategy.{name}.digest_scope")
            if lock.get("required_before_execution") is not True:
                raise _failure(
                    CampaignBlockerCode.MANIFEST_SCHEMA,
                    f"strategy.{name}.required_before_execution",
                    f"strategy {name} identity must be required before execution",
                )
        digest = lock.get("sha256")
        status = lock.get("status", "resolved" if name == "pine_source" else None)
        if (
            not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or (name != "pine_source" and status != "resolved")
        ):
            blockers.append(
                CampaignBlocker(
                    CampaignBlockerCode.IDENTITY_UNRESOLVED,
                    f"strategy.{name}",
                    f"strategy {name} identity is unresolved",
                )
            )
            strategy_digests[name] = None
        else:
            strategy_digests[name] = digest

    code_lock = _object(document.get("code_commit_lock"), "code_commit_lock")
    _exact_keys(
        code_lock,
        {"git_commit", "status", "required_before_execution"},
        "code_commit_lock",
        CampaignBlockerCode.MANIFEST_SCHEMA,
    )
    if code_lock.get("required_before_execution") is not True:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "code_commit_lock.required_before_execution",
            "code commit identity must be required before execution",
        )
    code_commit = code_lock.get("git_commit")
    if (
        not isinstance(code_commit, str)
        or _COMMIT.fullmatch(code_commit) is None
        or code_lock.get("status") != "resolved"
    ):
        blockers.append(
            CampaignBlocker(
                CampaignBlockerCode.IDENTITY_UNRESOLVED,
                "code_commit_lock",
                "code commit lock is unresolved",
            )
        )
        code_commit = None

    criteria_lock = _object(document.get("criteria_lock"), "criteria_lock")
    _exact_keys(
        criteria_lock,
        {"path", "sha256", "status", "required_before_execution"},
        "criteria_lock",
        CampaignBlockerCode.MANIFEST_SCHEMA,
    )
    if criteria_lock.get("path") != document.get("criteria_document"):
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "criteria_lock.path",
            "criteria lock path must match criteria_document",
        )
    if criteria_lock.get("required_before_execution") is not True:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "criteria_lock.required_before_execution",
            "criteria identity must be required before execution",
        )
    criteria_digest = _resolved_sha_lock(criteria_lock, "criteria_lock", blockers)

    data = _object(document.get("data"), "data")
    _exact_keys(data, _DATA_KEYS, "data", CampaignBlockerCode.MANIFEST_SCHEMA)
    dataset_lock = _object(data.get("dataset_version_lock"), "data.dataset_version_lock")
    _exact_keys(
        dataset_lock,
        {"dataset_id", "sha256", "status", "required_before_execution"},
        "data.dataset_version_lock",
        CampaignBlockerCode.MANIFEST_SCHEMA,
    )
    if dataset_lock.get("required_before_execution") is not True:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "data.dataset_version_lock.required_before_execution",
            "dataset identity must be required before execution",
        )
    dataset_id = _identity_string(
        dataset_lock.get("dataset_id"), "data.dataset_version_lock.dataset_id"
    )
    dataset_release_digest = _resolved_sha_lock(
        dataset_lock,
        "data.dataset_version_lock",
        blockers,
    )
    primary = tuple(
        _nonempty_string(item, f"data.primary_instruments[{index}]").strip()
        for index, item in enumerate(
            _array(data.get("primary_instruments"), "data.primary_instruments")
        )
    )
    if (
        len(primary) < 3
        or len(primary) != len(set(primary))
        or any(symbol != symbol.upper() for symbol in primary)
    ):
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "data.primary_instruments",
            "primary instruments must contain at least three unique canonical symbols",
        )
    benchmark = _nonempty_string(data.get("benchmark"), "data.benchmark").strip()
    if benchmark not in primary:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "data.benchmark",
            "benchmark must be one of the primary instruments",
        )
    timeframe = _nonempty_string(data.get("timeframe"), "data.timeframe")
    accessible_items = [
        _identity_string(item, f"data.accessible_partitions[{index}]")
        for index, item in enumerate(
            _array(data.get("accessible_partitions"), "data.accessible_partitions")
        )
    ]
    accessible = set(accessible_items)
    if not accessible:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "data.accessible_partitions",
            "at least one accessible partition is required",
        )
    normalized_accessible = {_normalized_identity(item) for item in accessible}
    if len(accessible) != len(accessible_items) or len(normalized_accessible) != len(accessible):
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "data.accessible_partitions",
            "accessible partition identities must be canonically unique",
        )
    if normalized_accessible & _ORDINARY_HOLDOUT_PARTITIONS:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "data.accessible_partitions",
            "holdout partitions cannot be ordinarily accessible",
        )
    holdout_identities, holdout_partitions = _validate_declared_holdouts(data)
    if normalized_accessible & holdout_partitions:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "data.accessible_partitions",
            "accessible partitions cannot alias any declared holdout partition",
        )
    return _GlobalIdentity(
        campaign_id=campaign_id,
        strategy_id=strategy_id,
        pine_source_digest=strategy_digests["pine_source"],
        input_contract_digest=strategy_digests["input_contract"],
        semantic_config_digest=strategy_digests["semantic_config"],
        code_commit=code_commit,
        criteria_digest=criteria_digest,
        dataset_id=dataset_id,
        dataset_release_digest=dataset_release_digest,
        primary_instruments=primary,
        benchmark=benchmark,
        timeframe=timeframe,
        accessible_partitions=frozenset(accessible),
        holdout_identities=holdout_identities,
        holdout_partitions=holdout_partitions,
    )


def _validate_source_bound_identity(
    identity: _GlobalIdentity,
    settings: FiveToolSettings,
    blockers: list[CampaignBlocker],
) -> None:
    expected = {
        "strategy.pine_source.sha256": load_contract().pine.source_sha256,
        "strategy.input_contract.sha256": settings.contract_digest,
        "strategy.semantic_config.sha256": semantic_contract_digest(),
    }
    declared = {
        "strategy.pine_source.sha256": identity.pine_source_digest,
        "strategy.input_contract.sha256": identity.input_contract_digest,
        "strategy.semantic_config.sha256": identity.semantic_config_digest,
    }
    for location, expected_digest in expected.items():
        if declared[location] != expected_digest:
            blockers.append(
                CampaignBlocker(
                    CampaignBlockerCode.IDENTITY_UNRESOLVED,
                    location,
                    f"{location} does not match the source-bound runtime identity",
                )
            )
    settings_benchmark = settings.text("bench_sym").rsplit(":", maxsplit=1)[-1].strip().upper()
    if settings_benchmark != identity.benchmark:
        blockers.append(
            CampaignBlocker(
                CampaignBlockerCode.IDENTITY_UNRESOLVED,
                "data.benchmark",
                "data benchmark does not match the Pine bench_sym ticker",
            )
        )


def _validate_declared_holdouts(
    data: dict[str, object],
) -> tuple[frozenset[tuple[str, str]], frozenset[str]]:
    declared = _array(data.get("declared_holdouts"), "data.declared_holdouts")
    if not declared:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "data.declared_holdouts",
            "at least one declared holdout is required",
        )
    seen: set[tuple[str, str]] = set()
    for index, value in enumerate(declared):
        location = f"data.declared_holdouts[{index}]"
        holdout = _object(value, location)
        _exact_keys(
            holdout,
            {"dataset_id", "partition", "start_utc", "status", "ordinary_research_access"},
            location,
            CampaignBlockerCode.MANIFEST_SCHEMA,
        )
        dataset_id = _identity_string(holdout.get("dataset_id"), f"{location}.dataset_id")
        partition = _identity_string(holdout.get("partition"), f"{location}.partition")
        _nonempty_string(holdout.get("status"), f"{location}.status")
        _utc_timestamp(holdout.get("start_utc"), f"{location}.start_utc")
        _require_literal(
            holdout.get("ordinary_research_access"),
            "forbidden",
            f"{location}.ordinary_research_access",
        )
        identity = _normalized_identity(dataset_id), _normalized_identity(partition)
        if identity in seen:
            raise _failure(
                CampaignBlockerCode.MANIFEST_SCHEMA,
                location,
                "declared holdout identities must be unique",
            )
        seen.add(identity)
    contamination = _string_set(data.get("known_contamination"), "data.known_contamination")
    if not contamination:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "data.known_contamination",
            "known contamination must be explicitly enumerated",
        )
    return frozenset(seen), frozenset(partition for _, partition in seen)


def _validate_campaign_metadata(document: dict[str, object]) -> None:
    _nonempty_string(document.get("purpose"), "purpose")
    _nonempty_string(document.get("criteria_document"), "criteria_document")
    created = _nonempty_string(document.get("created_at_utc"), "created_at_utc")
    try:
        parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError as error:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "created_at_utc",
            "created_at_utc must be ISO-8601",
        ) from error
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "created_at_utc",
            "created_at_utc must identify a UTC instant",
        )
    if document.get("performance_claims") != []:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "performance_claims",
            "a preregistered campaign cannot contain performance claims",
        )
    _require_literal(document.get("promotion_authority"), "none", "promotion_authority")
    execution_state = document.get("execution_state")
    if execution_state not in {"blocked_until_identity_locks_resolve", EXECUTION_READY}:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "execution_state",
            "execution state is unsupported",
        )
    declared_blockers = _array(
        document.get("blocked_before_first_data_read"), "blocked_before_first_data_read"
    )
    if execution_state == EXECUTION_READY and declared_blockers:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "blocked_before_first_data_read",
            "an execution-ready manifest cannot retain declared blockers",
        )
    if execution_state != EXECUTION_READY and not declared_blockers:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "blocked_before_first_data_read",
            "a blocked manifest must enumerate blockers",
        )
    reference = _object(document.get("reference_arm"), "reference_arm")
    _exact_keys(
        reference,
        {"arm_id", "pine_overrides", "research_ablation"},
        "reference_arm",
        CampaignBlockerCode.MANIFEST_SCHEMA,
    )
    _require_literal(reference.get("arm_id"), "5t-full-default-reference", "reference_arm.arm_id")
    if reference.get("pine_overrides") != {}:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "reference_arm",
            "reference arm must bind the Pine defaults",
        )
    for name in ("costs", "fill_policy", "statistics", "trial_accounting"):
        if not _object(document.get(name), name):
            raise _failure(
                CampaignBlockerCode.MANIFEST_SCHEMA,
                name,
                f"{name} policy must be non-empty",
            )
    changes = _string_set(
        document.get("identity_changes_that_invalidate_campaign"),
        "identity_changes_that_invalidate_campaign",
    )
    if changes != _INVALIDATING_IDENTITIES:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "identity_changes_that_invalidate_campaign",
            "identity invalidations must be exact and complete",
        )


def _validate_execution_metadata(
    document: dict[str, object], replay_policy: FiveToolReplayPolicy
) -> str:
    fill_policy = _object(document.get("fill_policy"), "fill_policy")
    _exact_keys(
        fill_policy,
        _FILL_POLICY_KEYS,
        "fill_policy",
        CampaignBlockerCode.MANIFEST_SCHEMA,
    )
    if not fill_policy or any(
        not isinstance(value, str) or not value.strip() for value in fill_policy.values()
    ):
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "fill_policy",
            "fill policy must be non-empty and fully specified",
        )
    costs = _object(document.get("costs"), "costs")
    _exact_keys(
        costs,
        {
            "commission_bps_per_fill",
            "slippage_ticks_per_fill",
            "spread_policy",
            "funding_borrow_model_data_costs",
            "stress",
        },
        "costs",
        CampaignBlockerCode.MANIFEST_SCHEMA,
    )
    commission = _finite_nonnegative_number(
        costs.get("commission_bps_per_fill"), "costs.commission_bps_per_fill"
    )
    slippage = _integer(costs.get("slippage_ticks_per_fill"), "costs.slippage_ticks_per_fill")
    if slippage < 0:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "costs.slippage_ticks_per_fill",
            "base slippage must be non-negative",
        )
    if (
        commission != replay_policy.commission_bps_per_fill
        or slippage != replay_policy.slippage_ticks_per_fill
    ):
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "costs",
            "base costs must exactly match the replay policy",
        )
    _nonempty_string(costs.get("spread_policy"), "costs.spread_policy")
    _nonempty_string(
        costs.get("funding_borrow_model_data_costs"),
        "costs.funding_borrow_model_data_costs",
    )
    stress = _object(costs.get("stress"), "costs.stress")
    _exact_keys(
        stress,
        {"commission_bps_per_fill", "slippage_ticks_per_fill", "require_positive_after_stress"},
        "costs.stress",
        CampaignBlockerCode.MANIFEST_SCHEMA,
    )
    stress_commission = _finite_nonnegative_number(
        stress.get("commission_bps_per_fill"), "costs.stress.commission_bps_per_fill"
    )
    stress_slippage = _integer(
        stress.get("slippage_ticks_per_fill"), "costs.stress.slippage_ticks_per_fill"
    )
    if stress_slippage < 0 or stress.get("require_positive_after_stress") is not True:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "costs.stress",
            "stress costs must be non-negative and require a positive result",
        )
    accounting = _object(document.get("trial_accounting"), "trial_accounting")
    _exact_keys(
        accounting,
        _TRIAL_ACCOUNTING_KEYS,
        "trial_accounting",
        CampaignBlockerCode.MANIFEST_SCHEMA,
    )
    required_accounting: dict[str, object] = {
        "record_kind_start": "trial_started",
        "start_must_precede_reader": True,
        "reader_and_evaluator_failures_count": True,
        "candidate_order_or_display_rename_changes_verdict": False,
    }
    for key, expected in required_accounting.items():
        if accounting.get(key) != expected:
            raise _failure(
                CampaignBlockerCode.MANIFEST_SCHEMA,
                f"trial_accounting.{key}",
                "trial-accounting safety control is missing or changed",
            )
    _nonempty_string(accounting.get("multiplicity"), "trial_accounting.multiplicity")
    statistics = _object(document.get("statistics"), "statistics")
    _exact_keys(
        statistics,
        _STATISTICS_KEYS,
        "statistics",
        CampaignBlockerCode.MANIFEST_SCHEMA,
    )
    exact_integers = {
        "instruments_required": 3,
        "materially_different_regimes_required": 2,
    }
    for key, expected in exact_integers.items():
        if _integer(statistics.get(key), f"statistics.{key}") != expected:
            raise _failure(
                CampaignBlockerCode.MANIFEST_SCHEMA,
                f"statistics.{key}",
                f"statistics.{key} must equal {expected}",
            )
    exact_thresholds = {
        "deflated_sharpe_probability_min": 0.95,
        "fwer_or_fdr_q_max": 0.05,
        "probability_backtest_overfit_max": 0.1,
        "parameter_neighbor_pass_fraction_min": 0.67,
    }
    normalized_thresholds: dict[str, float] = {}
    for key, expected in exact_thresholds.items():
        actual = _number(statistics.get(key), f"statistics.{key}")
        if not 0.0 <= actual <= 1.0 or actual != expected:
            raise _failure(
                CampaignBlockerCode.MANIFEST_SCHEMA,
                f"statistics.{key}",
                f"statistics.{key} must equal {expected}",
            )
        normalized_thresholds[key] = actual
    string_policies = _STATISTICS_KEYS - set(exact_integers) - set(exact_thresholds)
    for key in string_policies:
        _nonempty_string(statistics.get(key), f"statistics.{key}")
    normalized_statistics = {
        **{key: statistics[key] for key in string_policies},
        **exact_integers,
        **normalized_thresholds,
    }
    normalized_costs = {
        "commission_bps_per_fill": commission,
        "slippage_ticks_per_fill": slippage,
        "spread_policy": costs["spread_policy"],
        "funding_borrow_model_data_costs": costs["funding_borrow_model_data_costs"],
        "stress": {
            "commission_bps_per_fill": stress_commission,
            "slippage_ticks_per_fill": stress_slippage,
            "require_positive_after_stress": True,
        },
    }
    return _canonical_digest(
        {
            "fill_policy": fill_policy,
            "costs": normalized_costs,
            "statistics": normalized_statistics,
            "trial_accounting": accounting,
        }
    )


def _resolved_sha_lock(
    lock: dict[str, object],
    location: str,
    blockers: list[CampaignBlocker],
) -> str | None:
    digest = lock.get("sha256")
    if (
        not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or lock.get("status") != "resolved"
    ):
        blockers.append(
            CampaignBlocker(
                CampaignBlockerCode.IDENTITY_UNRESOLVED,
                location,
                f"{location} SHA-256 lock is unresolved",
            )
        )
        return None
    return digest


def _history_start(document: dict[str, object]) -> datetime:
    data = _object(document.get("data"), "data")
    return _utc_timestamp(data.get("history_start_utc"), "data.history_start_utc")


def _utc_timestamp(value: object, location: str) -> datetime:
    raw = _nonempty_string(value, location)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            location,
            f"{location} must be ISO-8601",
        ) from error
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            location,
            f"{location} must identify a UTC instant",
        )
    return parsed.astimezone(UTC)


def _parse_replay_policy(value: object) -> tuple[FiveToolReplayPolicy, str]:
    location = "replay_policy"
    lock = _object(value, location, code=CampaignBlockerCode.REPLAY_POLICY_INVALID)
    _exact_keys(lock, {"canonical", "sha256"}, location, CampaignBlockerCode.REPLAY_POLICY_INVALID)
    canonical = _object(
        lock.get("canonical"),
        f"{location}.canonical",
        code=CampaignBlockerCode.REPLAY_POLICY_INVALID,
    )
    expected_keys = set(FiveToolReplayPolicy().canonical_payload)
    _exact_keys(
        canonical,
        expected_keys,
        f"{location}.canonical",
        CampaignBlockerCode.REPLAY_POLICY_INVALID,
    )
    try:
        initial = _number(canonical.get("initial_equity"), f"{location}.initial_equity")
        commission = _number(
            canonical.get("commission_bps_per_fill"),
            f"{location}.commission_bps_per_fill",
        )
        slippage = _integer(
            canonical.get("slippage_ticks_per_fill"),
            f"{location}.slippage_ticks_per_fill",
        )
        target_slippage = _boolean(
            canonical.get("apply_slippage_to_target_limits"),
            f"{location}.apply_slippage_to_target_limits",
        )
        policy = FiveToolReplayPolicy(
            initial_equity=initial,
            parameter_variant=_nonempty_string(
                canonical.get("parameter_variant"), f"{location}.parameter_variant"
            ),
            fill_policy=FillPolicy(
                _nonempty_string(canonical.get("fill_policy"), f"{location}.fill_policy")
            ),
            commission_bps_per_fill=commission,
            slippage_ticks_per_fill=slippage,
            apply_slippage_to_target_limits=target_slippage,
            terminal_position_policy=TerminalPositionPolicy(
                _nonempty_string(
                    canonical.get("terminal_position_policy"),
                    f"{location}.terminal_position_policy",
                )
            ),
        )
    except (ReplayInputError, ValueError) as error:
        raise _failure(
            CampaignBlockerCode.REPLAY_POLICY_INVALID,
            location,
            f"replay policy is invalid: {error}",
        ) from error
    if _canonical_json(canonical) != _canonical_json(policy.canonical_payload):
        raise _failure(
            CampaignBlockerCode.REPLAY_POLICY_INVALID,
            f"{location}.canonical",
            "replay canonical payload does not exactly match the adapter policy",
        )
    digest = _sha256(lock.get("sha256"), f"{location}.sha256")
    if digest != policy.digest:
        raise _failure(
            CampaignBlockerCode.REPLAY_POLICY_INVALID,
            f"{location}.sha256",
            "replay policy SHA-256 does not match its canonical payload",
        )
    return policy, digest


def _hypotheses(value: object) -> frozenset[str]:
    values = _array(value, "hypothesis_ids")
    hypotheses = tuple(
        _nonempty_string(item, f"hypothesis_ids[{index}]") for index, item in enumerate(values)
    )
    expected = frozenset(_EXPECTED_CELL_HYPOTHESES.values())
    if len(hypotheses) != len(set(hypotheses)) or frozenset(hypotheses) != expected:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "hypothesis_ids",
            "hypothesis IDs must contain the exact six v2 component hypotheses",
        )
    return frozenset(hypotheses)


def _parse_arm(
    value: object,
    *,
    location: str,
    base_settings: FiveToolSettings,
) -> _ArmDraft:
    arm = _object(value, location, code=CampaignBlockerCode.ABLATION_POLICY_INVALID)
    _exact_keys(
        arm,
        {"arm_id", "pine_overrides", "research_ablation"},
        location,
        CampaignBlockerCode.ABLATION_POLICY_INVALID,
    )
    arm_id = _nonempty_string(arm.get("arm_id"), f"{location}.arm_id")
    overrides = _pine_overrides(arm.get("pine_overrides"), f"{location}.pine_overrides")
    try:
        effective = _settings_with_overrides(base_settings, dict(overrides))
    except (FiveToolInputError, ValueError) as error:
        raise _failure(
            CampaignBlockerCode.PINE_OVERRIDE_INVALID,
            f"{location}.pine_overrides",
            str(error),
        ) from error
    effective_values = dict(effective.inputs)
    normalized_overrides = tuple((name, effective_values[name]) for name, _ in overrides)
    research = _parse_research_ablation(
        arm.get("research_ablation"),
        f"{location}.research_ablation",
    )
    return _ArmDraft(arm_id, normalized_overrides, research, effective)


def _parse_research_ablation(
    value: object,
    location: str,
) -> ResearchAblationLock | None:
    if value is None:
        return None
    lock = _object(value, location, code=CampaignBlockerCode.ABLATION_POLICY_INVALID)
    _exact_keys(
        lock,
        {"schema_version", "policy_id", "sha256"},
        location,
        CampaignBlockerCode.ABLATION_POLICY_INVALID,
    )
    _require_literal(
        lock.get("schema_version"),
        RESEARCH_ABLATION_SCHEMA_VERSION,
        f"{location}.schema_version",
        code=CampaignBlockerCode.ABLATION_POLICY_INVALID,
    )
    return ResearchAblationLock(
        schema_version=RESEARCH_ABLATION_SCHEMA_VERSION,
        policy_id=_nonempty_string(lock.get("policy_id"), f"{location}.policy_id"),
        sha256=_sha256(lock.get("sha256"), f"{location}.sha256"),
    )


def _parse_cells(
    value: object,
    *,
    hypotheses: frozenset[str],
    reference: _ArmDraft,
    replay_digest: str,
    blockers: list[CampaignBlocker],
    allow_synthetic_resolved_cells: bool,
) -> tuple[CompiledCampaignCell, ...]:
    raw_cells = _array(value, "campaign_cells")
    if not raw_cells:
        blockers.append(
            CampaignBlocker(
                CampaignBlockerCode.MANIFEST_SCHEMA,
                "campaign_cells",
                "campaign cells must be non-empty",
            )
        )
        return ()
    seen: set[str] = set()
    covered: set[str] = set()
    compiled: list[CompiledCampaignCell] = []
    for index, raw in enumerate(raw_cells):
        location = f"campaign_cells[{index}]"
        try:
            cell = _object(raw, location, code=CampaignBlockerCode.ABLATION_POLICY_INVALID)
            _exact_keys(
                cell,
                {"cell_id", "hypothesis_id", "role", "ablation_policy"},
                location,
                CampaignBlockerCode.ABLATION_POLICY_INVALID,
            )
            cell_id = _nonempty_string(cell.get("cell_id"), f"{location}.cell_id")
            if cell_id in seen:
                raise _failure(
                    CampaignBlockerCode.ABLATION_POLICY_INVALID,
                    f"{location}.cell_id",
                    f"duplicate campaign cell ID {cell_id!r}",
                )
            seen.add(cell_id)
            hypothesis = _nonempty_string(cell.get("hypothesis_id"), f"{location}.hypothesis_id")
            expected_hypothesis = _EXPECTED_CELL_HYPOTHESES.get(cell_id)
            if expected_hypothesis is None or hypothesis != expected_hypothesis:
                raise _failure(
                    CampaignBlockerCode.ABLATION_POLICY_INVALID,
                    f"{location}.hypothesis_id",
                    "campaign cell ID and hypothesis do not match the frozen v2 topology",
                )
            covered.add(hypothesis)
            role = _nonempty_string(cell.get("role"), f"{location}.role")
            resolved = _parse_cell_policy(
                cell.get("ablation_policy"),
                location=f"{location}.ablation_policy",
                cell_id=cell_id,
                hypothesis_id=hypothesis,
                role=role,
                reference=reference,
                replay_digest=replay_digest,
                blockers=blockers,
                allow_synthetic_resolved_cells=allow_synthetic_resolved_cells,
            )
            if resolved is not None:
                compiled.append(resolved)
        except _CompileFailure as error:
            blockers.append(error.blocker)
    for hypothesis in sorted(hypotheses - covered):
        blockers.append(
            CampaignBlocker(
                CampaignBlockerCode.ABLATION_POLICY_INVALID,
                "campaign_cells",
                f"hypothesis {hypothesis!r} has no paired campaign cell",
            )
        )
    if seen != set(_EXPECTED_CELL_HYPOTHESES):
        blockers.append(
            CampaignBlocker(
                CampaignBlockerCode.ABLATION_POLICY_INVALID,
                "campaign_cells",
                "campaign cells must contain the exact seven v2 paired comparisons",
            )
        )
    result = tuple(sorted(compiled, key=lambda item: item.cell_id))
    _reject_duplicate_execution_configs(result, blockers)
    return result


def _parse_cell_policy(
    value: object,
    *,
    location: str,
    cell_id: str,
    hypothesis_id: str,
    role: str,
    reference: _ArmDraft,
    replay_digest: str,
    blockers: list[CampaignBlocker],
    allow_synthetic_resolved_cells: bool,
) -> CompiledCampaignCell | None:
    policy = _object(value, location, code=CampaignBlockerCode.ABLATION_POLICY_INVALID)
    expected_keys = {
        "schema_version",
        "status",
        "treatment",
        "control",
        "comparison",
        "allowed_differences",
        "held_fixed",
        "neighbor_axes",
        "resolution_blockers",
    }
    _exact_keys(policy, expected_keys, location, CampaignBlockerCode.ABLATION_POLICY_INVALID)
    _require_literal(
        policy.get("schema_version"),
        ABLATION_POLICY_SCHEMA_VERSION,
        f"{location}.schema_version",
        code=CampaignBlockerCode.ABLATION_POLICY_INVALID,
    )
    try:
        status = AblationPolicyStatus(_nonempty_string(policy.get("status"), f"{location}.status"))
    except ValueError as error:
        raise _failure(
            CampaignBlockerCode.ABLATION_POLICY_INVALID,
            f"{location}.status",
            "ablation status must be pending_resolution or resolved",
        ) from error
    resolution_blockers = _resolution_blockers(
        policy.get("resolution_blockers"),
        f"{location}.resolution_blockers",
        allowed_codes=_CELL_RESOLUTION_CODES,
        invalid_code=CampaignBlockerCode.ABLATION_POLICY_INVALID,
    )
    if status is AblationPolicyStatus.PENDING_RESOLUTION:
        required_codes = _REQUIRED_CELL_PENDING_CODES[cell_id]
        if frozenset(item.code for item in resolution_blockers) != required_codes:
            raise _failure(
                CampaignBlockerCode.ABLATION_POLICY_INVALID,
                f"{location}.resolution_blockers",
                "pending cell blockers do not exactly match the frozen v2 semantic policy",
            )
        if (
            policy.get("treatment") is not None
            or policy.get("control") is not None
            or policy.get("comparison") is not None
            or policy.get("held_fixed") is not None
            or policy.get("allowed_differences") != []
            or policy.get("neighbor_axes") != []
            or not resolution_blockers
        ):
            raise _failure(
                CampaignBlockerCode.ABLATION_POLICY_INVALID,
                location,
                "pending ablation must keep semantic fields null/empty and enumerate blockers",
            )
        blockers.extend(resolution_blockers)
        return None
    if resolution_blockers:
        raise _failure(
            CampaignBlockerCode.ABLATION_POLICY_INVALID,
            f"{location}.resolution_blockers",
            "resolved ablation cannot retain resolution blockers",
        )
    if not allow_synthetic_resolved_cells:
        blockers.append(
            CampaignBlocker(
                CampaignBlockerCode.UNREPRESENTABLE_ABLATION,
                f"{location}.status",
                "canonical v2 cell semantics are unresolved; resolved execution "
                "requires a new reviewed schema",
            )
        )
    treatment = _parse_arm(
        policy.get("treatment"),
        location=f"{location}.treatment",
        base_settings=reference.effective_settings,
    )
    control = _parse_arm(
        policy.get("control"),
        location=f"{location}.control",
        base_settings=reference.effective_settings,
    )
    if treatment.arm_id == control.arm_id:
        raise _failure(
            CampaignBlockerCode.ABLATION_POLICY_INVALID,
            location,
            "treatment and control arm IDs must differ",
        )
    for arm_name, arm in (("treatment", treatment), ("control", control)):
        if arm.research_ablation is not None:
            blockers.append(
                CampaignBlocker(
                    CampaignBlockerCode.UNREPRESENTABLE_ABLATION,
                    f"{location}.{arm_name}.research_ablation",
                    "the campaign compiler cannot execute a non-Pine research ablation",
                )
            )
    comparison = _parse_comparison(
        policy.get("comparison"),
        location=f"{location}.comparison",
        treatment_id=treatment.arm_id,
        control_id=control.arm_id,
    )
    allowed = _selectors(
        policy.get("allowed_differences"),
        f"{location}.allowed_differences",
        treatment.effective_settings,
    )
    if not allowed:
        raise _failure(
            CampaignBlockerCode.DIFFERENCE_POLICY_INVALID,
            f"{location}.allowed_differences",
            "a paired experiment must declare at least one allowed difference",
        )
    held_fixed = _parse_held_fixed(policy.get("held_fixed"), f"{location}.held_fixed")
    actual = _actual_differences(treatment, control)
    if set(allowed) != actual:
        message = (
            f"declared selectors {sorted(allowed)!r} do not equal "
            f"actual differences {sorted(actual)!r}"
        )
        raise _failure(
            CampaignBlockerCode.DIFFERENCE_POLICY_INVALID,
            f"{location}.allowed_differences",
            message,
        )
    neighbors = _parse_neighbor_axes(
        policy.get("neighbor_axes"),
        location=f"{location}.neighbor_axes",
        treatment=treatment,
        control=control,
        allowed=allowed,
        replay_digest=replay_digest,
    )
    if not neighbors:
        raise _failure(
            CampaignBlockerCode.MISSING_NEIGHBOR_AXIS,
            f"{location}.neighbor_axes",
            "resolved v2 comparisons require at least one monotone Pine neighbor axis",
        )
    compiled_treatment = _compile_arm(treatment, replay_digest)
    compiled_control = _compile_arm(control, replay_digest)
    candidate_configs = [
        compiled_treatment.config_sha256,
        compiled_control.config_sha256,
        *(item.arm.config_sha256 for item in neighbors),
    ]
    if len(candidate_configs) != len(set(candidate_configs)):
        raise _failure(
            CampaignBlockerCode.NEIGHBOR_AXIS_INVALID,
            f"{location}.neighbor_axes",
            "treatment, control, and derived neighbors must have unique effective configs",
        )
    policy_digest = _canonical_digest(
        {
            "schema_version": ABLATION_POLICY_SCHEMA_VERSION,
            "status": AblationPolicyStatus.RESOLVED,
            "treatment_config_sha256": compiled_treatment.config_sha256,
            "control_config_sha256": compiled_control.config_sha256,
            "comparison": {
                "kind": comparison.kind,
                "treatment_arm_id": comparison.treatment_arm_id,
                "control_arm_id": comparison.control_arm_id,
                "estimand": comparison.estimand,
                "direction": comparison.direction,
            },
            "allowed_differences": allowed,
            "held_fixed": {
                "pine_inputs": held_fixed.pine_inputs,
                "settings_fields": held_fixed.settings_fields,
                "replay_policy": held_fixed.replay_policy,
            },
            "neighbors": [
                {
                    "axis_selector": item.axis_selector,
                    "axis_value": item.axis_value,
                    "base_arm_id": item.base_arm_id,
                    "config_sha256": item.arm.config_sha256,
                }
                for item in neighbors
            ],
        }
    )
    execution_digest = _canonical_digest(
        {
            "schema_version": COMPILED_CELL_SCHEMA_VERSION,
            "cell_id": cell_id,
            "hypothesis_id": hypothesis_id,
            "treatment_config_sha256": compiled_treatment.config_sha256,
            "control_config_sha256": compiled_control.config_sha256,
            "neighbor_config_sha256": [item.arm.config_sha256 for item in neighbors],
            "ablation_policy_sha256": policy_digest,
            "replay_policy_sha256": replay_digest,
        }
    )
    return CompiledCampaignCell(
        cell_id=cell_id,
        hypothesis_id=hypothesis_id,
        role=role,
        treatment=compiled_treatment,
        control=compiled_control,
        comparison=comparison,
        allowed_differences=allowed,
        held_fixed=held_fixed,
        neighbors=neighbors,
        ablation_policy_sha256=policy_digest,
        replay_policy_sha256=replay_digest,
        execution_sha256=execution_digest,
    )


def _reject_duplicate_execution_configs(
    cells: tuple[CompiledCampaignCell, ...], blockers: list[CampaignBlocker]
) -> None:
    seen: dict[str, tuple[str, str]] = {}
    for cell in cells:
        candidates = [
            ("treatment", cell.treatment),
            ("control", cell.control),
            *((f"neighbor:{index}", item.arm) for index, item in enumerate(cell.neighbors)),
        ]
        for role, arm in candidates:
            prior = seen.get(arm.config_sha256)
            if prior is not None:
                blockers.append(
                    CampaignBlocker(
                        CampaignBlockerCode.DUPLICATE_EXECUTION_KEY,
                        f"campaign_cells.{cell.cell_id}.{role}",
                        (
                            "effective execution config duplicates "
                            f"campaign_cells.{prior[0]}.{prior[1]}"
                        ),
                    )
                )
            else:
                seen[arm.config_sha256] = cell.cell_id, role


def _parse_comparison(
    value: object,
    *,
    location: str,
    treatment_id: str,
    control_id: str,
) -> CampaignComparison:
    comparison = _object(value, location, code=CampaignBlockerCode.ABLATION_POLICY_INVALID)
    _exact_keys(
        comparison,
        {"kind", "treatment_arm_id", "control_arm_id", "estimand", "direction"},
        location,
        CampaignBlockerCode.ABLATION_POLICY_INVALID,
    )
    _require_literal(
        comparison.get("kind"),
        "paired_treatment_vs_control",
        f"{location}.kind",
        code=CampaignBlockerCode.ABLATION_POLICY_INVALID,
    )
    declared_treatment = _nonempty_string(
        comparison.get("treatment_arm_id"), f"{location}.treatment_arm_id"
    )
    declared_control = _nonempty_string(
        comparison.get("control_arm_id"), f"{location}.control_arm_id"
    )
    if declared_treatment != treatment_id or declared_control != control_id:
        raise _failure(
            CampaignBlockerCode.ABLATION_POLICY_INVALID,
            location,
            "comparison arm references must exactly match treatment and control",
        )
    try:
        direction = ComparisonDirection(
            _nonempty_string(comparison.get("direction"), f"{location}.direction")
        )
    except ValueError as error:
        raise _failure(
            CampaignBlockerCode.ABLATION_POLICY_INVALID,
            f"{location}.direction",
            "comparison direction is unsupported",
        ) from error
    return CampaignComparison(
        kind="paired_treatment_vs_control",
        treatment_arm_id=treatment_id,
        control_arm_id=control_id,
        estimand=_nonempty_string(comparison.get("estimand"), f"{location}.estimand"),
        direction=direction,
    )


def _parse_held_fixed(value: object, location: str) -> HeldFixedPolicy:
    held = _object(value, location, code=CampaignBlockerCode.DIFFERENCE_POLICY_INVALID)
    _exact_keys(
        held,
        {"pine_inputs", "settings_fields", "replay_policy"},
        location,
        CampaignBlockerCode.DIFFERENCE_POLICY_INVALID,
    )
    if (
        held.get("pine_inputs") != "all_except_allowed_differences"
        or held.get("settings_fields") != list(_HELD_SETTINGS_FIELDS)
        or held.get("replay_policy") is not True
    ):
        raise _failure(
            CampaignBlockerCode.DIFFERENCE_POLICY_INVALID,
            location,
            "held_fixed must equal the complete v1 fixed-coordinate policy",
        )
    return HeldFixedPolicy(
        pine_inputs="all_except_allowed_differences",
        settings_fields=_HELD_SETTINGS_FIELDS,
        replay_policy=True,
    )


def _selectors(
    value: object,
    location: str,
    settings: FiveToolSettings,
) -> tuple[str, ...]:
    raw = _array(value, location)
    selectors = tuple(
        _nonempty_string(item, f"{location}[{index}]") for index, item in enumerate(raw)
    )
    if len(selectors) != len(set(selectors)) or tuple(sorted(selectors)) != selectors:
        raise _failure(
            CampaignBlockerCode.DIFFERENCE_POLICY_INVALID,
            location,
            "allowed difference selectors must be unique and lexically sorted",
        )
    names = {name for name, _ in settings.inputs}
    for selector in selectors:
        if selector == "research_ablation":
            continue
        if not selector.startswith("pine:") or selector[5:] not in names:
            raise _failure(
                CampaignBlockerCode.DIFFERENCE_POLICY_INVALID,
                location,
                f"unknown typed difference selector {selector!r}",
            )
    return selectors


def _actual_differences(left: _ArmDraft, right: _ArmDraft) -> set[str]:
    differences = {
        f"pine:{name}"
        for (name, left_value), (right_name, right_value) in zip(
            left.effective_settings.inputs,
            right.effective_settings.inputs,
            strict=True,
        )
        if name == right_name and _canonical_json(left_value) != _canonical_json(right_value)
    }
    if left.research_ablation != right.research_ablation:
        differences.add("research_ablation")
    return differences


def _parse_neighbor_axes(
    value: object,
    *,
    location: str,
    treatment: _ArmDraft,
    control: _ArmDraft,
    allowed: tuple[str, ...],
    replay_digest: str,
) -> tuple[CompiledCampaignNeighbor, ...]:
    axes = _array(value, location)
    compiled: list[CompiledCampaignNeighbor] = []
    seen: set[str] = set()
    arms = {treatment.arm_id: treatment, control.arm_id: control}
    for index, raw_axis in enumerate(axes):
        axis_location = f"{location}[{index}]"
        axis = _object(raw_axis, axis_location, code=CampaignBlockerCode.NEIGHBOR_AXIS_INVALID)
        _exact_keys(
            axis,
            {"base_arm_id", "selector", "values"},
            axis_location,
            CampaignBlockerCode.NEIGHBOR_AXIS_INVALID,
        )
        base_id = _nonempty_string(axis.get("base_arm_id"), f"{axis_location}.base_arm_id")
        if base_id not in arms:
            raise _failure(
                CampaignBlockerCode.NEIGHBOR_AXIS_INVALID,
                f"{axis_location}.base_arm_id",
                "neighbor axis base must be the treatment or control arm",
            )
        selector = _nonempty_string(axis.get("selector"), f"{axis_location}.selector")
        if selector in seen or selector not in allowed or not selector.startswith("pine:"):
            raise _failure(
                CampaignBlockerCode.NEIGHBOR_AXIS_INVALID,
                f"{axis_location}.selector",
                "neighbor axes must be unique allowed Pine dimensions",
            )
        seen.add(selector)
        name = selector[5:]
        base = arms[base_id]
        base_values = dict(base.effective_settings.inputs)
        center = base_values[name]
        if isinstance(center, bool) or not isinstance(center, int | float):
            raise _failure(
                CampaignBlockerCode.NEIGHBOR_AXIS_INVALID,
                f"{axis_location}.selector",
                "neighbor axes require a numeric Pine input",
            )
        raw_values = _array(axis.get("values"), f"{axis_location}.values")
        if len(raw_values) < 3:
            raise _failure(
                CampaignBlockerCode.NEIGHBOR_AXIS_INVALID,
                f"{axis_location}.values",
                "neighbor axis requires a center and values on both sides",
            )
        numeric: list[int | float] = []
        for value_index, item in enumerate(raw_values):
            if (
                isinstance(item, bool)
                or not isinstance(item, int | float)
                or not math.isfinite(item)
            ):
                raise _failure(
                    CampaignBlockerCode.NEIGHBOR_AXIS_INVALID,
                    f"{axis_location}.values[{value_index}]",
                    "neighbor axis values must be finite numbers",
                )
            if isinstance(center, int) and not isinstance(item, int):
                raise _failure(
                    CampaignBlockerCode.NEIGHBOR_AXIS_INVALID,
                    f"{axis_location}.values[{value_index}]",
                    "integer Pine axes require integer values",
                )
            numeric.append(float(item) if isinstance(center, float) else item)
        if any(left >= right for left, right in pairwise(numeric)):
            raise _failure(
                CampaignBlockerCode.NEIGHBOR_AXIS_INVALID,
                f"{axis_location}.values",
                "neighbor axis values must be strictly increasing",
            )
        center_indexes = [
            value_index
            for value_index, item in enumerate(numeric)
            if _canonical_json(item) == _canonical_json(center)
        ]
        if len(center_indexes) != 1 or center_indexes[0] in {0, len(numeric) - 1}:
            raise _failure(
                CampaignBlockerCode.NEIGHBOR_AXIS_INVALID,
                f"{axis_location}.values",
                "neighbor axis must contain the exact base value with neighbors on both sides",
            )
        for value_index, axis_value in enumerate(numeric):
            if value_index == center_indexes[0]:
                continue
            try:
                settings = _settings_with_overrides(base.effective_settings, {name: axis_value})
            except (FiveToolInputError, ValueError) as error:
                raise _failure(
                    CampaignBlockerCode.NEIGHBOR_AXIS_INVALID,
                    f"{axis_location}.values[{value_index}]",
                    str(error),
                ) from error
            normalized_axis_value = dict(settings.inputs)[name]
            assert isinstance(normalized_axis_value, int | float) and not isinstance(
                normalized_axis_value, bool
            )
            neighbor_overrides = dict(base.pine_overrides)
            neighbor_overrides[name] = normalized_axis_value
            neighbor_draft = _ArmDraft(
                arm_id=(f"{base_id}--neighbor--{name}--{_canonical_digest(normalized_axis_value)}"),
                pine_overrides=tuple(sorted(neighbor_overrides.items())),
                research_ablation=base.research_ablation,
                effective_settings=settings,
            )
            compiled.append(
                CompiledCampaignNeighbor(
                    axis_selector=selector,
                    axis_value=normalized_axis_value,
                    base_arm_id=base_id,
                    arm=_compile_arm(neighbor_draft, replay_digest),
                )
            )
    return tuple(
        sorted(
            compiled,
            key=lambda item: (
                item.axis_selector,
                item.axis_value,
                item.base_arm_id,
                item.arm.arm_id,
            ),
        )
    )


def _compile_arm(draft: _ArmDraft, replay_digest: str) -> CompiledCampaignArm:
    settings_payload = {
        "history_start_utc": draft.effective_settings.history_start_utc.isoformat(),
        "inputs": draft.effective_settings.inputs,
        "contract_digest": draft.effective_settings.contract_digest,
        "exchange_timezone": draft.effective_settings.exchange_timezone,
        "point_value": draft.effective_settings.point_value,
        "minimum_tick": draft.effective_settings.minimum_tick,
    }
    if _canonical_digest(settings_payload) != draft.effective_settings.digest:
        raise ValueError("FiveToolSettings canonical payload disagrees with its digest")
    config_bytes = _canonical_bytes(
        {
            "schema_version": COMPILED_ARM_SCHEMA_VERSION,
            "effective_settings": settings_payload,
            "effective_settings_sha256": draft.effective_settings.digest,
            "research_ablation": (
                draft.research_ablation.canonical_payload
                if draft.research_ablation is not None
                else None
            ),
            "replay_policy_sha256": replay_digest,
        }
    )
    config_sha256 = hashlib.sha256(config_bytes).hexdigest()
    return CompiledCampaignArm(
        arm_id=draft.arm_id,
        pine_overrides=draft.pine_overrides,
        research_ablation=draft.research_ablation,
        effective_settings=draft.effective_settings,
        effective_settings_sha256=draft.effective_settings.digest,
        config_bytes=config_bytes,
        config_sha256=config_sha256,
    )


def _parse_execution_bindings(
    value: object,
    *,
    dataset_id: str | None,
    dataset_release_digest: str | None,
    accessible_partitions: frozenset[str],
    holdout_identities: frozenset[tuple[str, str]],
    holdout_partitions: frozenset[str],
    blockers: list[CampaignBlocker],
) -> ExecutionBindings | None:
    location = "execution_bindings"
    binding = _object(value, location, code=CampaignBlockerCode.EXECUTION_BINDING_INVALID)
    expected = {
        "schema_version",
        "status",
        "catalog_manifest_sha256",
        "partition_stage_map",
        "requests",
        "evaluator",
        "resolution_blockers",
    }
    _exact_keys(binding, expected, location, CampaignBlockerCode.EXECUTION_BINDING_INVALID)
    _require_literal(
        binding.get("schema_version"),
        EXECUTION_BINDINGS_SCHEMA_VERSION,
        f"{location}.schema_version",
        code=CampaignBlockerCode.EXECUTION_BINDING_INVALID,
    )
    try:
        status = AblationPolicyStatus(_nonempty_string(binding.get("status"), f"{location}.status"))
    except ValueError as error:
        raise _failure(
            CampaignBlockerCode.EXECUTION_BINDING_INVALID,
            f"{location}.status",
            "execution binding status must be pending_resolution or resolved",
        ) from error
    resolution_blockers = _resolution_blockers(
        binding.get("resolution_blockers"),
        f"{location}.resolution_blockers",
        allowed_codes=_EXECUTION_RESOLUTION_CODES,
        invalid_code=CampaignBlockerCode.EXECUTION_BINDING_INVALID,
    )
    if status is AblationPolicyStatus.PENDING_RESOLUTION:
        if (
            binding.get("catalog_manifest_sha256") is not None
            or binding.get("partition_stage_map") is not None
            or binding.get("requests") is not None
            or binding.get("evaluator") is not None
            or not resolution_blockers
        ):
            raise _failure(
                CampaignBlockerCode.EXECUTION_BINDING_INVALID,
                location,
                "pending execution bindings must keep identities null and enumerate blockers",
            )
        blockers.extend(resolution_blockers)
        return None
    if resolution_blockers:
        raise _failure(
            CampaignBlockerCode.EXECUTION_BINDING_INVALID,
            f"{location}.resolution_blockers",
            "resolved execution bindings cannot retain blockers",
        )
    catalog = _sha256(binding.get("catalog_manifest_sha256"), f"{location}.catalog_manifest_sha256")
    raw_stage_map = _object(
        binding.get("partition_stage_map"),
        f"{location}.partition_stage_map",
        code=CampaignBlockerCode.EXECUTION_BINDING_INVALID,
    )
    stage_map: dict[str, str] = {}
    for partition, raw_stage in raw_stage_map.items():
        _identity_string(
            partition,
            f"{location}.partition_stage_map.{partition}",
            code=CampaignBlockerCode.EXECUTION_BINDING_INVALID,
        )
        stage = _nonempty_string(raw_stage, f"{location}.partition_stage_map.{partition}")
        if stage not in _STAGES:
            raise _failure(
                CampaignBlockerCode.EXECUTION_BINDING_INVALID,
                f"{location}.partition_stage_map.{partition}",
                f"unsupported trial stage {stage!r}",
            )
        stage_map[partition] = stage
    raw_requests = _array(binding.get("requests"), f"{location}.requests")
    if not raw_requests:
        raise _failure(
            CampaignBlockerCode.EXECUTION_BINDING_INVALID,
            f"{location}.requests",
            "resolved execution bindings require at least one certified request",
        )
    requests: list[ExecutionRequestBinding] = []
    seen: set[str] = set()
    seen_catalog_keys: set[tuple[str, str, str]] = set()
    seen_content_keys: set[tuple[str, str]] = set()
    for index, raw_request in enumerate(raw_requests):
        request_location = f"{location}.requests[{index}]"
        request = _object(
            raw_request,
            request_location,
            code=CampaignBlockerCode.EXECUTION_BINDING_INVALID,
        )
        _exact_keys(
            request,
            {
                "request_id",
                "dataset_id",
                "partition",
                "data_version",
                "source_id",
                "source_receipt_sha256",
            },
            request_location,
            CampaignBlockerCode.EXECUTION_BINDING_INVALID,
        )
        request_id = _nonempty_string(request.get("request_id"), f"{request_location}.request_id")
        if request_id in seen:
            raise _failure(
                CampaignBlockerCode.EXECUTION_BINDING_INVALID,
                f"{request_location}.request_id",
                "execution request IDs must be unique",
            )
        seen.add(request_id)
        parsed = ExecutionRequestBinding(
            request_id=request_id,
            dataset_id=_identity_string(
                request.get("dataset_id"),
                f"{request_location}.dataset_id",
                code=CampaignBlockerCode.EXECUTION_BINDING_INVALID,
            ),
            partition=_identity_string(
                request.get("partition"),
                f"{request_location}.partition",
                code=CampaignBlockerCode.EXECUTION_BINDING_INVALID,
            ),
            data_version=_sha256(request.get("data_version"), f"{request_location}.data_version"),
            source_id=_identity_string(
                request.get("source_id"),
                f"{request_location}.source_id",
                code=CampaignBlockerCode.EXECUTION_BINDING_INVALID,
            ),
            source_receipt_sha256=_sha256(
                request.get("source_receipt_sha256"),
                f"{request_location}.source_receipt_sha256",
            ),
        )
        if parsed.partition not in accessible_partitions:
            raise _failure(
                CampaignBlockerCode.EXECUTION_BINDING_INVALID,
                f"{request_location}.partition",
                "request partition is not declared accessible",
            )
        if parsed.partition not in stage_map:
            raise _failure(
                CampaignBlockerCode.EXECUTION_BINDING_INVALID,
                f"{request_location}.partition",
                "request partition has no stage mapping",
            )
        normalized_request_identity = (
            _normalized_identity(parsed.dataset_id),
            _normalized_identity(parsed.partition),
        )
        if (
            normalized_request_identity in holdout_identities
            or normalized_request_identity[1] in holdout_partitions
        ):
            raise _failure(
                CampaignBlockerCode.EXECUTION_BINDING_INVALID,
                request_location,
                "ordinary execution requests cannot alias a declared holdout identity",
            )
        if dataset_id is None or dataset_release_digest is None:
            raise _failure(
                CampaignBlockerCode.EXECUTION_BINDING_INVALID,
                request_location,
                "certified dataset release identity is unresolved",
            )
        if parsed.dataset_id != dataset_id:
            raise _failure(
                CampaignBlockerCode.EXECUTION_BINDING_INVALID,
                request_location,
                "request dataset ID disagrees with the campaign release lock",
            )
        catalog_key = (
            normalized_request_identity[0],
            normalized_request_identity[1],
            parsed.data_version,
        )
        if catalog_key in seen_catalog_keys:
            raise _failure(
                CampaignBlockerCode.EXECUTION_BINDING_INVALID,
                request_location,
                "execution requests must have unique catalog dataset/partition/version keys",
            )
        seen_catalog_keys.add(catalog_key)
        content_key = normalized_request_identity[0], parsed.data_version
        if content_key in seen_content_keys:
            raise _failure(
                CampaignBlockerCode.EXECUTION_BINDING_INVALID,
                request_location,
                "execution partitions must not alias one dataset content identity",
            )
        seen_content_keys.add(content_key)
        requests.append(parsed)
    if set(stage_map) != {request.partition for request in requests}:
        raise _failure(
            CampaignBlockerCode.EXECUTION_BINDING_INVALID,
            f"{location}.partition_stage_map",
            "partition stage map must exactly cover the certified requests",
        )
    evaluator_raw = _object(
        binding.get("evaluator"),
        f"{location}.evaluator",
        code=CampaignBlockerCode.EXECUTION_BINDING_INVALID,
    )
    _exact_keys(
        evaluator_raw,
        {"schema_version", "evaluator_id", "sha256"},
        f"{location}.evaluator",
        CampaignBlockerCode.EXECUTION_BINDING_INVALID,
    )
    _require_literal(
        evaluator_raw.get("schema_version"),
        EVALUATOR_BINDING_SCHEMA_VERSION,
        f"{location}.evaluator.schema_version",
        code=CampaignBlockerCode.EXECUTION_BINDING_INVALID,
    )
    evaluator = EvaluatorBinding(
        schema_version=EVALUATOR_BINDING_SCHEMA_VERSION,
        evaluator_id=_nonempty_string(
            evaluator_raw.get("evaluator_id"), f"{location}.evaluator.evaluator_id"
        ),
        sha256=_sha256(evaluator_raw.get("sha256"), f"{location}.evaluator.sha256"),
    )
    requests.sort(
        key=lambda item: (
            item.request_id,
            item.dataset_id,
            item.partition,
            item.data_version,
            item.source_id,
            item.source_receipt_sha256,
        )
    )
    payload = {
        "schema_version": EXECUTION_BINDINGS_SCHEMA_VERSION,
        "catalog_manifest_sha256": catalog,
        "partition_stage_map": dict(sorted(stage_map.items())),
        "requests": [
            {
                "request_id": item.request_id,
                "dataset_id": item.dataset_id,
                "partition": item.partition,
                "data_version": item.data_version,
                "source_id": item.source_id,
                "source_receipt_sha256": item.source_receipt_sha256,
            }
            for item in requests
        ],
        "evaluator": {
            "schema_version": evaluator.schema_version,
            "evaluator_id": evaluator.evaluator_id,
            "sha256": evaluator.sha256,
        },
    }
    return ExecutionBindings(
        schema_version=EXECUTION_BINDINGS_SCHEMA_VERSION,
        catalog_manifest_sha256=catalog,
        partition_stage_map=tuple(sorted(stage_map.items())),
        requests=tuple(requests),
        evaluator=evaluator,
        sha256=_canonical_digest(payload),
    )


def _trial_blueprints(
    *,
    identity: _GlobalIdentity,
    manifest_sha256: str,
    campaign_sha256: str,
    cells: tuple[CompiledCampaignCell, ...],
    bindings: ExecutionBindings,
) -> tuple[CompiledTrialBlueprint, ...]:
    """Build candidate trials; the identity-only reference arm is intentionally omitted."""

    assert identity.code_commit is not None and identity.criteria_digest is not None
    out: list[CompiledTrialBlueprint] = []
    for cell in cells:
        arms = [cell.treatment, cell.control, *(neighbor.arm for neighbor in cell.neighbors)]
        for arm in arms:
            for request in bindings.requests:
                out.append(
                    CompiledTrialBlueprint(
                        campaign_id=identity.campaign_id,
                        campaign_manifest_sha256=manifest_sha256,
                        campaign_sha256=campaign_sha256,
                        cell_id=cell.cell_id,
                        hypothesis_id=cell.hypothesis_id,
                        arm_id=arm.arm_id,
                        stage=bindings.stage_for(request.partition),
                        strategy_id=identity.strategy_id,
                        config_bytes=arm.config_bytes,
                        config_digest=arm.config_sha256,
                        code_commit=identity.code_commit,
                        criteria_digest=identity.criteria_digest,
                        evaluator_id=bindings.evaluator.evaluator_id,
                        evaluator_digest=bindings.evaluator.sha256,
                        request=request,
                    )
                )
    return tuple(
        sorted(
            out,
            key=lambda item: (
                item.cell_id,
                item.arm_id,
                item.request.request_id,
                item.stage,
                item.config_digest,
            ),
        )
    )


def _settings_with_overrides(
    base: FiveToolSettings,
    overrides: Mapping[str, InputValue],
) -> FiveToolSettings:
    values = dict(base.inputs)
    unknown = sorted(set(overrides) - set(values))
    if unknown:
        raise FiveToolInputError(f"unknown Five-Tool input overrides: {unknown}")
    for name, value in overrides.items():
        expected = values[name]
        if isinstance(expected, bool):
            valid_type = isinstance(value, bool)
        elif isinstance(expected, int):
            valid_type = isinstance(value, int) and not isinstance(value, bool)
        elif isinstance(expected, float):
            valid_type = isinstance(value, int | float) and not isinstance(value, bool)
        else:
            valid_type = isinstance(value, str)
        if not valid_type:
            raise FiveToolInputError(
                f"override {name!r} has incompatible type {type(value).__name__}"
            )
        values[name] = float(value) if isinstance(expected, float) else value
    return FiveToolSettings(
        history_start_utc=base.history_start_utc,
        inputs=tuple((name, values[name]) for name, _ in base.inputs),
        contract_digest=base.contract_digest,
        exchange_timezone=base.exchange_timezone,
        point_value=base.point_value,
        minimum_tick=base.minimum_tick,
    )


def _pine_overrides(value: object, location: str) -> tuple[tuple[str, InputValue], ...]:
    raw = _object(value, location, code=CampaignBlockerCode.PINE_OVERRIDE_INVALID)
    out: list[tuple[str, InputValue]] = []
    for name, item in sorted(raw.items()):
        if item is None or type(item) not in (bool, int, float, str):
            raise _failure(
                CampaignBlockerCode.PINE_OVERRIDE_INVALID,
                f"{location}.{name}",
                "Pine override must be a boolean, integer, finite number, or string",
            )
        if isinstance(item, float) and not math.isfinite(item):
            raise _failure(
                CampaignBlockerCode.PINE_OVERRIDE_INVALID,
                f"{location}.{name}",
                "Pine override must be finite",
            )
        out.append((name, cast(InputValue, item)))
    return tuple(out)


def _resolution_blockers(
    value: object,
    location: str,
    *,
    allowed_codes: frozenset[CampaignBlockerCode],
    invalid_code: CampaignBlockerCode,
) -> tuple[CampaignBlocker, ...]:
    raw = _array(value, location)
    blockers: list[CampaignBlocker] = []
    seen_codes: set[CampaignBlockerCode] = set()
    for index, item in enumerate(raw):
        item_location = f"{location}[{index}]"
        declared = _object(item, item_location, code=invalid_code)
        _exact_keys(declared, {"code", "message"}, item_location, invalid_code)
        raw_code = declared.get("code")
        try:
            code = CampaignBlockerCode(raw_code) if isinstance(raw_code, str) else None
        except ValueError:
            code = None
        if code is None or code not in allowed_codes:
            raise _failure(
                invalid_code,
                f"{item_location}.code",
                f"unknown or disallowed resolution blocker code {raw_code!r}",
            )
        if code in seen_codes:
            raise _failure(
                invalid_code,
                f"{item_location}.code",
                f"resolution blocker code {code.value!r} must be unique",
            )
        seen_codes.add(code)
        message = declared.get("message")
        if not isinstance(message, str) or not message.strip():
            raise _failure(
                invalid_code,
                f"{item_location}.message",
                "resolution blocker message must be a non-empty string",
            )
        blockers.append(CampaignBlocker(code, item_location, message))
    return tuple(blockers)


def _deduplicate_blockers(blockers: list[CampaignBlocker]) -> list[CampaignBlocker]:
    seen: set[tuple[CampaignBlockerCode, str, str]] = set()
    out: list[CampaignBlocker] = []
    for blocker in blockers:
        identity = blocker.code, blocker.location, blocker.message
        if identity not in seen:
            seen.add(identity)
            out.append(blocker)
    return out


def _require_json_value(value: object, *, location: str) -> None:
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{location} contains a non-finite number")
        if _is_negative_zero(value):
            raise ValueError(f"{location} contains signed zero")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _require_json_value(item, location=f"{location}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{location} contains a non-string object key")
            _require_json_value(item, location=f"{location}.{key}")
        return
    raise ValueError(f"{location} contains non-JSON type {type(value).__name__}")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return _canonical_json(value).encode("utf-8")


def _object(
    value: object,
    location: str,
    *,
    code: CampaignBlockerCode = CampaignBlockerCode.MANIFEST_SCHEMA,
) -> dict[str, object]:
    if not isinstance(value, dict) or not all(type(key) is str for key in value):
        raise _failure(code, location, f"{location} must be an object")
    return cast(dict[str, object], value)


def _array(value: object, location: str) -> list[object]:
    if not isinstance(value, list):
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            location,
            f"{location} must be an array",
        )
    return cast(list[object], value)


def _exact_keys(
    value: dict[str, object],
    expected: set[str],
    location: str,
    code: CampaignBlockerCode,
) -> None:
    if set(value) != expected:
        message = (
            f"{location} keys are not exact; "
            f"missing={sorted(expected - set(value))}, "
            f"unknown={sorted(set(value) - expected)}"
        )
        raise _failure(
            code,
            location,
            message,
        )


def _nonempty_string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            location,
            f"{location} must be a non-empty string",
        )
    return value


def _identity_string(
    value: object,
    location: str,
    *,
    code: CampaignBlockerCode = CampaignBlockerCode.MANIFEST_SCHEMA,
) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise _failure(
            code,
            location,
            f"{location} must use CertifiedDataRequest-compatible identity syntax",
        )
    return value


def _string_set(value: object, location: str) -> set[str]:
    raw = _array(value, location)
    strings = {_nonempty_string(item, f"{location}[{index}]") for index, item in enumerate(raw)}
    if len(strings) != len(raw):
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            location,
            f"{location} must not contain duplicates",
        )
    return strings


def _normalized_identity(value: str) -> str:
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    if not normalized:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            "identity",
            "dataset or partition identity must be non-empty",
        )
    return normalized


def _require_literal(
    value: object,
    expected: str,
    location: str,
    *,
    code: CampaignBlockerCode = CampaignBlockerCode.MANIFEST_SCHEMA,
) -> None:
    if value != expected:
        raise _failure(code, location, f"{location} must equal {expected!r}")


def _sha256(value: object, location: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            location,
            f"{location} must be a lowercase SHA-256",
        )
    return value


def _number(value: object, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            location,
            f"{location} must be a finite number",
        )
    parsed = float(value)
    return 0.0 if parsed == 0.0 else parsed


def _is_negative_zero(value: float) -> bool:
    return value == 0.0 and math.copysign(1.0, value) < 0.0


def _finite_nonnegative_number(value: object, location: str) -> float:
    parsed = _number(value, location)
    if parsed < 0.0:
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            location,
            f"{location} must be non-negative",
        )
    return parsed


def _integer(value: object, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            location,
            f"{location} must be an integer",
        )
    return value


def _boolean(value: object, location: str) -> bool:
    if not isinstance(value, bool):
        raise _failure(
            CampaignBlockerCode.MANIFEST_SCHEMA,
            location,
            f"{location} must be a boolean",
        )
    return value


def _failure(
    code: CampaignBlockerCode,
    location: str,
    message: str,
) -> _CompileFailure:
    return _CompileFailure(code, location, message)
