"""Refuse-closed QQQ overlay for the pinned Five-Tool Confluence candidate.

The base Five-Tool contract already translates all 219 Pine inputs.  This module does
not duplicate that engine.  It authenticates the exact source/contract/campaign bytes,
validates the QQQ-specific causal price and risk overlay, and returns blocked metadata.
It owns no dataset reader, trial registry, holdout, broker, order, or promotion capability.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from chronos.research.five_tool.contract import (
    default_input_values,
    input_contract_digest,
    load_contract,
    semantic_contract_digest,
)

SCHEMA_VERSION = "chronos-qqq-five-tool-candidate-v1"
CANDIDATE_ID = "qqq-five-tool-confluence-v3.6-integration-v1"
EXPECTED_CANDIDATE_SHA256 = "59348ca3da9e9b68ec4edd1fc54572783e9256ae9c55ac18ffe844c0b4b78054"

_PINE_SHA256 = "e51d5a40d2e933bf86847c7432364ba8934fd2de653d6aec3d7205639248e45f"
_CONTRACT_FILE_SHA256 = "a8e69b26c9e23a9c5b0674a8895c28a540665aa116c05781c1a6d2cfeddb769f"
_INPUT_CONTRACT_DIGEST = "93273762b1d01dade4133628a9a2cebf0a1364774fde654a9efc07c4ccf6d049"
_SEMANTIC_CONTRACT_DIGEST = "c0d85a17b26b9da3c1e68dd16fd341288fd3710021a7dbf9a07c953c3a9fe196"
_CAMPAIGN_FILE_SHA256 = "995d4b5c12e9ff442651c54a8568d3de8bbcfd591059eea4d4965f4414172170"
_CONSTITUTION_SHA256 = "4c99ce9d09f43a418c7342b0e40a0795b253bf3f1cd0e37d29419498b3008d56"

_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "status",
        "purpose",
        "governance",
        "base_five_tool_identity",
        "authority",
        "scope",
        "source_native_effective_defaults",
        "causal_price_domains",
        "entry",
        "position_plan",
        "risk_and_sizing",
        "costs",
        "pre_handoff_revalidation",
        "identity_locks",
        "blocked_before_first_data_read",
        "forbidden_imports_or_capabilities",
    }
)


class QQQConfluenceSpecError(ValueError):
    """The QQQ overlay is malformed, stale, or no longer safely blocked."""


class CandidateCompilationStatus(StrEnum):
    BLOCKED_BEFORE_FIRST_DATA_READ = "blocked_before_first_data_read"


class CandidateBlockerCode(StrEnum):
    OWNER_APPROVAL_PENDING = "owner_approval_pending"
    BASE_CAMPAIGN_PENDING = "base_campaign_pending"
    CERTIFIED_DATA_PENDING = "certified_data_pending"
    IDENTITY_LOCKS_PENDING = "identity_locks_pending"
    TRADINGVIEW_PARITY_PENDING = "tradingview_parity_pending"
    PAPER_LIFECYCLE_PENDING = "paper_lifecycle_pending"
    SHORT_EVIDENCE_PENDING = "short_evidence_pending"


@dataclass(frozen=True, slots=True)
class CandidateBlocker:
    code: CandidateBlockerCode
    detail: str


@dataclass(frozen=True, slots=True)
class CompiledQQQConfluenceCandidate:
    candidate_id: str
    candidate_sha256: str
    constitution_sha256: str
    status: CandidateCompilationStatus
    pine_sha256: str
    input_contract_digest: str
    semantic_contract_digest: str
    source_input_count: int
    blockers: tuple[CandidateBlocker, ...]
    order_authority: str
    promotion_authority: str
    registered_trials: int

    @property
    def data_read_permitted(self) -> bool:
        return False

    @property
    def executable(self) -> bool:
        return False


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_candidate_path() -> Path:
    return _repo_root() / "specs/qqq_five_tool_candidate_v1.json"


def default_constitution_path() -> Path:
    return _repo_root() / "research/qqq_v1_constitution.json"


def _mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise QQQConfluenceSpecError(f"{context} must be an object")
    return cast(dict[str, object], value)


def _list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise QQQConfluenceSpecError(f"{context} must be a list")
    return cast(list[object], value)


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise QQQConfluenceSpecError(f"{context} must be a non-empty string")
    return value


def _integer(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise QQQConfluenceSpecError(f"{context} must be an integer")
    return value


def _require_exact(value: object, expected: object, context: str) -> None:
    if value != expected:
        raise QQQConfluenceSpecError(f"{context} must remain {expected!r}")


def _file_sha256(relative_path: str) -> str:
    target = _repo_root() / relative_path
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError as error:
        raise QQQConfluenceSpecError(f"cannot authenticate {relative_path}: {error}") from error


def _validate_base_identity(base: dict[str, object]) -> None:
    expected_files = (
        ("pine_path", "pine_sha256", _PINE_SHA256),
        ("input_contract_path", "input_contract_file_sha256", _CONTRACT_FILE_SHA256),
        ("blocked_campaign_path", "blocked_campaign_file_sha256", _CAMPAIGN_FILE_SHA256),
    )
    for path_key, digest_key, expected in expected_files:
        relative = _string(base.get(path_key), f"base_five_tool_identity.{path_key}")
        _require_exact(base.get(digest_key), expected, f"base_five_tool_identity.{digest_key}")
        actual = _file_sha256(relative)
        if actual != expected:
            raise QQQConfluenceSpecError(
                f"{relative} drifted: expected {expected}, observed {actual}"
            )

    contract = load_contract()
    _require_exact(contract.strategy_id, "five_tool_confluence_v3_6", "strategy_id")
    _require_exact(contract.pine.source_sha256, _PINE_SHA256, "contract Pine identity")
    _require_exact(len(contract.inputs), 219, "source input count")
    _require_exact(base.get("source_input_count"), 219, "declared source input count")
    _require_exact(base.get("pine_overrides"), {}, "Pine overrides")
    _require_exact(
        base.get("input_contract_digest"), _INPUT_CONTRACT_DIGEST, "input contract digest"
    )
    _require_exact(
        base.get("semantic_contract_digest"),
        _SEMANTIC_CONTRACT_DIGEST,
        "semantic contract digest",
    )
    _require_exact(input_contract_digest(), _INPUT_CONTRACT_DIGEST, "live input contract")
    _require_exact(semantic_contract_digest(), _SEMANTIC_CONTRACT_DIGEST, "live semantic contract")


def _validate_source_defaults(values: Mapping[str, object], declared: dict[str, object]) -> None:
    expected_inputs: dict[str, object] = {
        "preset_input": "Auto",
        "use_vol_percentile_adjustment": True,
        "use_hysteresis": True,
        "use_ema_filter": True,
        "ema_filter_len": 100,
        "trig_flip": True,
        "trig_hidden": True,
        "trig_regular": True,
        "trig_reclaim": True,
        "min_score": 55,
        "allow_shorts": False,
        "use_short_side_v2": True,
        "short_plus_enabled": True,
        "use_long_side_v2": False,
        "long_plus_enabled": False,
        "use_time_stop": False,
        "exit_on_neutral": False,
        "risk_pct": 1.0,
        "atr_len": 14,
        "atr_mult": 2.0,
        "t1_r": 1.0,
        "t2_r": 2.0,
        "be_after_t1": True,
        "use_trail": True,
        "ch_len": 22,
        "ch_mult": 3.0,
        "trail_after_r": 1.0,
    }
    for name, expected in expected_inputs.items():
        _require_exact(values.get(name), expected, f"source default {name}")

    _require_exact(declared.get("preset"), "Auto_resolves_to_Daily", "daily preset")
    _require_exact(declared.get("regime_lookback"), 20, "regime lookback")
    _require_exact(declared.get("regime_enter_z"), 0.85, "regime enter z")
    _require_exact(declared.get("regime_exit_z"), 0.55, "regime exit z")
    _require_exact(declared.get("regime_confirmation_closes"), 2, "regime confirmation")
    _require_exact(declared.get("ema_length"), 100, "EMA length")
    _require_exact(declared.get("minimum_score"), 55, "minimum score")
    _require_exact(declared.get("master_allow_shorts"), False, "master short switch")
    _require_exact(declared.get("dedicated_short_v2"), True, "short v2 default")
    _require_exact(declared.get("short_plus"), True, "SHORT+ default")
    _require_exact(declared.get("dedicated_long_v2"), False, "long v2 default")
    _require_exact(declared.get("long_plus"), False, "LONG+ default")


def _validate_document(document: dict[str, object]) -> None:
    if frozenset(document) != _ROOT_KEYS:
        missing = sorted(_ROOT_KEYS - frozenset(document))
        extra = sorted(frozenset(document) - _ROOT_KEYS)
        raise QQQConfluenceSpecError(f"root keys changed: missing={missing}, extra={extra}")
    _require_exact(document.get("schema_version"), SCHEMA_VERSION, "schema_version")
    _require_exact(document.get("candidate_id"), CANDIDATE_ID, "candidate_id")
    _require_exact(
        document.get("status"),
        CandidateCompilationStatus.BLOCKED_BEFORE_FIRST_DATA_READ.value,
        "status",
    )

    governance = _mapping(document.get("governance"), "governance")
    _require_exact(
        governance.get("constitution_path"),
        "research/qqq_v1_constitution.json",
        "constitution path",
    )
    _require_exact(
        governance.get("constitution_sha256"), _CONSTITUTION_SHA256, "constitution identity"
    )
    _require_exact(governance.get("integration_boundary"), "ADR-0032", "integration ADR")
    _require_exact(
        governance.get("control_evidence_transfer"), "forbidden", "control evidence transfer"
    )

    _validate_base_identity(_mapping(document.get("base_five_tool_identity"), "base identity"))
    _validate_source_defaults(
        default_input_values(),
        _mapping(document.get("source_native_effective_defaults"), "source defaults"),
    )

    authority = _mapping(document.get("authority"), "authority")
    _require_exact(authority.get("order_authority"), "none", "order authority")
    _require_exact(authority.get("promotion_authority"), "none", "promotion authority")
    _require_exact(authority.get("selected_strategy"), None, "selected strategy")
    _require_exact(authority.get("registered_trials"), 0, "registered trials")
    _require_exact(authority.get("live_risk_authorized_usd"), 0, "live risk")
    _require_exact(authority.get("performance_claims"), [], "performance claims")
    _require_exact(authority.get("short_execution"), "forbidden", "short execution")

    scope = _mapping(document.get("scope"), "scope")
    _require_exact(scope.get("execution_target_symbol"), "QQQ", "target symbol")
    _require_exact(scope.get("bar_interval"), "1D", "bar interval")
    _require_exact(scope.get("history_start_utc"), None, "history start")
    _require_exact(scope.get("same_bar_action"), False, "same-bar action")

    domains = _mapping(document.get("causal_price_domains"), "causal price domains")
    decision_ohlc = _mapping(domains.get("decision_ohlc"), "decision OHLC")
    _require_exact(
        decision_ohlc.get("domain"),
        "point_in_time_total_return_OHLC_rebased_to_current_raw_close",
        "decision OHLC domain",
    )
    _require_exact(domains.get("ema100"), "decision_ohlc_close", "EMA domain")
    _require_exact(
        domains.get("atr14_gap_and_volatility"),
        "decision_ohlc_true_range",
        "ATR domain",
    )
    _require_exact(domains.get("domain_mixing"), "forbidden", "domain mixing")

    entry = _mapping(document.get("entry"), "entry")
    _require_exact(entry.get("state_requirement"), "flat", "entry state")
    _require_exact(entry.get("one_attempt_per_event"), True, "entry attempt count")
    _require_exact(entry.get("later_retry_chase_or_in_position_add"), False, "entry retry")
    _require_exact(entry.get("market_protection_collar_fraction"), 0.01, "market collar")

    position = _mapping(document.get("position_plan"), "position plan")
    _require_exact(position.get("target_1_r"), 1.0, "target 1")
    _require_exact(position.get("target_2_r"), 2.0, "target 2")
    _require_exact(position.get("break_even_after_target_1"), True, "breakeven")
    _require_exact(
        position.get("durable_paper_lifecycle"),
        "not_implemented_by_this_candidate_spec",
        "paper lifecycle status",
    )

    risk = _mapping(document.get("risk_and_sizing"), "risk and sizing")
    _require_exact(
        risk.get("applicable_capital_base_usd"),
        "min(marked_strategy_nav_usd,3000)",
        "applicable capital base",
    )
    _require_exact(risk.get("native_stop_risk_fraction"), 0.01, "native stop risk")
    _require_exact(risk.get("native_stop_risk_usd_max"), 30, "native stop-risk dollars")
    outer_cvar = _mapping(risk.get("outer_daily_cvar"), "outer daily CVaR")
    _require_exact(
        outer_cvar,
        {
            "confidence": 0.95,
            "lookback_completed_returns": 252,
            "direction_specific_tail_observations": 13,
            "max_loss_fraction_of_applicable_base": 0.015,
            "max_loss_usd": 45,
            "observation": (
                "one_session_direction_specific_loss_fraction_on_one_USD_of_unlevered_QQQ_"
                "exposure_from_the_point_in_time_total_return_close_to_close_return"
            ),
            "long_loss_fraction": "max(0,-one_session_total_return)",
            "short_loss_fraction": ("unavailable_until_certified_borrow_and_cost_evidence_exists"),
            "estimator": (
                "arithmetic_mean_of_the_13_greatest_loss_fractions_in_the_252_completed_"
                "return_window"
            ),
            "required_value": "finite_and_strictly_positive_otherwise_no_new_exposure",
        },
        "outer daily CVaR",
    )
    quantity_components = _mapping(risk.get("quantity_components"), "quantity components")
    _require_exact(
        quantity_components,
        {
            "signal_time_entry_basis_usd": "confirmed_raw_close_t",
            "handoff_entry_basis_usd": (
                "max(confirmed_raw_close_t,next_session_protected_buy_limit)"
            ),
            "signal_time_active_initial_stop_usd": (
                "active_side_structural_stop_usd_when_supplied_otherwise_confirmed_raw_"
                "close_t-2.0*ATR14_usd"
            ),
            "signal_time_native_stop_distance_usd": (
                "confirmed_raw_close_t-signal_time_active_initial_stop_usd"
            ),
            "handoff_native_stop_distance_usd": (
                "max(signal_time_native_stop_distance_usd,next_session_protected_buy_limit-"
                "signal_time_active_initial_stop_usd)"
            ),
            "native_stop_loss_budget_usd": "min(0.01*applicable_capital_base_usd,30)",
            "signal_time_native_stop_quantity": (
                "native_stop_loss_budget_usd/signal_time_native_stop_distance_usd"
            ),
            "handoff_native_stop_quantity": (
                "native_stop_loss_budget_usd/handoff_native_stop_distance_usd"
            ),
            "cvar_loss_budget_usd": "min(0.015*applicable_capital_base_usd,45)",
            "signal_time_cvar_quantity": (
                "cvar_loss_budget_usd/(signal_time_entry_basis_usd*"
                "direction_specific_unit_exposure_cvar_loss_fraction)"
            ),
            "handoff_cvar_quantity": (
                "cvar_loss_budget_usd/(handoff_entry_basis_usd*"
                "direction_specific_unit_exposure_cvar_loss_fraction)"
            ),
            "signal_time_gross_quantity": (
                "1.0*applicable_capital_base_usd/signal_time_entry_basis_usd"
            ),
            "handoff_gross_quantity": ("1.0*applicable_capital_base_usd/handoff_entry_basis_usd"),
            "signal_time_leverage_quantity": (
                "1.0*applicable_capital_base_usd/signal_time_entry_basis_usd"
            ),
            "handoff_leverage_quantity": (
                "1.0*applicable_capital_base_usd/handoff_entry_basis_usd"
            ),
            "signal_time_affordability_quantity": (
                "greatest_nonnegative_whole_QQQ_quantity_whose_signal_time_entry_notional_"
                "plus_projected_all_in_entry_and_exit_costs_does_not_exceed_fresh_settled_"
                "cash_after_owner_cash_floor"
            ),
            "handoff_affordability_quantity": (
                "greatest_nonnegative_whole_QQQ_quantity_whose_handoff_entry_notional_plus_"
                "projected_all_in_entry_and_exit_costs_does_not_exceed_fresh_settled_cash_"
                "after_owner_cash_floor"
            ),
            "owner_policy_quantity": (
                "fresh_owner_mandate_and_account_policy_ceiling_shares_for_QQQ"
            ),
            "missing_stale_nonfinite_nonpositive_or_uncertifiable_input": "no_new_exposure",
        },
        "quantity components",
    )
    _require_exact(
        risk.get("signal_time_permitted_quantity"),
        "floor_toward_zero(max(0,min(signal_time_native_stop_quantity,"
        "signal_time_cvar_quantity,signal_time_gross_quantity,"
        "signal_time_leverage_quantity,signal_time_affordability_quantity,"
        "owner_policy_quantity)))",
        "signal-time permitted quantity",
    )
    _require_exact(
        risk.get("handoff_permitted_quantity"),
        "min(signal_time_permitted_quantity,floor_toward_zero(max(0,min("
        "handoff_native_stop_quantity,handoff_cvar_quantity,handoff_gross_quantity,"
        "handoff_leverage_quantity,handoff_affordability_quantity,"
        "owner_policy_quantity))))",
        "handoff permitted quantity",
    )
    _require_exact(risk.get("gross_exposure_fraction_max"), 1.0, "gross limit")
    _require_exact(risk.get("leverage_max"), 1.0, "leverage limit")
    _require_exact(risk.get("in_position_upsize"), "forbidden", "in-position upsize")
    _require_exact(risk.get("observed_daily_and_session_loss_halt_fraction"), 0.02, "loss halt")
    _require_exact(risk.get("peak_to_trough_drawdown_halt_fraction"), 0.1, "drawdown halt")

    checks = _list(document.get("pre_handoff_revalidation"), "pre-handoff revalidation")
    if len(checks) != 9 or len(set(map(str, checks))) != len(checks):
        raise QQQConfluenceSpecError("nine unique pre-handoff checks are required")
    locks = _mapping(document.get("identity_locks"), "identity locks")
    if not locks or any(value is not None for value in locks.values()):
        raise QQQConfluenceSpecError("v1 candidate identity locks must remain unresolved")
    blockers = _list(
        document.get("blocked_before_first_data_read"), "blocked_before_first_data_read"
    )
    if len(blockers) != 7 or any(not isinstance(item, str) or not item for item in blockers):
        raise QQQConfluenceSpecError("seven explicit pre-data blockers are required")
    forbidden = set(
        map(
            str,
            _list(
                document.get("forbidden_imports_or_capabilities"),
                "forbidden_imports_or_capabilities",
            ),
        )
    )
    if forbidden != {
        "market_data_reader",
        "holdout_unlock",
        "trial_registration",
        "broker",
        "order_submission",
        "promotion",
    }:
        raise QQQConfluenceSpecError("forbidden capabilities changed")


def _verify_constitution(path: Path) -> str:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise QQQConfluenceSpecError(f"cannot read QQQ constitution: {error}") from error
    digest = hashlib.sha256(payload).hexdigest()
    if digest != _CONSTITUTION_SHA256:
        raise QQQConfluenceSpecError(
            f"QQQ constitution drifted: expected {_CONSTITUTION_SHA256}, observed {digest}"
        )
    return digest


def load_qqq_confluence_candidate(
    path: Path | None = None,
    *,
    constitution_path: Path | None = None,
) -> tuple[str, dict[str, object]]:
    """Authenticate and validate the exact QQQ candidate overlay."""

    target = path or default_candidate_path()
    try:
        payload = target.read_bytes()
    except OSError as error:
        raise QQQConfluenceSpecError(f"cannot read QQQ candidate overlay: {error}") from error
    digest = hashlib.sha256(payload).hexdigest()
    if digest != EXPECTED_CANDIDATE_SHA256:
        raise QQQConfluenceSpecError(
            f"QQQ candidate overlay drifted: expected {EXPECTED_CANDIDATE_SHA256}, "
            f"observed {digest}"
        )
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QQQConfluenceSpecError("QQQ candidate overlay is not valid JSON") from error
    document = _mapping(decoded, "candidate overlay")
    _validate_document(document)
    _verify_constitution(constitution_path or default_constitution_path())
    return digest, document


def compile_qqq_confluence_candidate(
    path: Path | None = None,
    *,
    constitution_path: Path | None = None,
) -> CompiledQQQConfluenceCandidate:
    """Return the authenticated identity and typed blockers, never a runnable plan."""

    digest, document = load_qqq_confluence_candidate(
        path,
        constitution_path=constitution_path,
    )
    authority = _mapping(document["authority"], "authority")
    blockers = (
        CandidateBlocker(
            CandidateBlockerCode.OWNER_APPROVAL_PENDING,
            "owner approval is recorded only by merge of this exact identity",
        ),
        CandidateBlocker(
            CandidateBlockerCode.BASE_CAMPAIGN_PENDING,
            "the base Five-Tool ablation and execution bindings remain unresolved",
        ),
        CandidateBlocker(
            CandidateBlockerCode.CERTIFIED_DATA_PENDING,
            "certified data and an owner-approved unopened holdout map are absent",
        ),
        CandidateBlocker(
            CandidateBlockerCode.IDENTITY_LOCKS_PENDING,
            "history, settings, benchmark, cost, power, evaluator, criteria, "
            "and code locks are absent",
        ),
        CandidateBlocker(
            CandidateBlockerCode.TRADINGVIEW_PARITY_PENDING,
            "TradingView trace and execution parity remain unverified",
        ),
        CandidateBlocker(
            CandidateBlockerCode.PAPER_LIFECYCLE_PENDING,
            "the durable paper position-management lifecycle is absent",
        ),
        CandidateBlocker(
            CandidateBlockerCode.SHORT_EVIDENCE_PENDING,
            "short compiler, borrow, account, legal, tax, and owner evidence are absent",
        ),
    )
    return CompiledQQQConfluenceCandidate(
        candidate_id=CANDIDATE_ID,
        candidate_sha256=digest,
        constitution_sha256=_CONSTITUTION_SHA256,
        status=CandidateCompilationStatus.BLOCKED_BEFORE_FIRST_DATA_READ,
        pine_sha256=_PINE_SHA256,
        input_contract_digest=_INPUT_CONTRACT_DIGEST,
        semantic_contract_digest=_SEMANTIC_CONTRACT_DIGEST,
        source_input_count=219,
        blockers=blockers,
        order_authority=_string(authority.get("order_authority"), "order authority"),
        promotion_authority=_string(authority.get("promotion_authority"), "promotion authority"),
        registered_trials=_integer(authority.get("registered_trials"), "registered trials"),
    )


__all__ = [
    "CANDIDATE_ID",
    "EXPECTED_CANDIDATE_SHA256",
    "SCHEMA_VERSION",
    "CandidateBlocker",
    "CandidateBlockerCode",
    "CandidateCompilationStatus",
    "CompiledQQQConfluenceCandidate",
    "QQQConfluenceSpecError",
    "compile_qqq_confluence_candidate",
    "default_candidate_path",
    "default_constitution_path",
    "load_qqq_confluence_candidate",
]
