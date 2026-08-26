from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from chronos.research import qqq_campaign_readiness
from chronos.research.qqq_campaign_readiness import (
    EXPECTED_READINESS_SHA256,
    READINESS_ID,
    ArtifactCode,
    CampaignCompilationStatus,
    QQQCampaignReadinessError,
    RequirementCode,
    RequirementScope,
    RequirementState,
    compile_qqq_campaign_readiness,
)

_ROOT = Path(__file__).resolve().parents[2]
_MODULE = _ROOT / "src/chronos/research/qqq_campaign_readiness.py"
_SPEC = _ROOT / "specs/qqq_campaign_readiness_v1.json"


def test_exact_readiness_artifact_compiles_only_to_blocked_metadata() -> None:
    payload = _SPEC.read_bytes()
    compiled = compile_qqq_campaign_readiness()

    assert hashlib.sha256(payload).hexdigest() == EXPECTED_READINESS_SHA256
    assert compiled.readiness_id == READINESS_ID
    assert compiled.readiness_sha256 == EXPECTED_READINESS_SHA256
    assert compiled.status is CampaignCompilationStatus.BLOCKED_BEFORE_FIRST_DATA_READ
    assert compiled.order_authority == "none"
    assert compiled.promotion_authority == "none"
    assert compiled.registered_trials == 0
    assert compiled.data_read_permitted is False
    assert compiled.trial_registration_permitted is False
    assert compiled.holdout_unlock_permitted is False
    assert compiled.executable is False
    assert compiled.ready_for_first_data_read is False


def test_current_constitution_strategy_and_inert_paper_artifacts_are_authenticated() -> None:
    compiled = compile_qqq_campaign_readiness()

    assert [artifact.code for artifact in compiled.artifacts] == list(ArtifactCode)
    assert [artifact.path for artifact in compiled.artifacts] == [
        "research/qqq_v1_constitution.json",
        "specs/qqq_sma_control_v1.json",
        "specs/qqq_five_tool_candidate_v1.json",
        "src/chronos/supervisor/position_management.py",
        "src/chronos/supervisor/position_admission.py",
    ]
    by_code = {artifact.code: artifact for artifact in compiled.artifacts}
    assert by_code[ArtifactCode.CONSTITUTION].content_sha256 == (
        "4c99ce9d09f43a418c7342b0e40a0795b253bf3f1cd0e37d29419498b3008d56"
    )
    assert by_code[ArtifactCode.SMA_CONTROL].content_sha256 == (
        "a0ec83b3431016df0c599895ead65083fc72b5afb87073dfbdf046d68e23bb03"
    )
    assert by_code[ArtifactCode.CONFLUENCE_CANDIDATE].content_sha256 == (
        "59348ca3da9e9b68ec4edd1fc54572783e9256ae9c55ac18ffe844c0b4b78054"
    )
    assert by_code[ArtifactCode.PAPER_MANAGEMENT].semantic_sha256 == (
        "7a5b29eb8055b0b4cf0f80476cca200234cfe96afd5327101da7e76ac09ec188"
    )
    assert all(artifact.state == "locked_in_repository" for artifact in compiled.artifacts)


def test_requirements_separate_owner_inputs_build_work_shorts_and_activation() -> None:
    compiled = compile_qqq_campaign_readiness()

    assert compiled.execution_symbol == "QQQ"
    assert compiled.robustness_panel_symbols == ("QQQ", "SPY", "IWM", "DIA", "GLD", "TLT")
    assert compiled.bar_interval == "1D"
    assert compiled.primary_kpi == "net_edge_confidence"
    assert [requirement.code for requirement in compiled.requirements] == list(RequirementCode)

    by_code = {requirement.code: requirement for requirement in compiled.requirements}
    assert {requirement.code for requirement in compiled.owner_actions} == {
        RequirementCode.READINESS_OWNER_APPROVAL,
        RequirementCode.CERTIFIED_SIX_SYMBOL_EXPORT,
        RequirementCode.INDEPENDENT_CORPORATE_ACTION_ATTESTATION,
        RequirementCode.OWNER_APPROVED_HOLDOUT_MAP,
        RequirementCode.BENCHMARK_AND_CASH_LEG_IDENTITY,
        RequirementCode.LONG_COST_SCHEDULE_IDENTITY,
        RequirementCode.TRADINGVIEW_TRACE_EXPORT,
    }
    assert by_code[RequirementCode.CERTIFIED_RELEASE_AND_CATALOG].state is (
        RequirementState.CHRONOS_BUILD_REQUIRED
    )
    assert by_code[RequirementCode.POWER_ANALYSIS_IDENTITY].scope is RequirementScope.SHARED
    assert by_code[RequirementCode.BASE_FIVE_TOOL_BINDINGS].scope is (
        RequirementScope.CONFLUENCE_CANDIDATE
    )
    assert by_code[RequirementCode.SHORT_SIDE_EVIDENCE].state is RequirementState.UNAVAILABLE
    assert by_code[RequirementCode.SHORT_SIDE_EVIDENCE].blocks_long_campaign is False
    assert by_code[RequirementCode.REAL_PAPER_LIFECYCLE_EVIDENCE].state is (
        RequirementState.DEFERRED_ACTIVATION
    )
    assert all(
        requirement.state is not RequirementState.SATISFIED for requirement in compiled.requirements
    )


def test_qqq_release_and_base_five_tool_intake_remain_distinct_identities() -> None:
    compiled = compile_qqq_campaign_readiness()

    assert compiled.qqq_release_symbols == ("QQQ", "SPY", "IWM", "DIA", "GLD", "TLT")
    assert compiled.base_five_tool_intake_symbols == (
        "GLD",
        "IWM",
        "QQQ",
        "RSP",
        "SPY",
        "VIX",
        "VIX3M",
    )
    assert compiled.cross_dataset_identity_transfer == "forbidden"
    assert compiled.qqq_release_symbols != compiled.base_five_tool_intake_symbols


@pytest.mark.parametrize(
    "relative_path",
    [
        "research/qqq_v1_constitution.json",
        "specs/qqq_sma_control_v1.json",
        "specs/qqq_five_tool_candidate_v1.json",
        "src/chronos/supervisor/position_management.py",
        "src/chronos/supervisor/position_admission.py",
    ],
)
def test_any_referenced_artifact_drift_refuses(
    relative_path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = Path(__file__).resolve().parents[2]
    for artifact in compile_qqq_campaign_readiness().artifacts:
        source = source_root / artifact.path
        target = tmp_path / artifact.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    changed = tmp_path / relative_path
    changed.write_bytes(changed.read_bytes() + b"\n")
    monkeypatch.setattr(qqq_campaign_readiness, "_repo_root", lambda: tmp_path)

    with pytest.raises(QQQCampaignReadinessError, match="drifted"):
        compile_qqq_campaign_readiness()


@pytest.mark.parametrize(
    "compiler_name",
    ["compile_qqq_control", "compile_qqq_confluence_candidate"],
)
def test_a_referenced_compiler_cannot_silently_gain_authority(
    compiler_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler = getattr(qqq_campaign_readiness, compiler_name)
    unsafe = replace(compiler(), order_authority="paper")
    monkeypatch.setattr(qqq_campaign_readiness, compiler_name, lambda *_args, **_kwargs: unsafe)

    with pytest.raises(QQQCampaignReadinessError, match="authority"):
        compile_qqq_campaign_readiness()


@pytest.mark.parametrize(
    "compiler_name",
    ["compile_qqq_control", "compile_qqq_confluence_candidate"],
)
def test_a_referenced_compiler_cannot_silently_drop_a_blocker(
    compiler_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler = getattr(qqq_campaign_readiness, compiler_name)
    incomplete = replace(compiler(), blockers=compiler().blockers[:-1])
    monkeypatch.setattr(qqq_campaign_readiness, compiler_name, lambda *_args, **_kwargs: incomplete)

    with pytest.raises(QQQCampaignReadinessError, match="blocker set"):
        compile_qqq_campaign_readiness()


@pytest.mark.parametrize(
    "compiler_name",
    ["compile_qqq_control", "compile_qqq_confluence_candidate"],
)
def test_a_referenced_compiler_cannot_silently_duplicate_a_blocker(
    compiler_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler = getattr(qqq_campaign_readiness, compiler_name)
    compiled = compiler()
    duplicated = replace(compiled, blockers=(*compiled.blockers, compiled.blockers[-1]))
    monkeypatch.setattr(qqq_campaign_readiness, compiler_name, lambda *_args, **_kwargs: duplicated)

    with pytest.raises(QQQCampaignReadinessError, match="blocker set"):
        compile_qqq_campaign_readiness()


def test_any_readiness_byte_drift_refuses_before_interpretation(tmp_path: Path) -> None:
    document = json.loads(_SPEC.read_text())
    document["authority"]["order_authority"] = "paper"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(document, sort_keys=True))

    with pytest.raises(QQQCampaignReadinessError, match="readiness drifted"):
        compile_qqq_campaign_readiness(changed)


def test_readiness_import_has_only_known_direct_imports_and_no_authority_capability() -> None:
    tree = ast.parse(_MODULE.read_text(), filename=str(_MODULE))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    chronos_imports = {name for name in imports if name == "chronos" or name.startswith("chronos.")}
    assert chronos_imports == {
        "chronos.research.qqq_confluence",
        "chronos.research.qqq_control",
    }

    # qqq_confluence transitively loads existing Five-Tool market-data code. This
    # probe excludes authority dependencies; it is not a no-data-module claim.
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
        "import chronos.research.qqq_campaign_readiness, sys; "
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
