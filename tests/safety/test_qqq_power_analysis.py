from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from chronos.research import qqq_power_analysis
from chronos.research.qqq_power_analysis import (
    ANALYSIS_ID,
    EXPECTED_ANALYSIS_SHA256,
    PowerAnalysisStatus,
    PowerBlockerCode,
    QQQPowerAnalysisError,
    compile_qqq_power_analysis,
    default_power_analysis_path,
    required_sample_size,
)

_ROOT = Path(__file__).resolve().parents[2]
_MODULE = _ROOT / "src/chronos/research/qqq_power_analysis.py"


def test_exact_identity_freezes_relative_power_and_no_authority() -> None:
    payload = default_power_analysis_path().read_bytes()
    compiled = compile_qqq_power_analysis()

    assert hashlib.sha256(payload).hexdigest() == EXPECTED_ANALYSIS_SHA256
    assert compiled.analysis_id == ANALYSIS_ID
    assert compiled.status is PowerAnalysisStatus.BLOCKED_PENDING_CLEAN_WINDOW_IDENTITY
    assert compiled.minimum_detectable_annualized_alpha_fraction == 0.04
    assert compiled.annualized_long_run_tracking_error_ceiling_fraction == 0.08
    assert compiled.confidence_lower_bound == 0.95
    assert compiled.target_power == 0.8
    assert compiled.annualization_sessions == 252
    assert compiled.power_required_n == 6233
    assert compiled.power_required_n_unit == "completed_OOS_daily_session_returns"
    assert compiled.required_year_equivalents == pytest.approx(24.730228928079057)
    assert compiled.order_authority == "none"
    assert compiled.promotion_authority == "none"
    assert compiled.registered_trials == 0
    assert compiled.data_read_permitted is False
    assert compiled.trial_registration_permitted is False
    assert compiled.holdout_unlock_permitted is False
    assert compiled.executable is False


def test_positions_are_a_separate_gate_and_absolute_date_remains_blocked() -> None:
    compiled = compile_qqq_power_analysis()

    assert compiled.minimum_oos_closed_positions == 100
    assert compiled.earliest_pass_offset_completed_sessions_from_clean_start_inclusive == 6232
    assert compiled.earliest_possible_pass_date is None
    assert compiled.absolute_pass_date_resolved is False
    assert [blocker.code for blocker in compiled.blockers] == list(PowerBlockerCode)


@pytest.mark.parametrize(
    (
        "effect",
        "tracking_error",
        "alpha",
        "power",
        "annualization_sessions",
        "expected_n",
    ),
    [
        (0.04, 0.04, 0.05, 0.80, 252, 1559),
        (0.04, 0.06, 0.05, 0.80, 252, 3506),
        (0.04, 0.08, 0.05, 0.80, 252, 6233),
        (0.04, 0.10, 0.05, 0.80, 252, 9738),
        (0.04, 0.12, 0.05, 0.80, 252, 14023),
        (0.04, 0.15, 0.05, 0.80, 252, 21910),
        (0.04, 0.20, 0.05, 0.80, 252, 38951),
        (0.02, 0.08, 0.05, 0.80, 252, 24929),
        (0.03, 0.08, 0.05, 0.80, 252, 11080),
        (0.06, 0.08, 0.05, 0.80, 252, 2770),
        (0.08, 0.08, 0.05, 0.80, 252, 1559),
        (0.04, 0.08, 0.05, 0.70, 252, 4744),
        (0.04, 0.08, 0.05, 0.90, 252, 8633),
        (0.04, 0.08, 0.05, 0.95, 252, 10909),
        (0.04, 0.08, 0.10, 0.80, 252, 4544),
        (0.04, 0.08, 0.025, 0.80, 252, 7912),
        (0.04, 0.08, 0.01, 0.80, 252, 10117),
        (0.04, 0.08, 0.005, 0.80, 252, 11773),
        (0.04, 0.08, 0.05, 0.80, 52, 1286),
        (0.04, 0.08, 0.05, 0.80, 12, 297),
    ],
)
def test_twenty_preregistered_sensitivity_cases_recompute_exactly(
    effect: float,
    tracking_error: float,
    alpha: float,
    power: float,
    annualization_sessions: int,
    expected_n: int,
) -> None:
    result = required_sample_size(
        minimum_detectable_annualized_alpha_fraction=effect,
        annualized_long_run_tracking_error_fraction=tracking_error,
        type_i_error_alpha=alpha,
        target_power=power,
        annualization_sessions=annualization_sessions,
    )

    assert result.required_observations == expected_n


@pytest.mark.parametrize(
    "overrides",
    [
        {"minimum_detectable_annualized_alpha_fraction": 0.0},
        {"annualized_long_run_tracking_error_fraction": 0.0},
        {"type_i_error_alpha": 0.0},
        {"type_i_error_alpha": 0.5},
        {"type_i_error_alpha": 1.0},
        {"target_power": 0.0},
        {"target_power": 0.5},
        {"target_power": 1.0},
        {"annualization_sessions": 0},
        {"annualization_sessions": 252.5},
        {"annualization_sessions": True},
        {"minimum_detectable_annualized_alpha_fraction": float("nan")},
    ],
)
def test_invalid_power_inputs_refuse(overrides: dict[str, float | int]) -> None:
    inputs: dict[str, float | int] = {
        "minimum_detectable_annualized_alpha_fraction": 0.04,
        "annualized_long_run_tracking_error_fraction": 0.08,
        "type_i_error_alpha": 0.05,
        "target_power": 0.8,
        "annualization_sessions": 252,
    }
    inputs.update(overrides)

    with pytest.raises(ValueError):
        required_sample_size(**inputs)  # type: ignore[arg-type]


def test_any_analysis_byte_drift_refuses_before_interpretation(tmp_path: Path) -> None:
    document = json.loads(default_power_analysis_path().read_text())
    document["authority"]["order_authority"] = "paper"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(document, sort_keys=True))

    with pytest.raises(QQQPowerAnalysisError, match="power analysis drifted"):
        compile_qqq_power_analysis(changed)


@pytest.mark.parametrize(
    "relative_path",
    [
        "research/qqq_v1_constitution.json",
        "specs/qqq_sma_control_v1.json",
        "specs/qqq_five_tool_candidate_v1.json",
    ],
)
def test_each_bound_artifact_is_authenticated(
    relative_path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = compile_qqq_power_analysis()
    for artifact in compiled.bound_artifacts:
        source = _ROOT / artifact.path
        target = tmp_path / artifact.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    changed = tmp_path / relative_path
    changed.write_bytes(changed.read_bytes() + b"\n")
    monkeypatch.setattr(qqq_power_analysis, "_repo_root", lambda: tmp_path)

    with pytest.raises(QQQPowerAnalysisError, match="drifted"):
        compile_qqq_power_analysis()


def test_power_module_imports_no_chronos_or_authority_dependency() -> None:
    tree = ast.parse(_MODULE.read_text(), filename=str(_MODULE))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    assert not any(name == "chronos" or name.startswith("chronos.") for name in imports)

    forbidden = (
        "chronos.api",
        "chronos.autonomy",
        "chronos.broker",
        "chronos.control",
        "chronos.execution",
        "chronos.histdata",
        "chronos.orders",
        "chronos.persistence",
        "chronos.registry",
        "chronos.risk",
        "chronos.service",
        "chronos.services",
        "chronos.strategy",
        "chronos.strategies",
        "chronos.supervisor",
        "fastapi",
        "httpx",
        "ib_async",
        "ibapi",
        "sqlalchemy",
        "sqlite3",
    )
    probe = (
        "import chronos.research.qqq_power_analysis, sys; "
        f"blocked={forbidden!r}; "
        "bad=[name for name in sys.modules if any(name == prefix or "
        "name.startswith(prefix + '.') for prefix in blocked)]; "
        "print(';'.join(sorted(bad)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == ""
