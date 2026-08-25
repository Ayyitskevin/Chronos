from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_CONSTITUTION = _ROOT / "research" / "qqq_v1_constitution.json"
_EXPECTED_SHA256 = "4c99ce9d09f43a418c7342b0e40a0795b253bf3f1cd0e37d29419498b3008d56"


def _load() -> tuple[bytes, dict[str, Any]]:
    payload = _CONSTITUTION.read_bytes()
    return payload, json.loads(payload)


def test_qqq_constitution_identity_is_pinned() -> None:
    payload, document = _load()

    assert document["schema_version"] == "chronos-qqq-constitution-v1"
    assert hashlib.sha256(payload).hexdigest() == _EXPECTED_SHA256


def test_qqq_constitution_grants_no_authority_or_evidence() -> None:
    _, document = _load()
    authority = document["authority"]
    research = document["research"]

    assert authority == {
        "live_risk_authorized_usd": 0,
        "order_authority": "none",
        "promotion_authority": "none",
        "submitting_mandate": "forbidden",
        "performance_claims": [],
        "selected_strategy": None,
        "registered_trials": 0,
    }
    assert research["owner_holdout"] is None
    assert document["status"] == "blocked_before_first_data_read"


def test_qqq_scope_does_not_smuggle_short_or_validation_authority() -> None:
    _, document = _load()
    scope = document["scope"]
    matrix = {entry["direction"]: entry for entry in document["capability_matrix"]}

    assert scope["execution_target_symbols"] == ["QQQ"]
    assert scope["robustness_panel_authority"] == "validation_only_never_execution"
    assert set(scope["robustness_panel_symbols"]) == {"QQQ", "SPY", "IWM", "DIA", "GLD", "TLT"}
    assert matrix["SHORT"]["live"] == "not_authorized"
    assert matrix["SHORT"]["supervised_paper"].startswith("blocked_no_compiler")


def test_qqq_owner_limits_are_exact_and_funding_is_not_authority() -> None:
    _, document = _load()
    capital = document["capital"]
    risk = document["risk"]
    economics = document["economics"]

    assert capital["research_reference_capital_usd"] == 3000
    assert capital["current_live_allocation_usd"] == 0
    assert capital["funding_is_authority"] is False
    assert capital["funding_evidence_gate"] == {
        "untouched_holdout": "pass_unchanged",
        "shadow_calendar_days_min": 90,
        "supervised_paper": "required",
        "performance_claim": "none_until_full_vision_ladder_passes",
    }
    assert risk["max_gross_exposure_fraction"] == 1.0
    assert risk["max_leverage"] == 1.0
    assert risk["max_peak_to_trough_drawdown_fraction"] == 0.1
    assert risk["max_daily_loss_usd_at_reference_capital"] == 60
    assert risk["max_daily_cvar_loss_usd_at_reference_capital"] == 45
    assert economics["annualized_post_cost_alpha_point_estimate_min_fraction"] == 0.04
    assert economics["benchmark_alpha_95pct_lower_bound_min_exclusive"] == 0.0
    assert economics["recurring_data_and_software_budget_usd_per_month"] == 0
