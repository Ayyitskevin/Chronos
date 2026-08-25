from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from chronos.research.qqq_control import (
    EXPECTED_PREREGISTRATION_SHA256,
    ControlBlockerCode,
    ControlCompilationStatus,
    QQQControlSpecError,
    compile_qqq_control,
    default_preregistration_path,
    load_qqq_control_preregistration,
)

_ROOT = Path(__file__).resolve().parents[2]
_MODULE = _ROOT / "src/chronos/research/qqq_control.py"


def test_exact_preregistration_compiles_only_to_blocked_metadata() -> None:
    payload = default_preregistration_path().read_bytes()
    compiled = compile_qqq_control()

    assert hashlib.sha256(payload).hexdigest() == EXPECTED_PREREGISTRATION_SHA256
    assert compiled.status is ControlCompilationStatus.BLOCKED_BEFORE_FIRST_DATA_READ
    assert compiled.order_authority == "none"
    assert compiled.promotion_authority == "none"
    assert compiled.registered_trials == 0
    assert compiled.data_read_permitted is False
    assert compiled.executable is False
    assert {blocker.code for blocker in compiled.blockers} == set(ControlBlockerCode)


def test_control_grid_is_primary_plus_four_one_axis_cells() -> None:
    compiled = compile_qqq_control()

    assert [cell.cell_id for cell in compiled.cells] == [
        "qqq-sma200-immediate-primary",
        "qqq-sma150-immediate-neighbor",
        "qqq-sma250-immediate-neighbor",
        "qqq-sma200-neutral-band-1pct",
        "qqq-sma200-five-close-confirmation",
    ]
    assert [cell.lookback_completed_sessions for cell in compiled.cells] == [
        200,
        150,
        250,
        200,
        200,
    ]
    assert compiled.cells[3].band_fraction == 0.01
    assert compiled.cells[4].confirmation_closes == 5


def test_chosen_equality_gap_cost_and_order_semantics_are_exact() -> None:
    _, document = load_qqq_control_preregistration()
    signal = document["signal"]
    order = document["entry_and_order_semantics"]
    economics = document["minimum_economic_trade"]
    assert isinstance(signal, dict)
    assert isinstance(order, dict)
    assert isinstance(economics, dict)

    assert signal["initialization"] == {
        "before_full_window": "flat",
        "first_full_window_above": "long",
        "first_full_window_below": "short",
        "first_full_window_equal": "remain_flat_until_first_strict_inequality",
    }
    assert signal["equality_after_initialization"] == "hold_prior_direction"
    assert order["market_protection_collar_fraction"] == 0.01
    assert order["one_attempt_per_event"] is True
    assert order["later_retry_or_chase"] is False
    assert "only_reduce_quantity_never_increase" in order["entry_gap_rule"]
    assert economics["maximum_projected_round_trip_cost_fraction_of_applicable_cvar_budget"] == 0.1


def test_any_byte_drift_refuses_before_interpretation(tmp_path: Path) -> None:
    document = json.loads(default_preregistration_path().read_text())
    document["authority"]["order_authority"] = "paper"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(document, sort_keys=True))

    with pytest.raises(QQQControlSpecError, match="preregistration drifted"):
        compile_qqq_control(changed)


def test_spec_loader_has_no_data_holdout_trial_or_execution_capability_import() -> None:
    tree = ast.parse(_MODULE.read_text(), filename=str(_MODULE))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    forbidden = (
        "chronos.marketdata",
        "chronos.histdata",
        "chronos.registry",
        "chronos.broker",
        "chronos.orders",
        "chronos.execution",
        "chronos.control",
        "chronos.supervisor",
    )
    assert not any(
        name == prefix or name.startswith(f"{prefix}.") for name in imports for prefix in forbidden
    )
