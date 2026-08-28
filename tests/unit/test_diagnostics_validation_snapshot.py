from __future__ import annotations

import importlib.util
import re
import sys
from datetime import date
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_SCRIPT = (
    ROOT / ".claude" / "skills" / "chronos-diagnostics" / "scripts" / "validation_snapshot.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def snapshot_module() -> ModuleType:
    return _load_module("validation_snapshot", SNAPSHOT_SCRIPT)


def test_reads_only_the_current_validation_summary(
    tmp_path: Path, snapshot_module: ModuleType
) -> None:
    results = tmp_path / "TEST_RESULTS.md"
    results.write_text(
        """# Test Results

## Summary (current — re-measured 2026-08-28)

Measured on exact `main` `0123456789abcdef0123456789abcdef01234567`, Python 3.12.

| Command | Result |
|---|---|
| `pytest -q` | **321 passed, 2 skipped, 9 warnings** (323 collected) |

## Summary (historical — re-measured 2026-08-02, superseded)

Measured on exact `main` `ffffffffffffffffffffffffffffffffffffffff`, Python 3.12.

| Command | Result |
|---|---|
| `pytest -q` | **99 passed, 1 skipped** (100 collected) |
""",
        encoding="utf-8",
    )

    snapshot = snapshot_module.read_validation_snapshot(results)

    assert snapshot is not None
    assert snapshot.measured_on == date(2026, 8, 28)
    assert snapshot.commit_sha == "0123456789abcdef0123456789abcdef01234567"
    assert snapshot.passed == 321
    assert snapshot.skipped == 2
    assert snapshot.collected == 323
    assert snapshot.describe() == (
        "323 collected / 321 passed, 2 skipped (2026-08-28 at 0123456789ab)"
    )


@pytest.mark.parametrize(
    "current_section",
    [
        """## Summary (historical — re-measured 2026-08-28, superseded)

Measured on exact `main` `0123456789abcdef0123456789abcdef01234567`.
| `pytest -q` | **321 passed, 2 skipped** (323 collected) |
""",
        """## Summary (current — re-measured 2026-08-28)

| `pytest -q` | **321 passed, 2 skipped** (323 collected) |
""",
        """## Summary (current — re-measured 2026-08-28)

Measured on exact `main` `0123456789abcdef0123456789abcdef01234567`.
| `pytest -q` | **321 passed, 2 skipped** (999 collected) |
""",
    ],
)
def test_rejects_missing_or_incoherent_current_evidence(
    tmp_path: Path, snapshot_module: ModuleType, current_section: str
) -> None:
    results = tmp_path / "TEST_RESULTS.md"
    results.write_text(f"# Test Results\n\n{current_section}", encoding="utf-8")

    assert snapshot_module.read_validation_snapshot(results) is None


def test_repository_current_summary_is_parseable(snapshot_module: ModuleType) -> None:
    snapshot = snapshot_module.read_validation_snapshot(ROOT / "docs" / "TEST_RESULTS.md")

    assert snapshot is not None
    assert len(snapshot.commit_sha) == 40
    assert snapshot.passed + snapshot.skipped == snapshot.collected


def test_diagnostics_derive_test_comparisons_from_the_current_summary() -> None:
    scripts = ROOT / ".claude" / "skills" / "chronos-diagnostics" / "scripts"
    state_inventory = (scripts / "state_inventory.py").read_text(encoding="utf-8")
    doc_drift = (scripts / "doc_drift_check.py").read_text(encoding="utf-8")

    for source in (state_inventory, doc_drift):
        assert "read_validation_snapshot" in source
        assert "2490" not in source
        assert "BASELINE_NOTE" not in source


@pytest.mark.parametrize(
    ("script_name", "function_name", "drop_warning"),
    [
        (
            "state_inventory.py",
            "report_test_collection",
            "collection count DROPPED below the documented 10 (9)",
        ),
        (
            "doc_drift_check.py",
            "live_collection_count",
            "live collection is below the documented 10 tests",
        ),
    ],
)
def test_collection_observers_warn_when_live_collection_drops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    snapshot_module: ModuleType,
    script_name: str,
    function_name: str,
    drop_warning: str,
) -> None:
    del snapshot_module
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "TEST_RESULTS.md").write_text(
        """# Test Results

## Summary (current — re-measured 2026-08-28)

Measured on exact `main` `0123456789abcdef0123456789abcdef01234567`.
| `pytest -q` | **10 passed, 0 skipped** (10 collected) |
""",
        encoding="utf-8",
    )
    python = tmp_path / "python"
    python.touch()
    monkeypatch.setenv("CHRONOS_DIAG_VENV_PYTHON", str(python))

    scripts = ROOT / ".claude" / "skills" / "chronos-diagnostics" / "scripts"
    module = _load_module(f"test_{script_name.removesuffix('.py')}", scripts / script_name)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="9 tests collected\n", stderr="", returncode=0
        ),
    )

    getattr(module, function_name)(tmp_path)

    assert drop_warning in capsys.readouterr().out


@pytest.mark.parametrize(
    ("script_name", "function_name"),
    [
        ("state_inventory.py", "report_test_collection"),
        ("doc_drift_check.py", "live_collection_count"),
    ],
)
def test_collection_observers_warn_when_current_evidence_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    snapshot_module: ModuleType,
    script_name: str,
    function_name: str,
) -> None:
    del snapshot_module
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "TEST_RESULTS.md").write_text("# Test Results\n", encoding="utf-8")
    monkeypatch.setenv("CHRONOS_DIAG_VENV_PYTHON", str(tmp_path / "missing-python"))

    scripts = ROOT / ".claude" / "skills" / "chronos-diagnostics" / "scripts"
    module = _load_module(f"invalid_{script_name.removesuffix('.py')}", scripts / script_name)

    getattr(module, function_name)(tmp_path)

    output = capsys.readouterr().out
    assert "no coherent current validation summary" in output
    assert "dated evidence" not in output


def test_routing_skills_do_not_cache_validation_or_branch_snapshots() -> None:
    skill_root = ROOT / ".claude" / "skills"
    skill_paths = (
        skill_root / "chronos-priorities-and-roadmap" / "SKILL.md",
        skill_root / "chronos-diagnostics" / "SKILL.md",
        skill_root / "chronos-real-gateway-campaign" / "SKILL.md",
    )
    forbidden = (
        "all four pass",
        "exactly one skip",
        "feat/wheel-dashboard-mvp",
        "chronos-option-chain-selection-v1",
        "76 pins",
        "1387",
        "6 => v7",
    )

    for path in skill_paths:
        text = path.read_text(encoding="utf-8")
        assert "docs/TEST_RESULTS.md" in text
        assert "make gates" in text
        assert re.search(r"\b[\d,]+\s+(?:passed|skipped|collected)\b", text) is None
        for stale_claim in forbidden:
            assert stale_claim not in text, f"{path} caches stale claim {stale_claim!r}"
