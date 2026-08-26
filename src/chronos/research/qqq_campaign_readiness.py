"""Authentication-only readiness report for the frozen QQQ research campaign.

This module reports repository identity and blockers.  It deliberately exposes
no market-data read operation, trial registry, holdout unlock, broker, order,
execution, promotion, or runtime capability.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from chronos.research.qqq_confluence import (
    CandidateBlockerCode,
    QQQConfluenceSpecError,
    compile_qqq_confluence_candidate,
)
from chronos.research.qqq_control import (
    ControlBlockerCode,
    QQQControlSpecError,
    compile_qqq_control,
)

SCHEMA_VERSION = "chronos-qqq-campaign-readiness-v1"
READINESS_ID = "qqq-campaign-readiness-v1-owner-review-2026-08-25"
EXPECTED_READINESS_SHA256 = "ddfdf0a1a69dd57d99baf14719eb7c4c9908b5ec5c7650e2161e027078bbc24d"

_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "readiness_id",
        "status",
        "purpose",
        "authority",
        "campaign",
        "data_boundaries",
        "artifacts",
        "requirements",
        "forbidden_capabilities",
    }
)
_FORBIDDEN_CAPABILITIES = {
    "market_data_reader",
    "holdout_unlock",
    "trial_registration",
    "broker",
    "order_construction",
    "order_submission",
    "promotion",
}


class QQQCampaignReadinessError(ValueError):
    """The readiness artifact is malformed, stale, or no longer safely blocked."""


class CampaignCompilationStatus(StrEnum):
    BLOCKED_BEFORE_FIRST_DATA_READ = "blocked_before_first_data_read"


class ArtifactCode(StrEnum):
    CONSTITUTION = "constitution"
    SMA_CONTROL = "sma_control"
    CONFLUENCE_CANDIDATE = "confluence_candidate"
    PAPER_MANAGEMENT = "paper_management"
    MANAGED_POSITION_ADMISSION = "managed_position_admission"


class ArtifactState(StrEnum):
    LOCKED_IN_REPOSITORY = "locked_in_repository"


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    code: ArtifactCode
    path: str
    content_sha256: str
    semantic_sha256: str | None
    state: ArtifactState


class RequirementCode(StrEnum):
    READINESS_OWNER_APPROVAL = "readiness_owner_approval"
    CERTIFIED_SIX_SYMBOL_EXPORT = "certified_six_symbol_export"
    INDEPENDENT_CORPORATE_ACTION_ATTESTATION = "independent_corporate_action_attestation"
    OWNER_APPROVED_HOLDOUT_MAP = "owner_approved_holdout_map"
    CERTIFIED_RELEASE_AND_CATALOG = "certified_release_and_catalog"
    BENCHMARK_AND_CASH_LEG_IDENTITY = "benchmark_and_cash_leg_identity"
    LONG_COST_SCHEDULE_IDENTITY = "long_cost_schedule_identity"
    POWER_ANALYSIS_IDENTITY = "power_analysis_identity"
    EVALUATOR_CRITERIA_CODE_REGISTRY_CAMPAIGN_IDENTITY = (
        "evaluator_criteria_code_registry_campaign_identity"
    )
    TRADINGVIEW_TRACE_EXPORT = "tradingview_trace_export"
    TRADINGVIEW_PARITY_EVIDENCE = "tradingview_parity_evidence"
    BASE_FIVE_TOOL_BINDINGS = "base_five_tool_bindings"
    SHORT_SIDE_EVIDENCE = "short_side_evidence"
    AUTHENTICATED_MANAGEMENT_EVENT_IDENTITY = "authenticated_management_event_identity"
    BROKER_HELD_PROTECTION_SEMANTICS = "broker_held_protection_semantics"
    REAL_PAPER_LIFECYCLE_EVIDENCE = "real_paper_lifecycle_evidence"


class RequirementScope(StrEnum):
    SHARED = "shared"
    CONFLUENCE_CANDIDATE = "confluence_candidate"
    SHORT_SIDE = "short_side"
    PAPER_ACTIVATION = "paper_activation"


class RequirementState(StrEnum):
    SATISFIED = "satisfied"
    OWNER_ACTION_REQUIRED = "owner_action_required"
    CHRONOS_BUILD_REQUIRED = "chronos_build_required"
    UNAVAILABLE = "unavailable"
    DEFERRED_ACTIVATION = "deferred_activation"


@dataclass(frozen=True, slots=True)
class CampaignRequirement:
    code: RequirementCode
    scope: RequirementScope
    state: RequirementState
    blocks_long_campaign: bool
    detail: str


@dataclass(frozen=True, slots=True)
class CompiledQQQCampaignReadiness:
    readiness_id: str
    readiness_sha256: str
    status: CampaignCompilationStatus
    execution_symbol: str
    robustness_panel_symbols: tuple[str, ...]
    bar_interval: str
    primary_kpi: str
    qqq_release_symbols: tuple[str, ...]
    base_five_tool_intake_symbols: tuple[str, ...]
    cross_dataset_identity_transfer: str
    artifacts: tuple[ArtifactIdentity, ...]
    requirements: tuple[CampaignRequirement, ...]
    order_authority: str
    promotion_authority: str
    registered_trials: int

    @property
    def data_read_permitted(self) -> bool:
        return False

    @property
    def trial_registration_permitted(self) -> bool:
        return False

    @property
    def holdout_unlock_permitted(self) -> bool:
        return False

    @property
    def executable(self) -> bool:
        return False

    @property
    def ready_for_first_data_read(self) -> bool:
        return False

    @property
    def owner_actions(self) -> tuple[CampaignRequirement, ...]:
        return tuple(
            requirement
            for requirement in self.requirements
            if requirement.state is RequirementState.OWNER_ACTION_REQUIRED
        )


def _default_readiness_path() -> Path:
    return Path(__file__).resolve().parents[3] / "specs/qqq_campaign_readiness_v1.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise QQQCampaignReadinessError(f"{context} must be an object")
    return cast(dict[str, object], value)


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise QQQCampaignReadinessError(f"{context} must be a non-empty string")
    return value


def _integer(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise QQQCampaignReadinessError(f"{context} must be an integer")
    return value


def _boolean(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise QQQCampaignReadinessError(f"{context} must be a boolean")
    return value


def _require_exact(value: object, expected: object, context: str) -> None:
    if value != expected:
        raise QQQCampaignReadinessError(f"{context} must remain {expected!r}")


def _parse_artifacts(value: object) -> tuple[ArtifactIdentity, ...]:
    if not isinstance(value, list):
        raise QQQCampaignReadinessError("artifacts must be a list")
    artifacts: list[ArtifactIdentity] = []
    for index, item in enumerate(value):
        raw = _mapping(item, f"artifacts[{index}]")
        try:
            code = ArtifactCode(_string(raw.get("code"), f"artifacts[{index}].code"))
            state = ArtifactState(_string(raw.get("state"), f"artifacts[{index}].state"))
        except ValueError as error:
            raise QQQCampaignReadinessError(f"artifacts[{index}] has an unknown enum") from error
        semantic_sha256 = raw.get("semantic_sha256")
        if semantic_sha256 is not None:
            semantic_sha256 = _string(
                semantic_sha256,
                f"artifacts[{index}].semantic_sha256",
            )
        artifacts.append(
            ArtifactIdentity(
                code=code,
                path=_string(raw.get("path"), f"artifacts[{index}].path"),
                content_sha256=_string(
                    raw.get("content_sha256"),
                    f"artifacts[{index}].content_sha256",
                ),
                semantic_sha256=semantic_sha256,
                state=state,
            )
        )
    expected = (
        ArtifactIdentity(
            ArtifactCode.CONSTITUTION,
            "research/qqq_v1_constitution.json",
            "4c99ce9d09f43a418c7342b0e40a0795b253bf3f1cd0e37d29419498b3008d56",
            None,
            ArtifactState.LOCKED_IN_REPOSITORY,
        ),
        ArtifactIdentity(
            ArtifactCode.SMA_CONTROL,
            "specs/qqq_sma_control_v1.json",
            "a0ec83b3431016df0c599895ead65083fc72b5afb87073dfbdf046d68e23bb03",
            None,
            ArtifactState.LOCKED_IN_REPOSITORY,
        ),
        ArtifactIdentity(
            ArtifactCode.CONFLUENCE_CANDIDATE,
            "specs/qqq_five_tool_candidate_v1.json",
            "59348ca3da9e9b68ec4edd1fc54572783e9256ae9c55ac18ffe844c0b4b78054",
            None,
            ArtifactState.LOCKED_IN_REPOSITORY,
        ),
        ArtifactIdentity(
            ArtifactCode.PAPER_MANAGEMENT,
            "src/chronos/supervisor/position_management.py",
            "0f89368049cd0b936fa88847fd0dc5c9d8dddd3a7ed1a69a78eb471d8edf0e2c",
            "7a5b29eb8055b0b4cf0f80476cca200234cfe96afd5327101da7e76ac09ec188",
            ArtifactState.LOCKED_IN_REPOSITORY,
        ),
        ArtifactIdentity(
            ArtifactCode.MANAGED_POSITION_ADMISSION,
            "src/chronos/supervisor/position_admission.py",
            "dc7e703fd5e393d3cf3baedf7b470437850fdb4a4dae9a2a61847d7353087846",
            None,
            ArtifactState.LOCKED_IN_REPOSITORY,
        ),
    )
    if tuple(artifacts) != expected:
        raise QQQCampaignReadinessError("frozen artifact identities or their order changed")
    return tuple(artifacts)


def _parse_campaign(value: object) -> tuple[str, tuple[str, ...], str, str]:
    campaign = _mapping(value, "campaign")
    _require_exact(campaign.get("execution_symbol"), "QQQ", "campaign.execution_symbol")
    _require_exact(
        campaign.get("robustness_panel_symbols"),
        ["QQQ", "SPY", "IWM", "DIA", "GLD", "TLT"],
        "campaign.robustness_panel_symbols",
    )
    _require_exact(campaign.get("bar_interval"), "1D", "campaign.bar_interval")
    _require_exact(campaign.get("primary_kpi"), "net_edge_confidence", "campaign.primary_kpi")
    _require_exact(
        campaign.get("strategy_order"),
        ["sma_control", "confluence_candidate_after_control_pass"],
        "campaign.strategy_order",
    )
    _require_exact(
        campaign.get("short_side"),
        "unavailable_without_new_identity_and_evidence",
        "campaign.short_side",
    )
    return "QQQ", ("QQQ", "SPY", "IWM", "DIA", "GLD", "TLT"), "1D", "net_edge_confidence"


def _parse_requirements(value: object) -> tuple[CampaignRequirement, ...]:
    if not isinstance(value, list):
        raise QQQCampaignReadinessError("requirements must be a list")
    requirements: list[CampaignRequirement] = []
    for index, item in enumerate(value):
        raw = _mapping(item, f"requirements[{index}]")
        try:
            code = RequirementCode(_string(raw.get("code"), f"requirements[{index}].code"))
            scope = RequirementScope(_string(raw.get("scope"), f"requirements[{index}].scope"))
            state = RequirementState(_string(raw.get("state"), f"requirements[{index}].state"))
        except ValueError as error:
            raise QQQCampaignReadinessError(f"requirements[{index}] has an unknown enum") from error
        requirements.append(
            CampaignRequirement(
                code=code,
                scope=scope,
                state=state,
                blocks_long_campaign=_boolean(
                    raw.get("blocks_long_campaign"),
                    f"requirements[{index}].blocks_long_campaign",
                ),
                detail=_string(raw.get("detail"), f"requirements[{index}].detail"),
            )
        )
    if tuple(requirement.code for requirement in requirements) != tuple(RequirementCode):
        raise QQQCampaignReadinessError("requirement codes or their order changed")

    owner_actions = {
        RequirementCode.READINESS_OWNER_APPROVAL,
        RequirementCode.CERTIFIED_SIX_SYMBOL_EXPORT,
        RequirementCode.INDEPENDENT_CORPORATE_ACTION_ATTESTATION,
        RequirementCode.OWNER_APPROVED_HOLDOUT_MAP,
        RequirementCode.BENCHMARK_AND_CASH_LEG_IDENTITY,
        RequirementCode.LONG_COST_SCHEDULE_IDENTITY,
        RequirementCode.TRADINGVIEW_TRACE_EXPORT,
    }
    chronos_builds = {
        RequirementCode.CERTIFIED_RELEASE_AND_CATALOG,
        RequirementCode.POWER_ANALYSIS_IDENTITY,
        RequirementCode.EVALUATOR_CRITERIA_CODE_REGISTRY_CAMPAIGN_IDENTITY,
        RequirementCode.TRADINGVIEW_PARITY_EVIDENCE,
        RequirementCode.BASE_FIVE_TOOL_BINDINGS,
    }
    deferred = {
        RequirementCode.AUTHENTICATED_MANAGEMENT_EVENT_IDENTITY,
        RequirementCode.BROKER_HELD_PROTECTION_SEMANTICS,
        RequirementCode.REAL_PAPER_LIFECYCLE_EVIDENCE,
    }
    for requirement in requirements:
        expected_state = (
            RequirementState.OWNER_ACTION_REQUIRED
            if requirement.code in owner_actions
            else RequirementState.CHRONOS_BUILD_REQUIRED
            if requirement.code in chronos_builds
            else RequirementState.DEFERRED_ACTIVATION
            if requirement.code in deferred
            else RequirementState.UNAVAILABLE
        )
        if requirement.state is not expected_state:
            raise QQQCampaignReadinessError(f"{requirement.code.value} state changed")
        if requirement.code is RequirementCode.BASE_FIVE_TOOL_BINDINGS:
            expected_scope = RequirementScope.CONFLUENCE_CANDIDATE
        elif requirement.code is RequirementCode.SHORT_SIDE_EVIDENCE:
            expected_scope = RequirementScope.SHORT_SIDE
        elif requirement.code in deferred:
            expected_scope = RequirementScope.PAPER_ACTIVATION
        else:
            expected_scope = RequirementScope.SHARED
        if requirement.scope is not expected_scope:
            raise QQQCampaignReadinessError(f"{requirement.code.value} scope changed")
        expected_blocks = requirement.code not in {
            RequirementCode.SHORT_SIDE_EVIDENCE,
            *deferred,
        }
        if requirement.blocks_long_campaign is not expected_blocks:
            raise QQQCampaignReadinessError(
                f"{requirement.code.value} long-campaign blocking status changed"
            )
    return tuple(requirements)


def _parse_data_boundaries(value: object) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    boundaries = _mapping(value, "data_boundaries")
    qqq_symbols = ("QQQ", "SPY", "IWM", "DIA", "GLD", "TLT")
    base_symbols = ("GLD", "IWM", "QQQ", "RSP", "SPY", "VIX", "VIX3M")
    _require_exact(
        boundaries.get("qqq_release_symbols"),
        list(qqq_symbols),
        "data_boundaries.qqq_release_symbols",
    )
    _require_exact(
        boundaries.get("base_five_tool_intake_symbols"),
        list(base_symbols),
        "data_boundaries.base_five_tool_intake_symbols",
    )
    _require_exact(
        boundaries.get("cross_dataset_identity_transfer"),
        "forbidden",
        "data_boundaries.cross_dataset_identity_transfer",
    )
    _require_exact(
        boundaries.get("base_five_tool_intake_status"),
        "separate_pending_certified_dataset_and_campaign_binding",
        "data_boundaries.base_five_tool_intake_status",
    )
    return qqq_symbols, base_symbols, "forbidden"


def _authenticate_artifacts(artifacts: tuple[ArtifactIdentity, ...]) -> None:
    by_code = {artifact.code: artifact for artifact in artifacts}
    root = _repo_root()
    try:
        control = compile_qqq_control(
            root / by_code[ArtifactCode.SMA_CONTROL].path,
            constitution_path=root / by_code[ArtifactCode.CONSTITUTION].path,
        )
        candidate = compile_qqq_confluence_candidate(
            root / by_code[ArtifactCode.CONFLUENCE_CANDIDATE].path,
            constitution_path=root / by_code[ArtifactCode.CONSTITUTION].path,
        )
    except (QQQControlSpecError, QQQConfluenceSpecError) as error:
        raise QQQCampaignReadinessError(f"referenced artifact drifted: {error}") from error
    for label, compiled in (("SMA control", control), ("Confluence candidate", candidate)):
        if (
            compiled.status.value != CampaignCompilationStatus.BLOCKED_BEFORE_FIRST_DATA_READ.value
            or compiled.order_authority != "none"
            or compiled.promotion_authority != "none"
            or compiled.registered_trials != 0
            or compiled.data_read_permitted
            or compiled.executable
        ):
            raise QQQCampaignReadinessError(f"{label} authority posture changed")
    if tuple(blocker.code for blocker in control.blockers) != tuple(ControlBlockerCode):
        raise QQQCampaignReadinessError("SMA control blocker set changed")
    if tuple(blocker.code for blocker in candidate.blockers) != tuple(CandidateBlockerCode):
        raise QQQCampaignReadinessError("Confluence candidate blocker set changed")
    _require_exact(
        control.constitution_sha256,
        by_code[ArtifactCode.CONSTITUTION].content_sha256,
        "compiled constitution identity",
    )
    _require_exact(
        control.preregistration_sha256,
        by_code[ArtifactCode.SMA_CONTROL].content_sha256,
        "compiled control identity",
    )
    _require_exact(
        candidate.candidate_sha256,
        by_code[ArtifactCode.CONFLUENCE_CANDIDATE].content_sha256,
        "compiled candidate identity",
    )
    for code in (ArtifactCode.PAPER_MANAGEMENT, ArtifactCode.MANAGED_POSITION_ADMISSION):
        artifact = by_code[code]
        try:
            observed = hashlib.sha256((root / artifact.path).read_bytes()).hexdigest()
        except OSError as error:
            raise QQQCampaignReadinessError(
                f"cannot authenticate {artifact.path}: {error}"
            ) from error
        if observed != artifact.content_sha256:
            raise QQQCampaignReadinessError(
                f"{artifact.path} drifted: expected {artifact.content_sha256}, observed {observed}"
            )


def _validate_document(
    document: dict[str, object],
) -> tuple[tuple[ArtifactIdentity, ...], tuple[CampaignRequirement, ...]]:
    if frozenset(document) != _ROOT_KEYS:
        missing = sorted(_ROOT_KEYS - frozenset(document))
        extra = sorted(frozenset(document) - _ROOT_KEYS)
        raise QQQCampaignReadinessError(f"root keys changed: missing={missing}, extra={extra}")
    _require_exact(document.get("schema_version"), SCHEMA_VERSION, "schema_version")
    _require_exact(document.get("readiness_id"), READINESS_ID, "readiness_id")
    _require_exact(
        document.get("status"),
        CampaignCompilationStatus.BLOCKED_BEFORE_FIRST_DATA_READ.value,
        "status",
    )
    authority = _mapping(document.get("authority"), "authority")
    _require_exact(authority.get("order_authority"), "none", "authority.order_authority")
    _require_exact(authority.get("promotion_authority"), "none", "authority.promotion_authority")
    _require_exact(authority.get("selected_strategy"), None, "authority.selected_strategy")
    _require_exact(authority.get("registered_trials"), 0, "authority.registered_trials")
    _require_exact(
        authority.get("live_risk_authorized_usd"), 0, "authority.live_risk_authorized_usd"
    )
    _require_exact(authority.get("performance_claims"), [], "authority.performance_claims")
    forbidden = document.get("forbidden_capabilities")
    if not isinstance(forbidden, list) or set(map(str, forbidden)) != _FORBIDDEN_CAPABILITIES:
        raise QQQCampaignReadinessError("forbidden capabilities changed")
    _parse_campaign(document.get("campaign"))
    _parse_data_boundaries(document.get("data_boundaries"))
    return (
        _parse_artifacts(document.get("artifacts")),
        _parse_requirements(document.get("requirements")),
    )


def _load_qqq_campaign_readiness(path: Path | None = None) -> tuple[str, dict[str, object]]:
    """Authenticate and validate the exact readiness document."""

    target = path or _default_readiness_path()
    try:
        payload = target.read_bytes()
    except OSError as error:
        raise QQQCampaignReadinessError(f"cannot read QQQ campaign readiness: {error}") from error
    digest = hashlib.sha256(payload).hexdigest()
    if digest != EXPECTED_READINESS_SHA256:
        raise QQQCampaignReadinessError(
            f"QQQ campaign readiness drifted: expected {EXPECTED_READINESS_SHA256}, "
            f"observed {digest}"
        )
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QQQCampaignReadinessError("QQQ campaign readiness is not valid JSON") from error
    document = _mapping(decoded, "readiness")
    artifacts, _ = _validate_document(document)
    _authenticate_artifacts(artifacts)
    return digest, document


def compile_qqq_campaign_readiness(
    path: Path | None = None,
) -> CompiledQQQCampaignReadiness:
    """Return authenticated blocked metadata and no runnable plan."""

    digest, document = _load_qqq_campaign_readiness(path)
    authority = _mapping(document["authority"], "authority")
    execution_symbol, robustness_panel_symbols, bar_interval, primary_kpi = _parse_campaign(
        document["campaign"]
    )
    qqq_release_symbols, base_five_tool_intake_symbols, identity_transfer = _parse_data_boundaries(
        document["data_boundaries"]
    )
    artifacts = _parse_artifacts(document["artifacts"])
    requirements = _parse_requirements(document["requirements"])
    return CompiledQQQCampaignReadiness(
        readiness_id=READINESS_ID,
        readiness_sha256=digest,
        status=CampaignCompilationStatus.BLOCKED_BEFORE_FIRST_DATA_READ,
        execution_symbol=execution_symbol,
        robustness_panel_symbols=robustness_panel_symbols,
        bar_interval=bar_interval,
        primary_kpi=primary_kpi,
        qqq_release_symbols=qqq_release_symbols,
        base_five_tool_intake_symbols=base_five_tool_intake_symbols,
        cross_dataset_identity_transfer=identity_transfer,
        artifacts=artifacts,
        requirements=requirements,
        order_authority=_string(authority.get("order_authority"), "order_authority"),
        promotion_authority=_string(authority.get("promotion_authority"), "promotion_authority"),
        registered_trials=_integer(authority.get("registered_trials"), "registered_trials"),
    )


__all__ = [
    "EXPECTED_READINESS_SHA256",
    "READINESS_ID",
    "SCHEMA_VERSION",
    "ArtifactCode",
    "ArtifactIdentity",
    "ArtifactState",
    "CampaignCompilationStatus",
    "CampaignRequirement",
    "CompiledQQQCampaignReadiness",
    "QQQCampaignReadinessError",
    "RequirementCode",
    "RequirementScope",
    "RequirementState",
    "compile_qqq_campaign_readiness",
]
