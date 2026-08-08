"""Frozen contract tests for Five-Tool Confluence AIO v3.6."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

import chronos.research.five_tool as five_tool
from chronos.research.five_tool import (
    ContractDriftError,
    FiveToolEngine,
    FiveToolSettings,
    default_input_values,
    input_contract_digest,
    load_contract,
    semantic_contract_digest,
    state_from_json,
    state_to_json,
)

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "research/pine/00_five_tool_confluence_aio.pine"
SPEC = ROOT / "specs/five_tool_confluence_v3_6.yaml"
GENERATOR = ROOT / "scripts/build_five_tool_input_contract.py"
PINNED_SHA256 = "e51d5a40d2e933bf86847c7432364ba8934fd2de653d6aec3d7205639248e45f"
PINNED_INPUT_DIGEST = "93273762b1d01dade4133628a9a2cebf0a1364774fde654a9efc07c4ccf6d049"


def test_live_source_has_independently_counted_pinned_identity() -> None:
    source_bytes = SOURCE.read_bytes()
    source = source_bytes.decode("utf-8")
    independent_declarations = re.findall(
        r"(?m)^\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*input\.[A-Za-z_][A-Za-z0-9_]*\s*\(",
        source,
    )

    assert hashlib.sha256(source_bytes).hexdigest() == PINNED_SHA256
    assert len(source.splitlines()) == 2443
    assert len(independent_declarations) == 219


def test_loader_exposes_all_inputs_in_source_order_and_immutable_records() -> None:
    contract = load_contract()

    assert contract.pine.source_sha256 == PINNED_SHA256
    assert contract.document_kind == "pine_input_contract"
    assert contract.pine.input_count == 219
    assert len(contract.inputs) == 219
    assert contract.inputs[0].name == "enable_orders"
    assert contract.inputs[-1].name == "use_json_alerts"
    assert [item.ordinal for item in contract.inputs] == list(range(1, 220))
    assert Counter(item.pine_type for item in contract.inputs) == {
        "bool": 75,
        "float": 66,
        "int": 49,
        "session": 2,
        "source": 3,
        "string": 19,
        "symbol": 1,
        "time": 3,
        "timeframe": 1,
    }
    with pytest.raises(FrozenInstanceError):
        contract.pine.input_count = 0  # type: ignore[misc]


def test_representative_time_source_session_symbol_and_bounds_are_exact() -> None:
    contract = load_contract()

    test_start = contract.input("test_start")
    assert test_start.pine_type == "time"
    assert test_start.default.kind == "timestamp"
    assert test_start.default.expression == 'timestamp("1 Jan 2018 00:00 +0000")'
    assert test_start.default.value == "1 Jan 2018 00:00 +0000"

    external = contract.input("ext_regime_src")
    assert external.pine_type == "source"
    assert external.default.kind == "expression"
    assert external.default.expression == "close"
    assert external.group == "1b · External Regime Override"

    session = contract.input("short_plus_session")
    assert session.pine_type == "session"
    assert session.default.value == "0935-1530"

    benchmark = contract.input("bench_sym")
    assert benchmark.pine_type == "symbol"
    assert benchmark.default.value == "AMEX:SPY"
    assert benchmark.title == "Benchmark"

    timeframe = contract.input("htf_tf")
    assert timeframe.pine_type == "timeframe"
    assert timeframe.default.value == "D"

    threshold = contract.input("min_oos_pf_warn")
    assert threshold.default.value == 1.05
    assert threshold.minval is not None and threshold.minval.value == 0.1
    assert threshold.maxval is not None and threshold.maxval.value == 5.0
    assert threshold.step is not None and threshold.step.value == 0.05


def test_engine_helpers_return_complete_ordered_defaults_and_stable_digest() -> None:
    defaults = default_input_values()

    assert len(defaults) == 219
    assert next(iter(defaults.items())) == ("enable_orders", True)
    assert defaults["test_start"] == "1 Jan 2018 00:00 +0000"
    assert defaults["ext_regime_src"] == "close"
    assert defaults["short_plus_session"] == "0935-1530"
    assert defaults["bench_sym"] == "AMEX:SPY"
    assert defaults["htf_tf"] == "D"
    assert re.fullmatch(r"[0-9a-f]{64}", input_contract_digest())
    assert input_contract_digest() == PINNED_INPUT_DIGEST
    assert re.fullmatch(r"[0-9a-f]{64}", semantic_contract_digest())


def test_public_signal_workflow_surface_round_trips_initial_state() -> None:
    expected = {
        "AccountSnapshot",
        "FiveToolBarInput",
        "FiveToolEngine",
        "FiveToolInputError",
        "FiveToolSettings",
        "FiveToolState",
        "FiveToolTrace",
        "SetupFamily",
        "Side",
        "align_five_tool_inputs",
        "evaluate_batch",
        "resume_batch",
        "state_from_json",
        "state_to_json",
    }
    assert expected <= set(five_tool.__all__)
    settings = FiveToolSettings.defaults(history_start_utc=datetime(2026, 1, 2, tzinfo=UTC))
    engine = FiveToolEngine(settings)
    assert state_from_json(state_to_json(engine.checkpoint())) == engine.checkpoint()


def test_multiline_calls_options_and_resolved_groups_are_preserved() -> None:
    contract = load_contract()
    validation_position = contract.input("validation_pos_in")

    assert "\n" in validation_position.declaration
    assert validation_position.options == (
        "top_left",
        "top_center",
        "top_right",
        "bottom_left",
        "bottom_center",
        "bottom_right",
    )
    assert validation_position.options_expression is not None
    assert validation_position.group_expression == "grp_val"
    assert validation_position.group == "0a · Research & Validation"


def test_manual_semantics_cover_timing_dependencies_warmups_and_known_deviations() -> None:
    semantics = load_contract().semantics

    assert semantics.parity_status == "UNVERIFIED"
    assert {item.id for item in semantics.timing} >= {
        "closed-primary-bars",
        "next-bar-orders",
        "prior-completed-htf",
        "history-pinned-markov",
    }
    assert [stage.ordinal for stage in semantics.dependency_order] == list(range(1, 8))
    assert len(semantics.warmups) >= 8
    assert {item.id for item in semantics.deviations} >= {
        "tradingview-owner-export",
        "intrabar-fill-priority",
        "three-leg-milestones",
        "side-switch-attribution",
        "stop-guard-asymmetry",
        "duplicate-alert-paths",
        "profit-factor-infinity",
        "daily-loss-on-daily-bars",
        "long-plus-age-gate",
    }


def test_artifact_schema_is_complete_for_every_input() -> None:
    document = json.loads(SPEC.read_text(encoding="utf-8"))
    assert set(document) == {
        "schema_version",
        "document_kind",
        "strategy_id",
        "capability_scope",
        "owner_approved",
        "pine",
        "semantics",
        "inputs",
    }
    assert set(document["pine"]) == {
        "source_path",
        "source_sha256",
        "source_line_count",
        "pine_language_version",
        "script_version",
        "input_count",
    }
    required_input_fields = {
        "ordinal",
        "name",
        "pine_type",
        "declaration_line",
        "declaration",
        "default",
        "title",
        "title_expression",
        "options",
        "options_expression",
        "minval",
        "maxval",
        "step",
        "group",
        "group_expression",
        "tooltip",
        "tooltip_expression",
        "inline",
        "extra_positional_arguments",
        "extra_named_arguments",
    }
    for item in document["inputs"]:
        assert set(item) == required_input_fields
        assert set(item["default"]) == {"expression", "kind", "value"}
        assert item["extra_positional_arguments"] == []
        assert item["extra_named_arguments"] == {}


def test_regeneration_is_byte_deterministic_and_check_mode_passes(tmp_path: Path) -> None:
    regenerated = tmp_path / "five_tool.yaml"
    first = subprocess.run(
        [sys.executable, str(GENERATOR), "--source", str(SOURCE), "--output", str(regenerated)],
        check=True,
        capture_output=True,
        text=True,
    )
    first_bytes = regenerated.read_bytes()
    second = subprocess.run(
        [sys.executable, str(GENERATOR), "--source", str(SOURCE), "--output", str(regenerated)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "wrote" in first.stdout
    assert "wrote" in second.stdout
    assert regenerated.read_bytes() == first_bytes == SPEC.read_bytes()
    checked = subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "--source",
            str(SOURCE),
            "--output",
            str(regenerated),
            "--check",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "current" in checked.stdout


def test_loader_fails_closed_when_source_bytes_drift(tmp_path: Path) -> None:
    changed_source = tmp_path / "changed.pine"
    changed_source.write_bytes(SOURCE.read_bytes() + b"// deliberate test drift\n")

    with pytest.raises(ContractDriftError, match="SHA256 drift"):
        load_contract(contract_path=SPEC, source_path=changed_source)
