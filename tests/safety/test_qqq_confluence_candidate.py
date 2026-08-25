from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from chronos.research.qqq_confluence import (
    EXPECTED_CANDIDATE_SHA256,
    CandidateBlockerCode,
    CandidateCompilationStatus,
    QQQConfluenceSpecError,
    compile_qqq_confluence_candidate,
    default_candidate_path,
    default_constitution_path,
    load_qqq_confluence_candidate,
)

_ROOT = Path(__file__).resolve().parents[2]
_MODULE = _ROOT / "src/chronos/research/qqq_confluence.py"


def test_exact_candidate_compiles_only_to_blocked_metadata() -> None:
    payload = default_candidate_path().read_bytes()
    compiled = compile_qqq_confluence_candidate()

    assert hashlib.sha256(payload).hexdigest() == EXPECTED_CANDIDATE_SHA256
    assert (
        compiled.constitution_sha256
        == hashlib.sha256(default_constitution_path().read_bytes()).hexdigest()
    )
    assert compiled.status is CandidateCompilationStatus.BLOCKED_BEFORE_FIRST_DATA_READ
    assert compiled.pine_sha256 == (
        "e51d5a40d2e933bf86847c7432364ba8934fd2de653d6aec3d7205639248e45f"
    )
    assert compiled.source_input_count == 219
    assert compiled.order_authority == "none"
    assert compiled.promotion_authority == "none"
    assert compiled.registered_trials == 0
    assert compiled.data_read_permitted is False
    assert compiled.executable is False
    assert {blocker.code for blocker in compiled.blockers} == set(CandidateBlockerCode)


def test_candidate_preserves_source_native_daily_defaults_and_asymmetry() -> None:
    _, document = load_qqq_confluence_candidate()
    base = document["base_five_tool_identity"]
    defaults = document["source_native_effective_defaults"]
    assert isinstance(base, dict)
    assert isinstance(defaults, dict)

    assert base["pine_overrides"] == {}
    assert defaults["preset"] == "Auto_resolves_to_Daily"
    assert (
        defaults["regime_lookback"],
        defaults["regime_enter_z"],
        defaults["regime_exit_z"],
        defaults["regime_confirmation_closes"],
    ) == (20, 0.85, 0.55, 2)
    assert defaults["ema_length"] == 100
    assert defaults["minimum_score"] == 55
    assert defaults["master_allow_shorts"] is False
    assert defaults["dedicated_short_v2"] is True
    assert defaults["short_plus"] is True
    assert defaults["dedicated_long_v2"] is False
    assert defaults["long_plus"] is False


def test_price_domains_and_protection_stack_are_explicit() -> None:
    _, document = load_qqq_confluence_candidate()
    domains = document["causal_price_domains"]
    position = document["position_plan"]
    risk = document["risk_and_sizing"]
    assert isinstance(domains, dict)
    assert isinstance(position, dict)
    assert isinstance(risk, dict)

    decision = domains["decision_ohlc"]
    assert isinstance(decision, dict)
    assert decision["domain"] == "point_in_time_total_return_OHLC_rebased_to_current_raw_close"
    assert domains["raw_execution"]["domain"] == "raw_tradable_prices"
    assert domains["domain_mixing"] == "forbidden"
    assert domains["corporate_action_between_signal_and_handoff"].startswith(
        "invalidate_and_consume"
    )

    assert position["target_1_r"] == 1.0
    assert position["target_2_r"] == 2.0
    assert position["break_even_after_target_1"] is True
    assert position["runner"] == {
        "enabled": True,
        "activation_r": 1.0,
        "chandelier_lookback": 22,
        "chandelier_atr_multiple": 3.0,
    }
    assert position["durable_paper_lifecycle"] == "not_implemented_by_this_candidate_spec"
    assert risk["applicable_capital_base_usd"] == "min(marked_strategy_nav_usd,3000)"
    assert risk["native_stop_risk_fraction"] == 0.01
    assert risk["outer_daily_cvar"]["long_loss_fraction"] == ("max(0,-one_session_total_return)")
    assert risk["outer_daily_cvar"]["estimator"] == (
        "arithmetic_mean_of_the_13_greatest_loss_fractions_in_the_252_completed_return_window"
    )
    assert risk["quantity_components"]["signal_time_cvar_quantity"] == (
        "cvar_loss_budget_usd/(signal_time_entry_basis_usd*"
        "direction_specific_unit_exposure_cvar_loss_fraction)"
    )
    assert risk["quantity_components"]["handoff_native_stop_distance_usd"] == (
        "max(signal_time_native_stop_distance_usd,next_session_protected_buy_limit-"
        "signal_time_active_initial_stop_usd)"
    )
    assert risk["handoff_permitted_quantity"].startswith("min(signal_time_permitted_quantity")
    assert risk["observed_daily_and_session_loss_halt_fraction"] == 0.02
    assert risk["in_position_upsize"] == "forbidden"


def test_any_overlay_byte_drift_refuses_before_interpretation(tmp_path: Path) -> None:
    document = json.loads(default_candidate_path().read_text())
    document["authority"]["order_authority"] = "paper"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(document, sort_keys=True))

    with pytest.raises(QQQConfluenceSpecError, match="candidate overlay drifted"):
        compile_qqq_confluence_candidate(changed)


def test_constitution_bytes_are_independently_authenticated(tmp_path: Path) -> None:
    constitution = json.loads(default_constitution_path().read_text())
    constitution["authority"]["live_risk_authorized_usd"] = 999_999
    changed = tmp_path / "constitution.json"
    changed.write_text(json.dumps(constitution, sort_keys=True))

    with pytest.raises(QQQConfluenceSpecError, match="constitution drifted"):
        compile_qqq_confluence_candidate(constitution_path=changed)


def test_candidate_loader_has_no_data_holdout_trial_or_execution_capability_import() -> None:
    tree = ast.parse(_MODULE.read_text(), filename=str(_MODULE))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    chronos_imports = {name for name in imports if name == "chronos" or name.startswith("chronos.")}
    assert chronos_imports == {"chronos.research.five_tool.contract"}
