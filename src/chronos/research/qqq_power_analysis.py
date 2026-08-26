"""Content-addressed, no-data power arithmetic for the QQQ primary control.

This module freezes a prospective sample-size calculation; it is not an evaluator and
does not inspect returns.  Its only I/O authenticates the exact power specification and
the three QQQ design artifacts it binds.  The absolute pass date remains blocked until
the owner approves a clean OOS window and its first completed exchange session.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from statistics import NormalDist
from typing import cast

SCHEMA_VERSION = "chronos-qqq-power-analysis-v1"
ANALYSIS_ID = "qqq-power-analysis-v1-owner-review-2026-08-26"
EXPECTED_ANALYSIS_SHA256 = "414f2833d131f5ef628168d124bc15ef281247862d1d4861e5d1cc2e672f52a4"

_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "analysis_id",
        "status",
        "purpose",
        "bound_artifacts",
        "authority",
        "estimand",
        "power_design",
        "result",
        "invalidation_conditions",
        "blockers",
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


class QQQPowerAnalysisError(ValueError):
    """The power identity is malformed, stale, or overstates what it can prove."""


class PowerAnalysisStatus(StrEnum):
    BLOCKED_PENDING_CLEAN_WINDOW_IDENTITY = "blocked_pending_clean_window_identity"


class PowerBlockerCode(StrEnum):
    OWNER_APPROVAL_PENDING = "owner_approval_pending"
    CLEAN_WINDOW_IDENTITY_PENDING = "clean_window_identity_pending"
    ABSOLUTE_PASS_DATE_PENDING = "absolute_pass_date_pending"
    SUCCESSOR_CAMPAIGN_BINDING_PENDING = "successor_campaign_binding_pending"


@dataclass(frozen=True, slots=True)
class BoundArtifact:
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class PowerArithmetic:
    required_observations: int
    required_year_equivalents: float
    z_significance: float
    z_power: float


@dataclass(frozen=True, slots=True)
class PowerBlocker:
    code: PowerBlockerCode
    detail: str


@dataclass(frozen=True, slots=True)
class CompiledQQQPowerAnalysis:
    analysis_id: str
    analysis_sha256: str
    status: PowerAnalysisStatus
    bound_artifacts: tuple[BoundArtifact, ...]
    minimum_detectable_annualized_alpha_fraction: float
    annualized_long_run_tracking_error_ceiling_fraction: float
    confidence_lower_bound: float
    target_power: float
    annualization_sessions: int
    power_required_n: int
    power_required_n_unit: str
    required_year_equivalents: float
    minimum_oos_closed_positions: int
    earliest_pass_offset_completed_sessions_from_clean_start_inclusive: int
    earliest_possible_pass_date: None
    blockers: tuple[PowerBlocker, ...]
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
    def absolute_pass_date_resolved(self) -> bool:
        return False


def default_power_analysis_path() -> Path:
    return Path(__file__).resolve().parents[3] / "specs/qqq_power_analysis_v1.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def required_sample_size(
    *,
    minimum_detectable_annualized_alpha_fraction: float,
    annualized_long_run_tracking_error_fraction: float,
    type_i_error_alpha: float,
    target_power: float,
    annualization_sessions: int,
) -> PowerArithmetic:
    """Return the prospective normal-approximation sample size for a mean.

    The tracking-error input is the annualized square root of the long-run variance of
    benchmark-relative daily returns.  It therefore has to include serial dependence;
    an ordinary IID standard deviation is not a valid substitute.
    """

    numeric = (
        minimum_detectable_annualized_alpha_fraction,
        annualized_long_run_tracking_error_fraction,
        type_i_error_alpha,
        target_power,
    )
    if not all(math.isfinite(value) for value in numeric):
        raise ValueError("power inputs must be finite")
    if minimum_detectable_annualized_alpha_fraction <= 0.0:
        raise ValueError("minimum detectable alpha must be positive")
    if annualized_long_run_tracking_error_fraction <= 0.0:
        raise ValueError("long-run tracking error must be positive")
    if not 0.0 < type_i_error_alpha < 0.5:
        raise ValueError("type-I error alpha must lie strictly between zero and one-half")
    if not 0.5 < target_power < 1.0:
        raise ValueError("target power must lie strictly between one-half and one")
    if (
        isinstance(annualization_sessions, bool)
        or not isinstance(annualization_sessions, int)
        or annualization_sessions <= 0
    ):
        raise ValueError("annualization sessions must be a positive integer")

    normal = NormalDist()
    z_significance = normal.inv_cdf(1.0 - type_i_error_alpha)
    z_power = normal.inv_cdf(target_power)
    effect_ratio = (
        annualized_long_run_tracking_error_fraction / minimum_detectable_annualized_alpha_fraction
    )
    years = (z_significance + z_power) ** 2 * effect_ratio**2
    return PowerArithmetic(
        required_observations=math.ceil(years * annualization_sessions),
        required_year_equivalents=years,
        z_significance=z_significance,
        z_power=z_power,
    )


def _mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise QQQPowerAnalysisError(f"{context} must be an object")
    return cast(dict[str, object], value)


def _list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise QQQPowerAnalysisError(f"{context} must be a list")
    return cast(list[object], value)


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise QQQPowerAnalysisError(f"{context} must be a non-empty string")
    return value


def _integer(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise QQQPowerAnalysisError(f"{context} must be an integer")
    return value


def _number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise QQQPowerAnalysisError(f"{context} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise QQQPowerAnalysisError(f"{context} must be finite")
    return numeric


def _require_exact(value: object, expected: object, context: str) -> None:
    if value != expected:
        raise QQQPowerAnalysisError(f"{context} must remain {expected!r}")


def _parse_bound_artifacts(value: object) -> tuple[BoundArtifact, ...]:
    artifacts: list[BoundArtifact] = []
    for index, item in enumerate(_list(value, "bound_artifacts")):
        raw = _mapping(item, f"bound_artifacts[{index}]")
        if frozenset(raw) != {"path", "sha256"}:
            raise QQQPowerAnalysisError(f"bound_artifacts[{index}] keys changed")
        artifacts.append(
            BoundArtifact(
                path=_string(raw.get("path"), f"bound_artifacts[{index}].path"),
                sha256=_string(raw.get("sha256"), f"bound_artifacts[{index}].sha256"),
            )
        )
    expected = (
        BoundArtifact(
            "research/qqq_v1_constitution.json",
            "4c99ce9d09f43a418c7342b0e40a0795b253bf3f1cd0e37d29419498b3008d56",
        ),
        BoundArtifact(
            "specs/qqq_sma_control_v1.json",
            "a0ec83b3431016df0c599895ead65083fc72b5afb87073dfbdf046d68e23bb03",
        ),
        BoundArtifact(
            "specs/qqq_five_tool_candidate_v1.json",
            "59348ca3da9e9b68ec4edd1fc54572783e9256ae9c55ac18ffe844c0b4b78054",
        ),
    )
    if tuple(artifacts) != expected:
        raise QQQPowerAnalysisError("bound artifact identities or their order changed")
    return tuple(artifacts)


def _authenticate_bound_artifacts(artifacts: tuple[BoundArtifact, ...]) -> None:
    root = _repo_root()
    for artifact in artifacts:
        try:
            observed = hashlib.sha256((root / artifact.path).read_bytes()).hexdigest()
        except OSError as error:
            raise QQQPowerAnalysisError(f"cannot authenticate {artifact.path}: {error}") from error
        if observed != artifact.sha256:
            raise QQQPowerAnalysisError(
                f"{artifact.path} drifted: expected {artifact.sha256}, observed {observed}"
            )


def _validate_authority(value: object) -> tuple[str, str, int]:
    authority = _mapping(value, "authority")
    _require_exact(authority.get("order_authority"), "none", "authority.order_authority")
    _require_exact(authority.get("promotion_authority"), "none", "authority.promotion_authority")
    _require_exact(authority.get("selected_strategy"), None, "authority.selected_strategy")
    _require_exact(authority.get("registered_trials"), 0, "authority.registered_trials")
    _require_exact(authority.get("market_data_reads"), 0, "authority.market_data_reads")
    _require_exact(authority.get("holdout_unlocks"), 0, "authority.holdout_unlocks")
    _require_exact(authority.get("performance_claims"), [], "authority.performance_claims")
    return "none", "none", 0


def _validate_design(document: dict[str, object]) -> PowerArithmetic:
    estimand = _mapping(document.get("estimand"), "estimand")
    _require_exact(
        estimand.get("confirmatory_cell_id"),
        "qqq-sma200-immediate-primary",
        "estimand.confirmatory_cell_id",
    )
    _require_exact(
        estimand.get("observation_unit"),
        "one_completed_OOS_daily_session_net_strategy_return_minus_volatility_matched_"
        "QQQ_cash_benchmark_return",
        "estimand.observation_unit",
    )
    effect = _number(
        estimand.get("minimum_detectable_annualized_post_cost_alpha_fraction"),
        "estimand.minimum_detectable_annualized_post_cost_alpha_fraction",
    )
    _require_exact(effect, 0.04, "minimum detectable alpha")
    _require_exact(
        estimand.get("null_hypothesis"),
        "annualized_mean_benchmark_relative_net_return_lte_0",
        "estimand.null_hypothesis",
    )
    _require_exact(
        estimand.get("alternative_at_power_point"),
        "annualized_mean_benchmark_relative_net_return_eq_0.04",
        "estimand.alternative_at_power_point",
    )
    sessions = _integer(estimand.get("annualization_sessions"), "annualization_sessions")
    _require_exact(sessions, 252, "annualization_sessions")
    _require_exact(
        estimand.get("cross_instrument_pooling"), "forbidden", "cross_instrument_pooling"
    )
    _require_exact(
        estimand.get("robustness_cell_treatment"),
        "not_selectable_substitutes_for_the_powered_primary_cell",
        "robustness_cell_treatment",
    )

    design = _mapping(document.get("power_design"), "power_design")
    confidence = _number(design.get("confidence_lower_bound"), "confidence_lower_bound")
    _require_exact(confidence, 0.95, "confidence_lower_bound")
    _require_exact(design.get("confidence_tail"), "one_sided_lower", "confidence_tail")
    alpha = _number(design.get("type_i_error_alpha"), "type_i_error_alpha")
    power = _number(design.get("target_power"), "target_power")
    beta = _number(design.get("type_ii_error_beta"), "type_ii_error_beta")
    tracking_error = _number(
        design.get("annualized_long_run_tracking_error_ceiling_fraction"),
        "annualized_long_run_tracking_error_ceiling_fraction",
    )
    _require_exact(alpha, 0.05, "type_i_error_alpha")
    _require_exact(power, 0.8, "target_power")
    _require_exact(beta, 0.2, "type_ii_error_beta")
    _require_exact(tracking_error, 0.08, "long-run tracking-error ceiling")
    _require_exact(
        design.get("minimum_information_ratio_at_power_point"),
        0.5,
        "minimum_information_ratio_at_power_point",
    )
    _require_exact(
        design.get("formula"),
        "ceil((z_1_minus_alpha+z_power)^2*(annualized_long_run_tracking_error/"
        "minimum_detectable_annualized_alpha)^2*annualization_sessions)",
        "power formula",
    )
    _require_exact(
        design.get("dependence_treatment"),
        "tracking_error_is_the_annualized_square_root_of_the_long_run_variance_of_daily_"
        "benchmark_relative_returns_and_must_include_serial_dependence",
        "dependence treatment",
    )
    _require_exact(
        design.get("multiplicity_treatment"),
        "only_the_preregistered_primary_cell_is_powered;_all_attempts_still_count_for_DSR_"
        "and_the_frozen_FWER_or_FDR_gate",
        "multiplicity treatment",
    )
    return required_sample_size(
        minimum_detectable_annualized_alpha_fraction=effect,
        annualized_long_run_tracking_error_fraction=tracking_error,
        type_i_error_alpha=alpha,
        target_power=power,
        annualization_sessions=sessions,
    )


def _validate_result(value: object, arithmetic: PowerArithmetic) -> None:
    result = _mapping(value, "result")
    required_n = _integer(result.get("power_required_N"), "result.power_required_N")
    _require_exact(required_n, arithmetic.required_observations, "result.power_required_N")
    _require_exact(
        result.get("power_required_N_unit"),
        "completed_OOS_daily_session_returns",
        "result.power_required_N_unit",
    )
    years = _number(result.get("required_year_equivalents"), "required_year_equivalents")
    if not math.isclose(years, arithmetic.required_year_equivalents, abs_tol=5e-11):
        raise QQQPowerAnalysisError("required_year_equivalents does not match the frozen formula")
    _require_exact(result.get("minimum_OOS_closed_positions"), 100, "minimum_OOS_closed_positions")
    _require_exact(
        result.get("sample_gate_composition"),
        "both_requirements_must_pass_independently;_a_numeric_max_across_session_returns_"
        "and_positions_is_forbidden",
        "sample_gate_composition",
    )
    _require_exact(
        result.get("earliest_pass_offset_completed_sessions_from_clean_start_inclusive"),
        arithmetic.required_observations - 1,
        "earliest pass offset",
    )
    _require_exact(result.get("earliest_possible_pass_date"), None, "earliest_possible_pass_date")


def _validate_document(
    document: dict[str, object],
) -> tuple[tuple[BoundArtifact, ...], PowerArithmetic, tuple[str, str, int]]:
    if frozenset(document) != _ROOT_KEYS:
        missing = sorted(_ROOT_KEYS - frozenset(document))
        extra = sorted(frozenset(document) - _ROOT_KEYS)
        raise QQQPowerAnalysisError(f"root keys changed: missing={missing}, extra={extra}")
    _require_exact(document.get("schema_version"), SCHEMA_VERSION, "schema_version")
    _require_exact(document.get("analysis_id"), ANALYSIS_ID, "analysis_id")
    _require_exact(
        document.get("status"),
        PowerAnalysisStatus.BLOCKED_PENDING_CLEAN_WINDOW_IDENTITY.value,
        "status",
    )
    artifacts = _parse_bound_artifacts(document.get("bound_artifacts"))
    authority = _validate_authority(document.get("authority"))
    arithmetic = _validate_design(document)
    _validate_result(document.get("result"), arithmetic)

    invalidations = _list(document.get("invalidation_conditions"), "invalidation_conditions")
    if len(invalidations) != 5 or any(
        not isinstance(item, str) or not item for item in invalidations
    ):
        raise QQQPowerAnalysisError("five explicit invalidation conditions are required")
    blockers = _list(document.get("blockers"), "blockers")
    if len(blockers) != len(PowerBlockerCode) or any(
        not isinstance(item, str) or not item for item in blockers
    ):
        raise QQQPowerAnalysisError("the four unresolved blockers must remain explicit")
    forbidden = _list(document.get("forbidden_capabilities"), "forbidden_capabilities")
    if set(map(str, forbidden)) != _FORBIDDEN_CAPABILITIES:
        raise QQQPowerAnalysisError("forbidden capabilities changed")
    return artifacts, arithmetic, authority


def load_qqq_power_analysis(path: Path | None = None) -> tuple[str, dict[str, object]]:
    """Authenticate the exact power artifact and every design artifact it binds."""

    target = path or default_power_analysis_path()
    try:
        payload = target.read_bytes()
    except OSError as error:
        raise QQQPowerAnalysisError(f"cannot read QQQ power analysis: {error}") from error
    digest = hashlib.sha256(payload).hexdigest()
    if digest != EXPECTED_ANALYSIS_SHA256:
        raise QQQPowerAnalysisError(
            f"QQQ power analysis drifted: expected {EXPECTED_ANALYSIS_SHA256}, observed {digest}"
        )
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QQQPowerAnalysisError("QQQ power analysis is not valid JSON") from error
    document = _mapping(decoded, "power analysis")
    artifacts, _, _ = _validate_document(document)
    _authenticate_bound_artifacts(artifacts)
    return digest, document


def compile_qqq_power_analysis(path: Path | None = None) -> CompiledQQQPowerAnalysis:
    """Return frozen relative arithmetic and preserve every unresolved absolute blocker."""

    digest, document = load_qqq_power_analysis(path)
    artifacts, arithmetic, authority = _validate_document(document)
    estimand = _mapping(document["estimand"], "estimand")
    design = _mapping(document["power_design"], "power_design")
    result = _mapping(document["result"], "result")
    blocker_details = tuple(
        _string(item, f"blockers[{index}]")
        for index, item in enumerate(_list(document["blockers"], "blockers"))
    )
    blockers = tuple(
        PowerBlocker(code, detail)
        for code, detail in zip(PowerBlockerCode, blocker_details, strict=True)
    )
    return CompiledQQQPowerAnalysis(
        analysis_id=ANALYSIS_ID,
        analysis_sha256=digest,
        status=PowerAnalysisStatus.BLOCKED_PENDING_CLEAN_WINDOW_IDENTITY,
        bound_artifacts=artifacts,
        minimum_detectable_annualized_alpha_fraction=_number(
            estimand["minimum_detectable_annualized_post_cost_alpha_fraction"],
            "minimum detectable alpha",
        ),
        annualized_long_run_tracking_error_ceiling_fraction=_number(
            design["annualized_long_run_tracking_error_ceiling_fraction"],
            "long-run tracking error",
        ),
        confidence_lower_bound=_number(design["confidence_lower_bound"], "confidence lower bound"),
        target_power=_number(design["target_power"], "target power"),
        annualization_sessions=_integer(
            estimand["annualization_sessions"], "annualization sessions"
        ),
        power_required_n=arithmetic.required_observations,
        power_required_n_unit=_string(result["power_required_N_unit"], "power_required_N_unit"),
        required_year_equivalents=arithmetic.required_year_equivalents,
        minimum_oos_closed_positions=_integer(
            result["minimum_OOS_closed_positions"], "minimum_OOS_closed_positions"
        ),
        earliest_pass_offset_completed_sessions_from_clean_start_inclusive=_integer(
            result["earliest_pass_offset_completed_sessions_from_clean_start_inclusive"],
            "earliest pass offset",
        ),
        earliest_possible_pass_date=None,
        blockers=blockers,
        order_authority=authority[0],
        promotion_authority=authority[1],
        registered_trials=authority[2],
    )


__all__ = [
    "ANALYSIS_ID",
    "EXPECTED_ANALYSIS_SHA256",
    "SCHEMA_VERSION",
    "BoundArtifact",
    "CompiledQQQPowerAnalysis",
    "PowerAnalysisStatus",
    "PowerArithmetic",
    "PowerBlocker",
    "PowerBlockerCode",
    "QQQPowerAnalysisError",
    "compile_qqq_power_analysis",
    "default_power_analysis_path",
    "load_qqq_power_analysis",
    "required_sample_size",
]
