"""Clean-room and drift checks for generated Pine audit documents."""

from __future__ import annotations

import shutil
from pathlib import Path

from scripts.build_audit_docs import render_documents

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_audit_docs_render_without_preexisting_outputs(tmp_path: Path) -> None:
    research = tmp_path / "research"
    research.mkdir()
    for name in (
        "strategy_registry.yaml",
        "pine_findings.json",
        "strategy_catalog_analysis.md",
    ):
        shutil.copyfile(REPO_ROOT / "research" / name, research / name)

    assert not (tmp_path / "docs").exists()
    catalog, audit, script_count, statuses = render_documents(tmp_path)
    analysis = (research / "strategy_catalog_analysis.md").read_text(encoding="utf-8").rstrip()

    assert analysis in catalog
    assert catalog.count("## Corpus composition and duplication analysis") == 1
    assert "Detailed per-script findings: [PINE_AUDIT.md](PINE_AUDIT.md)." in catalog
    assert audit.startswith("# Pine Forensic Audit (Phase 2)\n")
    assert script_count == 42
    assert sum(statuses.values()) == script_count


def test_tracked_audit_docs_match_clean_render() -> None:
    catalog, audit, _, _ = render_documents(REPO_ROOT)

    assert (REPO_ROOT / "docs" / "STRATEGY_CATALOG.md").read_text(encoding="utf-8") == catalog
    assert (REPO_ROOT / "docs" / "PINE_AUDIT.md").read_text(encoding="utf-8") == audit
