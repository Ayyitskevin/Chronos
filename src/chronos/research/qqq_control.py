"""Typed, refuse-closed loader for the QQQ SMA control preregistration.

This module is specification infrastructure, not a strategy runner.  Its only I/O is
reading the exact preregistration JSON.  It deliberately imports no market-data reader,
registry, holdout, broker, order, execution, promotion, or runtime capability.  The v1
artifact is blocked before the first data read; compiling it can describe the planned
cells and blockers but can never manufacture readiness.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

SCHEMA_VERSION = "chronos-qqq-sma-control-v1"
PREREGISTRATION_ID = "qqq-sma-control-v1-owner-review-2026-08-25"
EXPECTED_PREREGISTRATION_SHA256 = "06465d4541abd35119092176b3da71f958d3acb6f7dc6d3ccaa97fd5586991da"
EXPECTED_CONSTITUTION_SHA256 = "4c99ce9d09f43a418c7342b0e40a0795b253bf3f1cd0e37d29419498b3008d56"

_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "preregistration_id",
        "status",
        "purpose",
        "constitution",
        "authority",
        "scope",
        "price_domains",
        "signal",
        "risk_and_sizing",
        "entry_and_order_semantics",
        "minimum_economic_trade",
        "cells",
        "multiplicity",
        "statistics",
        "identity_locks",
        "blocked_before_first_data_read",
        "forbidden_imports_or_capabilities",
    }
)


class QQQControlSpecError(ValueError):
    """The preregistration is malformed, stale, or no longer safely blocked."""


class ControlCompilationStatus(StrEnum):
    BLOCKED_BEFORE_FIRST_DATA_READ = "blocked_before_first_data_read"


class ControlBlockerCode(StrEnum):
    OWNER_APPROVAL_PENDING = "owner_approval_pending"
    CERTIFIED_DATA_PENDING = "certified_data_pending"
    HOLDOUT_MAP_PENDING = "holdout_map_pending"
    BENCHMARK_PENDING = "benchmark_pending"
    COST_SCHEDULE_PENDING = "cost_schedule_pending"
    POWER_PENDING = "power_pending"
    EVALUATOR_PENDING = "evaluator_pending"
    CODE_COMMIT_PENDING = "code_commit_pending"
    TRADINGVIEW_PARITY_PENDING = "tradingview_parity_pending"
    SHORT_EVIDENCE_PENDING = "short_evidence_pending"


@dataclass(frozen=True, slots=True)
class ControlCell:
    cell_id: str
    role: str
    lookback_completed_sessions: int
    transition: str
    band_fraction: float
    confirmation_closes: int


@dataclass(frozen=True, slots=True)
class ControlBlocker:
    code: ControlBlockerCode
    detail: str


@dataclass(frozen=True, slots=True)
class CompiledQQQControl:
    preregistration_id: str
    preregistration_sha256: str
    status: ControlCompilationStatus
    cells: tuple[ControlCell, ...]
    blockers: tuple[ControlBlocker, ...]
    order_authority: str
    promotion_authority: str
    registered_trials: int

    @property
    def data_read_permitted(self) -> bool:
        return False

    @property
    def executable(self) -> bool:
        return False


def default_preregistration_path() -> Path:
    return Path(__file__).resolve().parents[3] / "specs/qqq_sma_control_v1.json"


def _mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise QQQControlSpecError(f"{context} must be an object")
    return cast(dict[str, object], value)


def _list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise QQQControlSpecError(f"{context} must be a list")
    return cast(list[object], value)


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise QQQControlSpecError(f"{context} must be a non-empty string")
    return value


def _integer(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise QQQControlSpecError(f"{context} must be an integer")
    return value


def _number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise QQQControlSpecError(f"{context} must be numeric")
    return float(value)


def _require_exact(value: object, expected: object, context: str) -> None:
    if value != expected:
        raise QQQControlSpecError(f"{context} must remain {expected!r}")


def _parse_cells(value: object) -> tuple[ControlCell, ...]:
    cells: list[ControlCell] = []
    for index, item in enumerate(_list(value, "cells")):
        raw = _mapping(item, f"cells[{index}]")
        cell = ControlCell(
            cell_id=_string(raw.get("cell_id"), f"cells[{index}].cell_id"),
            role=_string(raw.get("role"), f"cells[{index}].role"),
            lookback_completed_sessions=_integer(
                raw.get("lookback_completed_sessions"),
                f"cells[{index}].lookback_completed_sessions",
            ),
            transition=_string(raw.get("transition"), f"cells[{index}].transition"),
            band_fraction=_number(raw.get("band_fraction"), f"cells[{index}].band_fraction"),
            confirmation_closes=_integer(
                raw.get("confirmation_closes"), f"cells[{index}].confirmation_closes"
            ),
        )
        if cell.lookback_completed_sessions <= 0:
            raise QQQControlSpecError(f"{cell.cell_id}: lookback must be positive")
        if not 0.0 <= cell.band_fraction < 1.0:
            raise QQQControlSpecError(f"{cell.cell_id}: band fraction is invalid")
        if cell.confirmation_closes <= 0:
            raise QQQControlSpecError(f"{cell.cell_id}: confirmation must be positive")
        cells.append(cell)
    if len({cell.cell_id for cell in cells}) != len(cells):
        raise QQQControlSpecError("cell IDs must be unique")
    expected = (
        ControlCell("qqq-sma200-immediate-primary", "primary", 200, "immediate_two_state", 0, 1),
        ControlCell(
            "qqq-sma150-immediate-neighbor",
            "one_axis_parameter_neighbor",
            150,
            "immediate_two_state",
            0,
            1,
        ),
        ControlCell(
            "qqq-sma250-immediate-neighbor",
            "one_axis_parameter_neighbor",
            250,
            "immediate_two_state",
            0,
            1,
        ),
        ControlCell(
            "qqq-sma200-neutral-band-1pct",
            "prospective_robustness_variant",
            200,
            "three_state_neutral_band",
            0.01,
            1,
        ),
        ControlCell(
            "qqq-sma200-five-close-confirmation",
            "prospective_robustness_variant",
            200,
            "five_consecutive_strict_closes",
            0,
            5,
        ),
    )
    if tuple(cells) != expected:
        raise QQQControlSpecError("the five frozen control cells or their order changed")
    return tuple(cells)


def _validate_document(document: dict[str, object]) -> tuple[ControlCell, ...]:
    if frozenset(document) != _ROOT_KEYS:
        missing = sorted(_ROOT_KEYS - frozenset(document))
        extra = sorted(frozenset(document) - _ROOT_KEYS)
        raise QQQControlSpecError(f"root keys changed: missing={missing}, extra={extra}")
    _require_exact(document.get("schema_version"), SCHEMA_VERSION, "schema_version")
    _require_exact(document.get("preregistration_id"), PREREGISTRATION_ID, "preregistration_id")
    _require_exact(
        document.get("status"),
        ControlCompilationStatus.BLOCKED_BEFORE_FIRST_DATA_READ.value,
        "status",
    )

    constitution = _mapping(document.get("constitution"), "constitution")
    _require_exact(
        constitution.get("constitution_sha256"),
        EXPECTED_CONSTITUTION_SHA256,
        "constitution.constitution_sha256",
    )
    _require_exact(constitution.get("confluence_boundary_adr"), "ADR-0032", "confluence boundary")

    authority = _mapping(document.get("authority"), "authority")
    _require_exact(authority.get("order_authority"), "none", "authority.order_authority")
    _require_exact(authority.get("promotion_authority"), "none", "authority.promotion_authority")
    _require_exact(authority.get("selected_strategy"), None, "authority.selected_strategy")
    _require_exact(authority.get("registered_trials"), 0, "authority.registered_trials")
    _require_exact(
        authority.get("live_risk_authorized_usd"), 0, "authority.live_risk_authorized_usd"
    )
    _require_exact(authority.get("performance_claims"), [], "authority.performance_claims")

    scope = _mapping(document.get("scope"), "scope")
    _require_exact(scope.get("execution_target_symbol"), "QQQ", "scope symbol")
    _require_exact(scope.get("bar_interval"), "1D", "scope interval")
    _require_exact(scope.get("same_bar_action"), False, "same-bar action")

    signal = _mapping(document.get("signal"), "signal")
    _require_exact(signal.get("primary_lookback_completed_sessions"), 200, "SMA lookback")
    _require_exact(signal.get("window_includes_confirmed_session"), True, "SMA window")
    _require_exact(signal.get("primary_transition"), "immediate_two_state", "transition")
    initialization = _mapping(signal.get("initialization"), "signal.initialization")
    _require_exact(initialization.get("before_full_window"), "flat", "pre-window state")
    _require_exact(
        initialization.get("first_full_window_equal"),
        "remain_flat_until_first_strict_inequality",
        "equality initialization",
    )
    _require_exact(
        signal.get("equality_after_initialization"),
        "hold_prior_direction",
        "post-initialization equality",
    )
    _require_exact(signal.get("exit"), "first_confirmed_strict_signal_flip", "control exit")

    risk = _mapping(document.get("risk_and_sizing"), "risk_and_sizing")
    cvar = _mapping(risk.get("daily_cvar"), "risk_and_sizing.daily_cvar")
    _require_exact(cvar.get("confidence"), 0.95, "CVaR confidence")
    _require_exact(cvar.get("lookback_completed_returns"), 252, "CVaR lookback")
    _require_exact(cvar.get("tail_observation_count"), 13, "CVaR tail count")
    _require_exact(cvar.get("max_loss_fraction_of_applicable_base"), 0.015, "CVaR limit")
    _require_exact(cvar.get("max_loss_usd"), 45, "CVaR dollar limit")
    _require_exact(risk.get("gross_exposure_fraction_max"), 1.0, "gross limit")
    _require_exact(risk.get("leverage_max"), 1.0, "leverage limit")
    _require_exact(risk.get("in_position_upsize"), "forbidden", "in-position upsize")

    order = _mapping(document.get("entry_and_order_semantics"), "entry_and_order_semantics")
    _require_exact(order.get("one_attempt_per_event"), True, "entry attempt count")
    _require_exact(order.get("later_retry_or_chase"), False, "entry chase rule")
    _require_exact(order.get("order_form"), "protected_marketable_limit", "order form")
    _require_exact(order.get("market_protection_collar_fraction"), 0.01, "market collar")
    _require_exact(order.get("time_in_force"), "DAY", "time in force")
    checks = _list(order.get("pre_handoff_revalidation"), "pre_handoff_revalidation")
    if len(checks) != 8 or len(set(map(str, checks))) != len(checks):
        raise QQQControlSpecError("pre-handoff revalidation must retain eight unique checks")

    economics = _mapping(document.get("minimum_economic_trade"), "minimum_economic_trade")
    _require_exact(
        economics.get("maximum_projected_round_trip_cost_fraction_of_applicable_cvar_budget"),
        0.1,
        "economic-trade cost ceiling",
    )

    multiplicity = _mapping(document.get("multiplicity"), "multiplicity")
    _require_exact(multiplicity.get("planned_cells"), 5, "planned cell count")
    _require_exact(
        multiplicity.get("combinations_of_neighbor_axes"), "forbidden", "neighbor combinations"
    )
    _require_exact(
        multiplicity.get("evidence_transfer_to_five_tool_candidate"),
        "forbidden",
        "Five-Tool evidence transfer",
    )

    statistics = _mapping(document.get("statistics"), "statistics")
    _require_exact(statistics.get("power_required_N"), None, "power_required_N")
    locks = _mapping(document.get("identity_locks"), "identity_locks")
    if not locks or any(value is not None for value in locks.values()):
        raise QQQControlSpecError("v1 identity locks must remain explicitly unresolved")
    blockers = _list(
        document.get("blocked_before_first_data_read"), "blocked_before_first_data_read"
    )
    if len(blockers) != 8 or any(not isinstance(item, str) or not item for item in blockers):
        raise QQQControlSpecError("the eight pre-data blockers must remain explicit")
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
        raise QQQControlSpecError("forbidden capabilities changed")
    return _parse_cells(document.get("cells"))


def load_qqq_control_preregistration(path: Path | None = None) -> tuple[str, dict[str, object]]:
    """Load the exact preregistration bytes and return their digest and document."""

    target = path or default_preregistration_path()
    try:
        payload = target.read_bytes()
    except OSError as error:
        raise QQQControlSpecError(f"cannot read QQQ control preregistration: {error}") from error
    digest = hashlib.sha256(payload).hexdigest()
    if digest != EXPECTED_PREREGISTRATION_SHA256:
        raise QQQControlSpecError(
            f"QQQ control preregistration drifted: expected {EXPECTED_PREREGISTRATION_SHA256}, "
            f"observed {digest}"
        )
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QQQControlSpecError("QQQ control preregistration is not valid JSON") from error
    document = _mapping(decoded, "preregistration")
    _validate_document(document)
    return digest, document


def compile_qqq_control(path: Path | None = None) -> CompiledQQQControl:
    """Compile only typed blocked metadata; v1 grants no reader or execution plan."""

    digest, document = load_qqq_control_preregistration(path)
    authority = _mapping(document["authority"], "authority")
    cells = _parse_cells(document["cells"])
    blockers = (
        ControlBlocker(
            ControlBlockerCode.OWNER_APPROVAL_PENDING,
            "owner approval is recorded only when this exact identity merges",
        ),
        ControlBlocker(ControlBlockerCode.CERTIFIED_DATA_PENDING, "certified catalog is absent"),
        ControlBlocker(
            ControlBlockerCode.HOLDOUT_MAP_PENDING,
            "the content-addressed clean/seen/burned map is unresolved",
        ),
        ControlBlocker(ControlBlockerCode.BENCHMARK_PENDING, "cash-leg identity is unresolved"),
        ControlBlocker(
            ControlBlockerCode.COST_SCHEDULE_PENDING,
            "long cost identity and short borrow-cost identity are unresolved",
        ),
        ControlBlocker(ControlBlockerCode.POWER_PENDING, "power-required N is unresolved"),
        ControlBlocker(
            ControlBlockerCode.EVALUATOR_PENDING,
            "evaluator, criteria, registry, and campaign identities are unresolved",
        ),
        ControlBlocker(ControlBlockerCode.CODE_COMMIT_PENDING, "code commit is unresolved"),
        ControlBlocker(
            ControlBlockerCode.TRADINGVIEW_PARITY_PENDING,
            "TradingView parity is not satisfied or waived as a blocker",
        ),
        ControlBlocker(
            ControlBlockerCode.SHORT_EVIDENCE_PENDING,
            "short compiler, borrow, shortability, account, and owner evidence are absent",
        ),
    )
    return CompiledQQQControl(
        preregistration_id=PREREGISTRATION_ID,
        preregistration_sha256=digest,
        status=ControlCompilationStatus.BLOCKED_BEFORE_FIRST_DATA_READ,
        cells=cells,
        blockers=blockers,
        order_authority=_string(authority.get("order_authority"), "order_authority"),
        promotion_authority=_string(authority.get("promotion_authority"), "promotion_authority"),
        registered_trials=_integer(authority.get("registered_trials"), "registered_trials"),
    )


__all__ = [
    "EXPECTED_PREREGISTRATION_SHA256",
    "PREREGISTRATION_ID",
    "SCHEMA_VERSION",
    "CompiledQQQControl",
    "ControlBlocker",
    "ControlBlockerCode",
    "ControlCell",
    "ControlCompilationStatus",
    "QQQControlSpecError",
    "compile_qqq_control",
    "default_preregistration_path",
    "load_qqq_control_preregistration",
]
