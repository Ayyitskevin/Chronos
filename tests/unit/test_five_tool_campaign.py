"""Pure, fail-closed Five-Tool campaign compiler tests."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import chronos.research.five_tool as five_tool_package
from chronos.research.five_tool import compile_campaign_manifest as public_compile_campaign
from chronos.research.five_tool.campaign import (
    ABLATION_POLICY_SCHEMA_VERSION,
    CAMPAIGN_ID,
    CAMPAIGN_SCHEMA_VERSION,
    EVALUATOR_BINDING_SCHEMA_VERSION,
    EXECUTION_BINDINGS_SCHEMA_VERSION,
    EXECUTION_READY,
    RESEARCH_ABLATION_SCHEMA_VERSION,
    SUPERSEDES_CAMPAIGN_ID,
    CampaignBlockerCode,
    _compile_campaign_manifest_for_tests,
    compile_campaign_manifest,
)
from chronos.research.five_tool.contract import (
    input_contract_digest,
    load_contract,
    semantic_contract_digest,
)
from chronos.research.five_tool.models import FiveToolSettings
from chronos.research.five_tool.replay import FiveToolReplayPolicy

_ROOT = Path(__file__).resolve().parents[2]
_CAMPAIGN_MODULE = _ROOT / "src/chronos/research/five_tool/campaign.py"
_MANIFEST_PATH = _ROOT / "research/five_tool_v3_6_campaign_manifest.json"
_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64
_SHA_E = "e" * 64
_SHA_F = "f" * 64
_COMMIT = "1" * 40
_PINE_SHA = load_contract().pine.source_sha256
_INPUT_SHA = input_contract_digest()
_SEMANTIC_SHA = semantic_contract_digest()
_CELL_HYPOTHESES = {
    "5t-trend-directional-paired": "H-5T-001-TREND",
    "5t-momentum-score-paired": "H-5T-002-MOMENTUM",
    "5t-vol-scaling-paired": "H-5T-003-VOL-SCALING",
    "5t-rsi-divergence-paired": "H-5T-004-DIVERGENCE",
    "5t-mfi-divergence-paired": "H-5T-004-DIVERGENCE",
    "5t-relative-strength-paired": "H-5T-005-RELATIVE-STRENGTH",
    "5t-regime-filter-paired": "H-5T-006-REGIME-FILTER",
}
_HELD_FIXED = {
    "pine_inputs": "all_except_allowed_differences",
    "settings_fields": [
        "history_start_utc",
        "contract_digest",
        "exchange_timezone",
        "point_value",
        "minimum_tick",
    ],
    "replay_policy": True,
}


def _arm(arm_id: str, *, risk_pct: float | None = None) -> dict[str, Any]:
    overrides: dict[str, object] = {}
    if risk_pct is not None:
        overrides["risk_pct"] = risk_pct
    return {
        "arm_id": arm_id,
        "pine_overrides": overrides,
        "research_ablation": None,
    }


def _cell(
    *,
    cell_id: str = "5t-vol-scaling-paired",
    hypothesis_id: str = "H-5T-003-VOL-SCALING",
    treatment_id: str = "risk-150",
    control_id: str = "risk-100",
    treatment_risk: float = 1.5,
    control_risk: float = 1.0,
    neighbor_values: tuple[float, float, float] = (1.25, 1.5, 1.75),
) -> dict[str, Any]:
    return {
        "cell_id": cell_id,
        "hypothesis_id": hypothesis_id,
        "role": "paired Pine risk-input ablation",
        "ablation_policy": {
            "schema_version": ABLATION_POLICY_SCHEMA_VERSION,
            "status": "resolved",
            "treatment": _arm(treatment_id, risk_pct=treatment_risk),
            "control": _arm(control_id, risk_pct=control_risk),
            "comparison": {
                "kind": "paired_treatment_vs_control",
                "treatment_arm_id": treatment_id,
                "control_arm_id": control_id,
                "estimand": "net_expectancy_delta",
                "direction": "greater_than",
            },
            "allowed_differences": ["pine:risk_pct"],
            "held_fixed": copy.deepcopy(_HELD_FIXED),
            "neighbor_axes": [
                {
                    "base_arm_id": treatment_id,
                    "selector": "pine:risk_pct",
                    "values": list(neighbor_values),
                }
            ],
            "resolution_blockers": [],
        },
    }


def _ready_manifest() -> dict[str, Any]:
    replay = FiveToolReplayPolicy()
    return {
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "campaign_id": CAMPAIGN_ID,
        "supersedes_campaign_id": SUPERSEDES_CAMPAIGN_ID,
        "created_at_utc": "2026-08-08T00:00:00Z",
        "purpose": "Synthetic compiler contract fixture with no observed results.",
        "execution_state": EXECUTION_READY,
        "blocked_before_first_data_read": [],
        "performance_claims": [],
        "promotion_authority": "none",
        "code_commit_lock": {
            "git_commit": _COMMIT,
            "status": "resolved",
            "required_before_execution": True,
        },
        "strategy": {
            "strategy_id": "five_tool_confluence_v3_6",
            "version": "3.6",
            "scope": "research-only",
            "pine_source": {
                "path": "research/pine/00_five_tool_confluence_aio.pine",
                "sha256": _PINE_SHA,
            },
            "input_contract": {
                "path": "specs/five_tool_confluence_v3_6.yaml",
                "sha256": _INPUT_SHA,
                "digest_scope": "runtime executable-contract identity",
                "status": "resolved",
                "required_before_execution": True,
            },
            "semantic_config": {
                "path": "specs/five_tool_confluence_v3_6.yaml",
                "sha256": _SEMANTIC_SHA,
                "digest_scope": "source-bound timing/dependency/warmup/deviation identity",
                "status": "resolved",
                "required_before_execution": True,
            },
        },
        "data": {
            "primary_instruments": ["SPY", "QQQ", "IWM"],
            "benchmark": "SPY",
            "timeframe": "1D",
            "history_start_utc": "2010-01-04T00:00:00Z",
            "accessible_partitions": ["development"],
            "dataset_version_lock": {
                "dataset_id": "five-tool-certified-daily-v1",
                "sha256": _SHA_D,
                "status": "resolved",
                "required_before_execution": True,
            },
            "declared_holdouts": [
                {
                    "dataset_id": "five-tool-certified-holdout-v1",
                    "partition": "holdout",
                    "start_utc": "2026-09-01T00:00:00Z",
                    "status": "future_unopened",
                    "ordinary_research_access": "forbidden",
                }
            ],
            "known_contamination": ["Synthetic fixture is not certified evidence."],
        },
        "criteria_lock": {
            "path": "docs/FIVE_TOOL_RESEARCH_HYPOTHESES.md",
            "sha256": _SHA_E,
            "status": "resolved",
            "required_before_execution": True,
        },
        "replay_policy": {
            "canonical": replay.canonical_payload,
            "sha256": replay.digest,
        },
        "fill_policy": {
            "signal_clock": "confirmed primary bar close",
            "market_entry_eligibility": "next primary bar open",
            "higher_timeframe": "prior completed value only",
            "chart_ohlcv_approximation": "conservative stop first",
            "bar_magnifier": "complete identity-bound coverage required",
            "tradingview_fill_parity": "UNVERIFIED",
        },
        "costs": {
            "commission_bps_per_fill": 3.0,
            "slippage_ticks_per_fill": 2,
            "spread_policy": "bound by evaluator",
            "funding_borrow_model_data_costs": "bound by evaluator",
            "stress": {
                "commission_bps_per_fill": 6.0,
                "slippage_ticks_per_fill": 4,
                "require_positive_after_stress": True,
            },
        },
        "reference_arm": _arm("5t-full-default-reference"),
        "hypothesis_ids": list(dict.fromkeys(_CELL_HYPOTHESES.values())),
        "campaign_cells": [
            _cell(
                cell_id=cell_id,
                hypothesis_id=hypothesis_id,
                treatment_id=f"{cell_id}-treatment",
                control_id=f"{cell_id}-control",
                control_risk=round(0.5 + index * 0.5, 2),
                treatment_risk=round(0.7 + index * 0.5, 2),
                neighbor_values=(
                    round(0.6 + index * 0.5, 2),
                    round(0.7 + index * 0.5, 2),
                    round(0.8 + index * 0.5, 2),
                ),
            )
            for index, (cell_id, hypothesis_id) in enumerate(_CELL_HYPOTHESES.items())
        ],
        "statistics": {
            "sample_floor": "power-required N and at least 100 positions",
            "instruments_required": 3,
            "materially_different_regimes_required": 2,
            "expectancy_and_benchmark_alpha_95pct_lower_bound": "> 0 after costs",
            "deflated_sharpe_probability_min": 0.95,
            "fwer_or_fdr_q_max": 0.05,
            "probability_backtest_overfit_max": 0.1,
            "parameter_neighbor_pass_fraction_min": 0.67,
            "best_trade_removal": "net result remains positive",
            "best_month_removal": "net result remains positive",
            "drawdown_cvar_concentration_limits": "owner frozen",
            "two_phase_scoring": "evidence first, canonical multiplicity second",
        },
        "trial_accounting": {
            "record_kind_start": "trial_started",
            "start_must_precede_reader": True,
            "reader_and_evaluator_failures_count": True,
            "candidate_order_or_display_rename_changes_verdict": False,
            "multiplicity": "canonical registry starts",
        },
        "criteria_document": "docs/FIVE_TOOL_RESEARCH_HYPOTHESES.md",
        "identity_changes_that_invalidate_campaign": [
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
        ],
        "execution_bindings": {
            "schema_version": EXECUTION_BINDINGS_SCHEMA_VERSION,
            "status": "resolved",
            "catalog_manifest_sha256": _SHA_A,
            "partition_stage_map": {"development": "dev"},
            "requests": [
                {
                    "request_id": "certified-development",
                    "dataset_id": "five-tool-certified-daily-v1",
                    "partition": "development",
                    "data_version": _SHA_F,
                    "source_id": "certified-catalog",
                    "source_receipt_sha256": _SHA_B,
                }
            ],
            "evaluator": {
                "schema_version": EVALUATOR_BINDING_SCHEMA_VERSION,
                "evaluator_id": "five-tool-economic-evaluator-v1",
                "sha256": _SHA_C,
            },
            "resolution_blockers": [],
        },
    }


def _policy(manifest: dict[str, Any], index: int = 0) -> dict[str, Any]:
    cell = cast(dict[str, Any], manifest["campaign_cells"][index])
    return cast(dict[str, Any], cell["ablation_policy"])


def _codes(manifest: dict[str, Any]) -> set[CampaignBlockerCode]:
    return {blocker.code for blocker in compile_campaign_manifest(manifest).blockers}


def test_checked_manifest_is_structurally_compilable_but_remains_blocked() -> None:
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)

    report = compile_campaign_manifest(manifest)

    assert not report.ready
    assert report.plan is None
    codes = {item.code for item in report.blockers}
    assert CampaignBlockerCode.UNREPRESENTABLE_ABLATION in codes
    assert CampaignBlockerCode.CERTIFIED_CATALOG_PENDING in codes
    assert CampaignBlockerCode.MANIFEST_SCHEMA not in codes
    assert CampaignBlockerCode.ABLATION_POLICY_INVALID not in codes


def test_private_synthetic_pine_only_campaign_compiles_atomic_plan() -> None:
    manifest = _ready_manifest()

    report = _compile_campaign_manifest_for_tests(manifest)

    assert report.ready
    assert report.blockers == ()
    assert report.plan is not None
    assert report.plan.campaign_id == "five-tool-v3.6-preregistered-002"
    assert report.plan.reference_arm.research_ablation is None
    assert (
        report.plan.reference_arm.effective_settings_sha256
        == FiveToolSettings.defaults(
            history_start_utc=report.plan.reference_arm.effective_settings.history_start_utc
        ).digest
    )
    cell = next(item for item in report.plan.cells if item.cell_id == "5t-vol-scaling-paired")
    assert cell.allowed_differences == ("pine:risk_pct",)
    assert cell.treatment.research_ablation is None
    assert (
        cell.treatment.effective_settings_sha256
        == FiveToolSettings.defaults(
            history_start_utc=cell.treatment.effective_settings.history_start_utc,
            overrides={"risk_pct": 1.7},
        ).digest
    )
    assert hashlib.sha256(cell.treatment.config_bytes).hexdigest() == cell.treatment.config_sha256
    assert [neighbor.axis_value for neighbor in cell.neighbors] == [1.6, 1.8]
    assert len(report.plan.trials) == 28
    assert "5t-full-default-reference" not in {trial.arm_id for trial in report.plan.trials}
    assert {trial.stage for trial in report.plan.trials} == {"dev"}
    assert all(
        hashlib.sha256(trial.config_bytes).hexdigest() == trial.config_digest
        for trial in report.plan.trials
    )


def test_campaign_compiler_is_exposed_by_five_tool_package() -> None:
    report = public_compile_campaign(_ready_manifest())

    assert not report.ready
    assert report.plan is None
    assert CampaignBlockerCode.UNREPRESENTABLE_ABLATION in {
        blocker.code for blocker in report.blockers
    }
    assert "_compile_campaign_manifest_for_tests" not in five_tool_package.__all__


def test_public_v2_categorically_refuses_synthetic_resolved_cell_substitutions() -> None:
    report = compile_campaign_manifest(_ready_manifest())

    assert not report.ready
    assert report.plan is None
    semantic_blocks = [
        blocker
        for blocker in report.blockers
        if blocker.code is CampaignBlockerCode.UNREPRESENTABLE_ABLATION
        and blocker.location.endswith(".status")
    ]
    assert len(semantic_blocks) == 7


def test_compilation_and_all_bound_digests_are_deterministic() -> None:
    manifest = _ready_manifest()
    reordered = {key: copy.deepcopy(manifest[key]) for key in reversed(manifest)}

    left = _compile_campaign_manifest_for_tests(manifest)
    right = _compile_campaign_manifest_for_tests(reordered)

    assert left == right
    assert left.plan is not None
    assert len(left.plan.replay_policy_sha256) == 64
    assert len(left.plan.cells[0].ablation_policy_sha256) == 64
    assert len(left.plan.cells[0].execution_sha256) == 64
    assert len(left.plan.campaign_sha256) == 64


def test_semantic_campaign_digest_binds_code_criteria_and_replay_policy() -> None:
    baseline = _compile_campaign_manifest_for_tests(_ready_manifest())
    code_changed = _ready_manifest()
    code_changed["code_commit_lock"]["git_commit"] = "2" * 40
    criteria_changed = _ready_manifest()
    criteria_changed["criteria_lock"]["sha256"] = _SHA_A
    replay_changed = _ready_manifest()
    replay = FiveToolReplayPolicy(commission_bps_per_fill=4.0)
    replay_changed["replay_policy"] = {
        "canonical": replay.canonical_payload,
        "sha256": replay.digest,
    }
    replay_changed["costs"]["commission_bps_per_fill"] = 4.0

    reports = [
        baseline,
        _compile_campaign_manifest_for_tests(code_changed),
        _compile_campaign_manifest_for_tests(criteria_changed),
        _compile_campaign_manifest_for_tests(replay_changed),
    ]

    digests: set[str] = set()
    for report in reports:
        assert report.ready and report.plan is not None
        digests.add(report.plan.campaign_sha256)
    assert len(digests) == 4


def test_campaign_policy_digest_binds_accepted_execution_and_scoring_policy() -> None:
    baseline = _compile_campaign_manifest_for_tests(_ready_manifest())
    changed = _ready_manifest()
    changed["costs"]["stress"]["commission_bps_per_fill"] = 7.0

    revised = _compile_campaign_manifest_for_tests(changed)

    assert baseline.ready and revised.ready
    assert baseline.plan is not None and revised.plan is not None
    assert baseline.plan.campaign_policy_sha256 != revised.plan.campaign_policy_sha256
    assert baseline.plan.campaign_sha256 != revised.plan.campaign_sha256


def test_candidate_order_and_role_text_do_not_change_semantic_plan_identity() -> None:
    manifest = _ready_manifest()
    manifest["data"]["accessible_partitions"].append("validation")
    manifest["execution_bindings"]["partition_stage_map"]["validation"] = "validation"
    manifest["execution_bindings"]["requests"].append(
        {
            "request_id": "certified-validation",
            "dataset_id": "five-tool-certified-daily-v1",
            "partition": "validation",
            "data_version": _SHA_E,
            "source_id": "certified-catalog-second",
            "source_receipt_sha256": _SHA_C,
        }
    )
    reordered = copy.deepcopy(manifest)
    reordered["campaign_cells"].reverse()
    reordered["execution_bindings"]["requests"].reverse()
    reordered["campaign_cells"][0]["role"] = "display-only wording changed"

    left = _compile_campaign_manifest_for_tests(manifest)
    right = _compile_campaign_manifest_for_tests(reordered)

    assert left.ready and right.ready
    assert left.plan is not None and right.plan is not None
    assert left.manifest_sha256 != right.manifest_sha256
    assert left.plan.campaign_sha256 == right.plan.campaign_sha256
    assert left.plan.execution_bindings.sha256 == right.plan.execution_bindings.sha256
    assert {item.campaign_sha256 for item in left.plan.trials} == {left.plan.campaign_sha256}
    assert {item.campaign_sha256 for item in right.plan.trials} == {right.plan.campaign_sha256}
    assert {item.campaign_manifest_sha256 for item in left.plan.trials} != {
        item.campaign_manifest_sha256 for item in right.plan.trials
    }
    left_trials = [
        (item.cell_id, item.arm_id, item.request.request_id, item.config_digest)
        for item in left.plan.trials
    ]
    right_trials = [
        (item.cell_id, item.arm_id, item.request.request_id, item.config_digest)
        for item in right.plan.trials
    ]
    assert left_trials == right_trials


def test_equivalent_integer_and_float_axis_literals_have_one_semantic_identity() -> None:
    integer_literal = _ready_manifest()
    _policy(integer_literal)["treatment"]["pine_overrides"]["risk_pct"] = 4
    _policy(integer_literal)["neighbor_axes"][0]["values"] = [3.9, 4, 4.1]
    float_literal = copy.deepcopy(integer_literal)
    _policy(float_literal)["treatment"]["pine_overrides"]["risk_pct"] = 4.0
    _policy(float_literal)["neighbor_axes"][0]["values"] = [3.9, 4.0, 4.1]

    left = _compile_campaign_manifest_for_tests(integer_literal)
    right = _compile_campaign_manifest_for_tests(float_literal)

    assert left.ready and right.ready
    assert left.plan is not None and right.plan is not None
    assert left.manifest_sha256 != right.manifest_sha256
    assert left.plan.campaign_sha256 == right.plan.campaign_sha256
    left_cell = next(
        item for item in left.plan.cells if item.cell_id == "5t-trend-directional-paired"
    )
    assert [item.axis_value for item in left_cell.neighbors] == [3.9, 4.1]
    assert all(isinstance(item.axis_value, float) for item in left_cell.neighbors)


def test_any_blocker_suppresses_the_entire_executable_plan() -> None:
    manifest = _ready_manifest()
    manifest["execution_state"] = "blocked_until_identity_locks_resolve"
    manifest["blocked_before_first_data_read"] = ["owner review is still open"]

    report = _compile_campaign_manifest_for_tests(manifest)

    assert not report.ready
    assert report.plan is None
    assert CampaignBlockerCode.DECLARED_BLOCKER in {item.code for item in report.blockers}


def test_v2_root_schema_and_research_only_authority_are_enforced_directly() -> None:
    unknown = _ready_manifest()
    unknown["unreviewed_extension"] = True
    wrong_predecessor = _ready_manifest()
    wrong_predecessor["supersedes_campaign_id"] = "five-tool-v3.6-preregistered-000"
    claims = _ready_manifest()
    claims["performance_claims"] = ["looks profitable"]

    assert CampaignBlockerCode.MANIFEST_SCHEMA in _codes(unknown)
    assert CampaignBlockerCode.MANIFEST_SCHEMA in _codes(wrong_predecessor)
    assert CampaignBlockerCode.MANIFEST_SCHEMA in _codes(claims)


def test_incomplete_or_incoherent_nested_execution_metadata_cannot_be_ready() -> None:
    missing_data = _ready_manifest()
    del missing_data["data"]["primary_instruments"]
    incomplete_fill = _ready_manifest()
    del incomplete_fill["fill_policy"]["bar_magnifier"]
    mismatched_cost = _ready_manifest()
    mismatched_cost["costs"]["commission_bps_per_fill"] = 9.0
    incomplete_statistics = _ready_manifest()
    del incomplete_statistics["statistics"]["sample_floor"]
    accessible_holdout = _ready_manifest()
    accessible_holdout["data"]["accessible_partitions"] = ["final-holdout"]

    for manifest in (
        missing_data,
        incomplete_fill,
        mismatched_cost,
        incomplete_statistics,
        accessible_holdout,
    ):
        report = compile_campaign_manifest(manifest)
        assert report.plan is None
        assert CampaignBlockerCode.MANIFEST_SCHEMA in {item.code for item in report.blockers}


def test_declared_strategy_identities_must_match_source_bound_configuration() -> None:
    manifest = _ready_manifest()
    manifest["strategy"]["input_contract"]["sha256"] = _SHA_A

    report = compile_campaign_manifest(manifest)

    assert report.plan is None
    assert CampaignBlockerCode.IDENTITY_UNRESOLVED in _codes(manifest)


def test_manifest_benchmark_must_match_source_bound_pine_benchmark_ticker() -> None:
    manifest = _ready_manifest()
    manifest["data"]["benchmark"] = "QQQ"

    report = compile_campaign_manifest(manifest)

    assert report.plan is None
    assert CampaignBlockerCode.IDENTITY_UNRESOLVED in _codes(manifest)


def test_pending_ablation_and_execution_bindings_report_typed_blockers() -> None:
    manifest = _ready_manifest()
    policy = _policy(manifest)
    policy.update(
        {
            "status": "pending_resolution",
            "treatment": None,
            "control": None,
            "comparison": None,
            "allowed_differences": [],
            "held_fixed": None,
            "neighbor_axes": [],
            "resolution_blockers": [
                {
                    "code": "unrepresentable_ablation",
                    "message": "component semantics need a reviewed representation",
                },
                {
                    "code": "missing_neighbor_axis",
                    "message": "neighbor axis is not preregistered",
                },
            ],
        }
    )
    bindings = cast(dict[str, Any], manifest["execution_bindings"])
    bindings.update(
        {
            "status": "pending_resolution",
            "catalog_manifest_sha256": None,
            "partition_stage_map": None,
            "requests": None,
            "evaluator": None,
            "resolution_blockers": [
                {
                    "code": "certified_catalog_pending",
                    "message": "certified catalog is not locked",
                },
                {
                    "code": "evaluator_pending",
                    "message": "evaluator is not locked",
                },
            ],
        }
    )

    report = compile_campaign_manifest(manifest)

    assert report.plan is None
    assert CampaignBlockerCode.UNREPRESENTABLE_ABLATION in _codes(manifest)
    assert CampaignBlockerCode.MISSING_NEIGHBOR_AXIS in _codes(manifest)
    assert CampaignBlockerCode.CERTIFIED_CATALOG_PENDING in _codes(manifest)
    assert CampaignBlockerCode.EVALUATOR_PENDING in _codes(manifest)


def test_pending_resolution_blockers_reject_free_text_unknown_codes_and_extra_keys() -> None:
    free_text = _ready_manifest()
    policy = _policy(free_text)
    policy.update(
        {
            "status": "pending_resolution",
            "treatment": None,
            "control": None,
            "comparison": None,
            "allowed_differences": [],
            "held_fixed": None,
            "neighbor_axes": [],
            "resolution_blockers": ["UNRESOLVED_CELL_CONFIG: still prose"],
        }
    )
    unknown = copy.deepcopy(free_text)
    _policy(unknown)["resolution_blockers"] = [
        {"code": "invented_clearance", "message": "not reviewed"}
    ]
    extra = copy.deepcopy(free_text)
    _policy(extra)["resolution_blockers"] = [
        {
            "code": "unresolved_cell_config",
            "message": "not reviewed",
            "waive": True,
        }
    ]

    assert CampaignBlockerCode.ABLATION_POLICY_INVALID in _codes(free_text)
    assert CampaignBlockerCode.ABLATION_POLICY_INVALID in _codes(unknown)
    assert CampaignBlockerCode.ABLATION_POLICY_INVALID in _codes(extra)


def test_pending_cell_requires_the_exact_frozen_semantic_blocker_set() -> None:
    manifest = _ready_manifest()
    policy = _policy(manifest)
    policy.update(
        {
            "status": "pending_resolution",
            "treatment": None,
            "control": None,
            "comparison": None,
            "allowed_differences": [],
            "held_fixed": None,
            "neighbor_axes": [],
            "resolution_blockers": [
                {
                    "code": "unrepresentable_ablation",
                    "message": "reviewed component semantics are unavailable",
                }
            ],
        }
    )

    assert CampaignBlockerCode.ABLATION_POLICY_INVALID in _codes(manifest)


def test_unknown_or_wrongly_typed_pine_overrides_fail_closed() -> None:
    unknown = _ready_manifest()
    _policy(unknown)["treatment"]["pine_overrides"] = {"invented_component": True}
    wrong_type = _ready_manifest()
    _policy(wrong_type)["treatment"]["pine_overrides"] = {"risk_pct": True}

    assert CampaignBlockerCode.PINE_OVERRIDE_INVALID in _codes(unknown)
    assert CampaignBlockerCode.PINE_OVERRIDE_INVALID in _codes(wrong_type)
    assert compile_campaign_manifest(unknown).plan is None
    assert compile_campaign_manifest(wrong_type).plan is None


def test_complete_effective_setting_diff_must_exactly_match_selectors() -> None:
    undeclared = _ready_manifest()
    _policy(undeclared)["treatment"]["pine_overrides"]["max_best_trade_dep_pct"] = 30.0
    overdeclared = _ready_manifest()
    _policy(overdeclared)["allowed_differences"] = [
        "pine:max_best_trade_dep_pct",
        "pine:risk_pct",
    ]

    assert CampaignBlockerCode.DIFFERENCE_POLICY_INVALID in _codes(undeclared)
    assert CampaignBlockerCode.DIFFERENCE_POLICY_INVALID in _codes(overdeclared)


def test_any_opaque_research_ablation_lock_is_unrepresentable() -> None:
    manifest = _ready_manifest()
    lock = {
        "schema_version": RESEARCH_ABLATION_SCHEMA_VERSION,
        "policy_id": "opaque-stream-filter",
        "sha256": _SHA_E,
    }
    _policy(manifest)["treatment"]["research_ablation"] = copy.deepcopy(lock)
    _policy(manifest)["control"]["research_ablation"] = copy.deepcopy(lock)

    report = _compile_campaign_manifest_for_tests(manifest)

    assert report.plan is None
    blockers = [
        item
        for item in report.blockers
        if item.code is CampaignBlockerCode.UNREPRESENTABLE_ABLATION
    ]
    assert len(blockers) == 2


def test_reference_opaque_research_lock_is_also_unrepresentable() -> None:
    manifest = _ready_manifest()
    manifest["reference_arm"]["research_ablation"] = {
        "schema_version": RESEARCH_ABLATION_SCHEMA_VERSION,
        "policy_id": "reference-stream-policy",
        "sha256": _SHA_E,
    }

    report = compile_campaign_manifest(manifest)

    assert report.plan is None
    assert CampaignBlockerCode.UNREPRESENTABLE_ABLATION in _codes(manifest)


def test_neighbor_axes_must_be_derived_monotone_single_pine_dimensions() -> None:
    nonmonotone = _ready_manifest()
    _policy(nonmonotone)["neighbor_axes"][0]["values"] = [1.25, 1.5, 1.4]
    arbitrary = _ready_manifest()
    _policy(arbitrary)["neighbor_axes"][0]["selector"] = "research_ablation"

    assert CampaignBlockerCode.NEIGHBOR_AXIS_INVALID in _codes(nonmonotone)
    assert CampaignBlockerCode.NEIGHBOR_AXIS_INVALID in _codes(arbitrary)


def test_resolved_cell_cannot_omit_preregistered_neighbor_axes() -> None:
    manifest = _ready_manifest()
    _policy(manifest)["neighbor_axes"] = []

    report = _compile_campaign_manifest_for_tests(manifest)

    assert report.plan is None
    assert CampaignBlockerCode.MISSING_NEIGHBOR_AXIS in {
        blocker.code for blocker in report.blockers
    }


def test_neighbor_axis_cannot_duplicate_the_other_comparison_arm_config() -> None:
    manifest = _ready_manifest()
    _policy(manifest)["neighbor_axes"][0]["values"] = [0.5, 0.7, 0.9]

    report = compile_campaign_manifest(manifest)

    assert report.plan is None
    assert CampaignBlockerCode.NEIGHBOR_AXIS_INVALID in _codes(manifest)


def test_effective_execution_configs_must_be_unique_across_campaign_cells() -> None:
    manifest = _ready_manifest()
    manifest["campaign_cells"][1]["ablation_policy"] = copy.deepcopy(
        manifest["campaign_cells"][0]["ablation_policy"]
    )

    report = _compile_campaign_manifest_for_tests(manifest)

    assert report.plan is None
    assert CampaignBlockerCode.DUPLICATE_EXECUTION_KEY in {
        blocker.code for blocker in report.blockers
    }


def test_execution_bindings_must_exactly_match_campaign_dataset_and_partitions() -> None:
    wrong_dataset = _ready_manifest()
    wrong_dataset["execution_bindings"]["requests"][0]["dataset_id"] = "other-dataset"
    extra_stage = _ready_manifest()
    extra_stage["execution_bindings"]["partition_stage_map"]["validation"] = "validation"

    assert CampaignBlockerCode.EXECUTION_BINDING_INVALID in _codes(wrong_dataset)
    assert CampaignBlockerCode.EXECUTION_BINDING_INVALID in _codes(extra_stage)


def test_partition_content_sha_is_distinct_from_release_and_unique_across_partitions() -> None:
    manifest = _ready_manifest()
    assert (
        manifest["data"]["dataset_version_lock"]["sha256"]
        != manifest["execution_bindings"]["requests"][0]["data_version"]
    )
    manifest["data"]["accessible_partitions"].append("validation")
    manifest["execution_bindings"]["partition_stage_map"]["validation"] = "validation"
    duplicate_content = copy.deepcopy(manifest["execution_bindings"]["requests"][0])
    duplicate_content["request_id"] = "validation-content-alias"
    duplicate_content["partition"] = "validation"
    duplicate_content["source_receipt_sha256"] = _SHA_C
    manifest["execution_bindings"]["requests"].append(duplicate_content)

    report = _compile_campaign_manifest_for_tests(manifest)

    assert report.plan is None
    assert CampaignBlockerCode.EXECUTION_BINDING_INVALID in {
        blocker.code for blocker in report.blockers
    }


def test_catalog_key_cannot_be_duplicated_with_new_request_or_source_labels() -> None:
    manifest = _ready_manifest()
    duplicate = copy.deepcopy(manifest["execution_bindings"]["requests"][0])
    duplicate["request_id"] = "same-data-new-label"
    duplicate["source_id"] = "alternate-source-label"
    duplicate["source_receipt_sha256"] = _SHA_C
    manifest["execution_bindings"]["requests"].append(duplicate)

    report = compile_campaign_manifest(manifest)

    assert report.plan is None
    assert CampaignBlockerCode.EXECUTION_BINDING_INVALID in _codes(manifest)


def test_declared_holdout_partition_alias_cannot_be_accessible_or_requested() -> None:
    manifest = _ready_manifest()
    manifest["data"]["declared_holdouts"][0]["partition"] = "Secret-Final"
    manifest["data"]["accessible_partitions"] = ["secret_final"]
    manifest["execution_bindings"]["partition_stage_map"] = {"secret_final": "dev"}
    manifest["execution_bindings"]["requests"][0]["partition"] = "secret_final"

    report = _compile_campaign_manifest_for_tests(manifest)

    assert report.plan is None
    assert CampaignBlockerCode.MANIFEST_SCHEMA in {blocker.code for blocker in report.blockers}


def test_execution_source_id_uses_certified_request_identity_syntax() -> None:
    manifest = _ready_manifest()
    manifest["execution_bindings"]["requests"][0]["source_id"] = "invalid source!"

    report = _compile_campaign_manifest_for_tests(manifest)

    assert report.plan is None
    assert CampaignBlockerCode.EXECUTION_BINDING_INVALID in {
        blocker.code for blocker in report.blockers
    }


def test_signed_zero_is_refused_at_every_manifest_numeric_boundary() -> None:
    pine_override = _ready_manifest()
    _policy(pine_override)["treatment"]["pine_overrides"]["risk_pct"] = -0.0
    neighbor_axis = _ready_manifest()
    _policy(neighbor_axis)["neighbor_axes"][0]["values"][0] = -0.0
    replay_cost = _ready_manifest()
    replay_cost["replay_policy"]["canonical"]["commission_bps_per_fill"] = -0.0
    scoring_threshold = _ready_manifest()
    scoring_threshold["statistics"]["fwer_or_fdr_q_max"] = -0.0

    for manifest in (pine_override, neighbor_axis, replay_cost, scoring_threshold):
        report = _compile_campaign_manifest_for_tests(manifest)
        assert report.manifest_sha256 is None
        assert report.plan is None
        assert {blocker.code for blocker in report.blockers} == {
            CampaignBlockerCode.MANIFEST_SCHEMA
        }


def test_ordinary_campaign_cannot_compile_a_holdout_stage() -> None:
    manifest = _ready_manifest()
    manifest["data"]["accessible_partitions"] = ["future_holdout"]
    bindings = manifest["execution_bindings"]
    bindings["partition_stage_map"] = {"future_holdout": "holdout"}
    bindings["requests"][0]["partition"] = "future_holdout"

    report = compile_campaign_manifest(manifest)

    assert report.plan is None
    assert CampaignBlockerCode.EXECUTION_BINDING_INVALID in _codes(manifest)


def test_replay_payload_and_digest_must_exactly_match_adapter_policy() -> None:
    manifest = _ready_manifest()
    manifest["replay_policy"]["sha256"] = _SHA_E

    report = compile_campaign_manifest(manifest)

    assert report.plan is None
    assert CampaignBlockerCode.REPLAY_POLICY_INVALID in _codes(manifest)


def test_multiple_paired_cells_may_cover_one_hypothesis() -> None:
    report = _compile_campaign_manifest_for_tests(_ready_manifest())

    assert report.ready
    assert report.plan is not None
    h4_cells = {
        cell.cell_id for cell in report.plan.cells if cell.hypothesis_id == "H-5T-004-DIVERGENCE"
    }
    assert h4_cells == {"5t-rsi-divergence-paired", "5t-mfi-divergence-paired"}


def test_invalid_manifest_shape_returns_blocker_instead_of_raising() -> None:
    manifest = _ready_manifest()
    manifest["campaign_cells"] = "not-an-array"

    report = compile_campaign_manifest(manifest)

    assert report.plan is None
    assert CampaignBlockerCode.MANIFEST_SCHEMA in _codes(manifest)


def test_campaign_compiler_has_no_capability_imports() -> None:
    source = _CAMPAIGN_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    forbidden = (
        "chronos.registry",
        "chronos.research.certified_data",
        "chronos.research.replay_store",
        "chronos.research.trial_runner",
        "chronos.research.five_tool_trials",
        "chronos.research.holdout",
        "chronos.execution",
        "chronos.orders",
        "chronos.promotion",
        "os",
        "pathlib",
        "random",
        "socket",
        "subprocess",
        "time",
        "urllib",
        "httpx",
        "requests",
    )
    assert not any(name.startswith(forbidden) for name in imported)
    allowed_chronos_imports = {
        "chronos.research.five_tool.contract",
        "chronos.research.five_tool.models",
        "chronos.research.five_tool.planning",
        "chronos.research.five_tool.replay",
    }
    assert {name for name in imported if name.startswith("chronos.")} == allowed_chronos_imports
    forbidden_calls = {"now", "utcnow", "today", "getenv", "open", "write_text", "write_bytes"}
    assert not any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id in forbidden_calls)
            or (isinstance(node.func, ast.Attribute) and node.func.attr in forbidden_calls)
        )
        for node in ast.walk(tree)
    )
    assert "deterministic reads of the frozen" in source
    assert "those configuration reads are the sole I/O boundary" in source
