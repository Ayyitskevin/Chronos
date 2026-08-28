"""Contract tests for the generated Phase 0 capability/current-state artifacts."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError

from chronos.api.autonomy_wiring import BackendGatherers
from chronos.autonomy.enums import AutonomyMode, DecisionKind, StrategyForm, TradableAssetClass
from chronos.broker.demo import DemoBroker
from chronos.broker.ibkr import IBKRBroker
from chronos.broker.official_ibkr import OfficialIBKRBroker
from chronos.config.settings import Settings
from chronos.domain.enums import BrokerAdapter, BrokerMode, IBEnvironment
from chronos.runtime import build_runtime
from chronos.supervisor.compiler import _CAPABILITY_MATRIX, _CLOSING_MATRIX

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "build_current_state.py"
MATRIX = ROOT / "docs" / "generated" / "capability-matrix.json"
CURRENT_STATE = ROOT / "docs" / "generated" / "CURRENT_STATE.md"
ARCHITECTURE = ROOT / "docs" / "ARCHITECTURE.md"


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_current_state", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _matrix() -> dict[str, object]:
    loaded = json.loads(MATRIX.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _rows(document: dict[str, object] | None = None) -> list[dict[str, object]]:
    loaded = document or _matrix()
    columns = loaded["matrix_columns"]
    values = loaded["matrix_rows"]
    assert isinstance(columns, list)
    assert isinstance(values, list)
    return [dict(zip(columns, row, strict=True)) for row in values]


def _function_ast(function: object) -> ast.FunctionDef | ast.AsyncFunctionDef:
    parsed = ast.parse(textwrap.dedent(inspect.getsource(function)))
    node = parsed.body[0]
    assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    return node


def _if_chain(node: ast.If) -> tuple[list[str], list[list[ast.stmt]], list[ast.stmt]]:
    tests: list[str] = []
    bodies: list[list[ast.stmt]] = []
    current = node
    while True:
        tests.append(ast.unparse(current.test))
        bodies.append(current.body)
        if len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
            current = current.orelse[0]
            continue
        return tests, bodies, current.orelse


def _assigned_constructor(statements: list[ast.stmt], target: str) -> str:
    for statement in statements:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == target
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
        ):
            return statement.value.func.id
    raise AssertionError(f"no constructor assignment found for {target}")


def test_committed_artifacts_are_current() -> None:
    completed = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "current-state artifacts are current" in completed.stdout


def test_regeneration_is_byte_deterministic(tmp_path: Path) -> None:
    generated_matrix = tmp_path / "matrix.json"
    generated_page = tmp_path / "current.md"
    command = [
        sys.executable,
        str(GENERATOR),
        "--matrix-output",
        str(generated_matrix),
        "--current-state-output",
        str(generated_page),
    ]
    subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    first = (generated_matrix.read_bytes(), generated_page.read_bytes())
    subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)

    assert (generated_matrix.read_bytes(), generated_page.read_bytes()) == first
    assert first == (MATRIX.read_bytes(), CURRENT_STATE.read_bytes())


def test_matrix_is_the_full_compiler_adapter_mode_cross_product() -> None:
    document = _matrix()
    capabilities = document["compiler_capabilities"]
    evidence_profiles = document["evidence_profiles"]
    rows = _rows(document)
    assert isinstance(capabilities, list)
    assert isinstance(evidence_profiles, list)
    assert isinstance(rows, list)
    expected_compiler_rows = len(_CAPABILITY_MATRIX) + len(_CLOSING_MATRIX)
    expected_matrix_rows = (
        expected_compiler_rows * len(BrokerAdapter) * len(AutonomyMode) * len(evidence_profiles)
    )

    assert len(capabilities) == expected_compiler_rows
    assert document["matrix_row_count"] == expected_matrix_rows == len(rows)
    row_keys = {
        (
            row["asset_family"],
            row["decision_kind"],
            row["strategy_shape"],
            row["broker_adapter"],
            row["mode"],
            row["evidence_source"],
        )
        for row in rows
    }
    assert len(row_keys) == len(rows)
    assert {
        (
            row["asset_family"],
            row["decision_kind"],
            row["strategy_shape"],
            row["order_intent"],
        )
        for row in capabilities
    } == {
        (asset.value, decision.value, strategy.value, intent.value)
        for (asset, decision, strategy), intent in _CAPABILITY_MATRIX.items()
    } | {
        (asset.value, decision.value, None, intent.value)
        for (asset, decision), intent in _CLOSING_MATRIX.items()
    }


def test_every_matrix_row_carries_the_required_phase_zero_axes() -> None:
    document = _matrix()
    columns = document["matrix_columns"]
    assert isinstance(columns, list)
    required = {
        "asset_family",
        "decision_kind",
        "strategy_shape",
        "broker_adapter",
        "mode",
        "evidence_source",
        "promotion_status",
    }
    assert required <= set(columns)
    assert all(len(row) == len(columns) for row in document["matrix_rows"])


def test_mapped_and_unmapped_vocabulary_are_complete_disjoint_partitions() -> None:
    document = _matrix()
    capabilities = document["compiler_capabilities"]
    unmapped = document["unmapped_vocabulary"]
    assert isinstance(capabilities, list)
    assert isinstance(unmapped, dict)

    cases = (
        ("asset_family", "asset_families", TradableAssetClass),
        ("decision_kind", "decision_kinds", DecisionKind),
        ("strategy_shape", "strategy_shapes", StrategyForm),
    )
    for mapped_key, unmapped_key, vocabulary in cases:
        mapped = {row[mapped_key] for row in capabilities if row[mapped_key] is not None}
        absent = set(unmapped[unmapped_key])
        assert mapped.isdisjoint(absent)
        assert mapped | absent == {item.value for item in vocabulary}


def test_generation_cannot_claim_default_authority() -> None:
    document = _matrix()
    semantics = document["authority_semantics"]
    defaults = document["repository_defaults"]
    rows = _rows(document)
    assert isinstance(semantics, dict)
    assert isinstance(defaults, dict)
    assert isinstance(rows, list)

    assert semantics["reads_external_state"] is False
    assert semantics["default_runtime_status"] == "INERT_NO_MANDATE"
    assert defaults["autonomy_mandate_file"] is None
    assert defaults["allow_order_transmit"] is False
    assert defaults["allow_live_trading"] is False
    assert defaults["enable_autonomy_option_selection"] is False
    assert {row["promotion_status"] for row in rows} == {"NOT_CONFIGURED_BY_DEFAULT"}
    assert not any("AUTHORIZED" in str(row["current_status"]) for row in rows)


def test_decision_evidence_sources_include_default_and_both_bound_origins() -> None:
    document = _matrix()
    profiles = document["evidence_profiles"]
    rows = _rows(document)
    assert isinstance(profiles, list)
    assert isinstance(rows, list)
    by_source = {profile["evidence_source"]: profile for profile in profiles}

    assert set(by_source) == {"placeholder_unbound", "backend_served", "alert_attested"}
    assert by_source["placeholder_unbound"]["binding_status"] == "DEFAULT_UNBOUND"
    assert by_source["placeholder_unbound"]["configuration_required"] is False
    assert by_source["backend_served"]["citation_kinds"] == ["worker_evidence_snapshot"]
    assert by_source["alert_attested"]["citation_kinds"] == ["tradingview_alert"]
    assert {row["evidence_source"] for row in rows} == set(by_source)
    assert {profile["market_evidence_source"] for profile in document["adapter_profiles"]} == {
        "DEMO_BROKER_FIXTURE",
        "IBKR_GATEWAY_OFFICIAL_API",
        "IBKR_GATEWAY_IB_ASYNC_READ_ONLY",
    }


def test_instrument_fact_gaps_and_option_default_are_visible() -> None:
    rows = _rows()

    option_open = [
        row
        for row in rows
        if row["asset_family"] == "EQUITY_OPTION" and row["decision_kind"] == "OPEN"
    ]
    option_closing = [
        row
        for row in rows
        if row["asset_family"] == "EQUITY_OPTION" and row["decision_kind"] in {"CLOSE", "REDUCE"}
    ]
    assert option_open
    assert {row["instrument_facts_status"] for row in option_open} == {
        "OPTION_SELECTION_RECEIPT_DISABLED_BY_DEFAULT"
    }
    assert option_closing
    assert {row["instrument_facts_status"] for row in option_closing} == {
        "UNAVAILABLE_IN_PRODUCTION_GATHERER"
    }
    ib_async_crypto = [
        row
        for row in rows
        if row["asset_family"] == "CRYPTO" and row["broker_adapter"] == "ib_async"
    ]
    assert ib_async_crypto
    assert {row["instrument_facts_status"] for row in ib_async_crypto} == {
        "UNAVAILABLE_ADAPTER_QUALIFY_CRYPTO"
    }
    assert not any(
        row["current_status"] == "CONDITIONAL_OWNER_AND_EVIDENCE_GATED"
        for row in rows
        if row["broker_adapter"] == "ib_async"
    )


def test_adapter_profiles_pin_demo_alias_and_live_adapter_boundary() -> None:
    profiles = _matrix()["adapter_profiles"]
    assert isinstance(profiles, list)
    by_adapter = {row["broker_adapter"]: row for row in profiles}

    assert set(by_adapter) == {item.value for item in BrokerAdapter}
    assert by_adapter["demo"]["paper_submission_path"] is False
    assert by_adapter["demo"]["live_submission_path"] is False
    assert by_adapter["official_ibkr"]["paper_submission_path"] is True
    assert by_adapter["official_ibkr"]["live_submission_path"] is True
    assert by_adapter["ib_async"]["paper_submission_path"] is False
    assert by_adapter["ib_async"]["live_submission_path"] is False
    assert by_adapter["demo"]["submit_order_status"] == "UNCONDITIONAL_REFUSAL"
    assert by_adapter["official_ibkr"]["submit_order_status"] == "IMPLEMENTED"
    assert by_adapter["ib_async"]["submit_order_status"] == "UNCONDITIONAL_REFUSAL"
    assert _matrix()["configuration_findings"] == [
        {
            "id": "BROKER_ADAPTER_DEMO_IBKR_ALIAS",
            "status": "UNRESOLVED",
            "observation": (
                "BrokerMode.DEMO selects DemoBroker without consulting broker_adapter; "
                "BrokerMode.IBKR plus BrokerAdapter.DEMO reaches build_runtime's fallback "
                "and constructs OfficialIBKRBroker."
            ),
            "source": "src/chronos/runtime.py:build_runtime",
        }
    ]


def test_adapter_path_claims_match_executable_settings_and_runtime_selection() -> None:
    paper_base = {
        "broker_mode": BrokerMode.IBKR,
        "ib_environment": IBEnvironment.PAPER,
        "allow_order_transmit": True,
        "allow_live_trading": False,
        "ib_account_id": "DU12345",
        "ib_account_allowlist": ("DU12345",),
    }
    for adapter in (BrokerAdapter.OFFICIAL_IBKR, BrokerAdapter.IB_ASYNC):
        configured = Settings.model_validate({**paper_base, "broker_adapter": adapter})
        assert configured.transmission_possible is True

    demo = Settings.model_validate(
        {
            **paper_base,
            "broker_mode": BrokerMode.DEMO,
            "broker_adapter": BrokerAdapter.DEMO,
        }
    )
    assert demo.transmission_possible is False

    live_base = {
        **paper_base,
        "ib_environment": IBEnvironment.LIVE,
        "allow_live_trading": True,
        "ib_account_id": "U12345",
        "ib_account_allowlist": ("U12345",),
    }
    official_live = Settings.model_validate(
        {**live_base, "broker_adapter": BrokerAdapter.OFFICIAL_IBKR}
    )
    assert official_live.live_transmission_possible is True
    for adapter in (BrokerAdapter.DEMO, BrokerAdapter.IB_ASYNC):
        with pytest.raises(ValidationError, match="only adapter with a validated live order path"):
            Settings.model_validate({**live_base, "broker_adapter": adapter})

    runtime_ast = _function_ast(build_runtime)
    selector = next(
        node
        for node in ast.walk(runtime_ast)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "settings.broker_mode is BrokerMode.DEMO"
    )
    tests, bodies, fallback = _if_chain(selector)
    assert tests == [
        "settings.broker_mode is BrokerMode.DEMO",
        "settings.broker_adapter is BrokerAdapter.IB_ASYNC",
    ]
    assert [_assigned_constructor(body, "broker") for body in bodies] == [
        "DemoBroker",
        "IBKRBroker",
    ]
    assert _assigned_constructor(fallback, "broker") == "OfficialIBKRBroker"

    assert isinstance(_function_ast(DemoBroker.submit_order).body[-1], ast.Raise)
    assert isinstance(_function_ast(IBKRBroker.submit_order).body[-1], ast.Raise)
    assert isinstance(_function_ast(OfficialIBKRBroker.submit_order).body[-1], ast.Return)
    assert isinstance(_function_ast(IBKRBroker.qualify_crypto).body[-1], ast.Raise)
    assert not isinstance(_function_ast(DemoBroker.qualify_crypto).body[-1], ast.Raise)
    assert not isinstance(_function_ast(OfficialIBKRBroker.qualify_crypto).body[-1], ast.Raise)


def test_instrument_fact_claims_match_the_production_gatherer_branches() -> None:
    gatherer_ast = _function_ast(BackendGatherers.instrument_facts)
    try_node = next(node for node in ast.walk(gatherer_ast) if isinstance(node, ast.Try))
    selector = try_node.body[0]
    assert isinstance(selector, ast.If)
    tests, _, fallback = _if_chain(selector)
    assert tests == [
        "decision.asset_class is TradableAssetClass.EQUITY",
        "decision.asset_class is TradableAssetClass.CRYPTO",
        (
            "decision.asset_class is TradableAssetClass.EQUITY_OPTION and decision.kind is "
            "DecisionKind.OPEN"
        ),
    ]
    assert len(fallback) == 1
    assert isinstance(fallback[0], ast.Return)
    assert isinstance(fallback[0].value, ast.Constant) and fallback[0].value.value is None


def test_source_fingerprints_bind_every_declared_input() -> None:
    module = _generator()
    sources = _matrix()["source_fingerprints"]
    assert isinstance(sources, list)
    assert [Path(item["path"]) for item in sources] == list(module.SOURCE_PATHS)
    for source in sources:
        assert source["sha256"] == hashlib.sha256((ROOT / source["path"]).read_bytes()).hexdigest()


def test_current_state_page_is_generated_and_links_the_machine_artifact() -> None:
    page = CURRENT_STATE.read_text(encoding="utf-8")
    assert "Generated file — do not hand-edit" in page
    assert "**not authorization**" in page
    assert "INERT_NO_MANDATE" in page
    assert "UNAVAILABLE_IN_PRODUCTION_GATHERER" in page
    assert "UNAVAILABLE_ADAPTER_QUALIFY_CRYPTO" in page
    assert "BrokerAdapter.DEMO" in page
    assert "placeholder_unbound" in page
    assert "backend_served" in page
    assert "alert_attested" in page
    assert "[`capability-matrix.json`](capability-matrix.json)" in page
    assert not any(line.endswith(" ") for line in page.splitlines())


def test_canonical_architecture_page_links_the_generated_current_state() -> None:
    architecture = ARCHITECTURE.read_text(encoding="utf-8")
    assert "[generated/CURRENT_STATE.md](generated/CURRENT_STATE.md)" in architecture
