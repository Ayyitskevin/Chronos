"""Contract tests for the source-driven Chronos documentation map."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude/skills/chronos-docs-map/SKILL.md"
STALE_INVENTORY = SKILL.parent / "references/doc-inventory.md"


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_docs_map_points_to_live_authorities() -> None:
    text = _skill_text()
    authorities = (
        "AGENTS.md",
        "docs/AGENT_PROTOCOL.md",
        "docs/VISION_COMPLETION_PLAN.md",
        "DECISIONS.md",
        "docs/adr/",
        "docs/ARCHITECTURE.md",
        "docs/generated/CURRENT_STATE.md",
        "docs/generated/capability-matrix.json",
        "scripts/build_current_state.py",
        "README.md",
        "CHANGELOG.md",
        "docs/TEST_PLAN.md",
        "docs/TEST_RESULTS.md",
        "RISK_REGISTER.md",
        "docs/safety.md",
        "docs/limitations.md",
        ".claude/skills/chronos-diagnostics/scripts/doc_drift_check.py",
        "src/chronos/",
        "tests/",
    )

    for authority in authorities:
        assert authority in text
        assert (ROOT / authority).exists()


def test_docs_map_derives_revision_inventory_and_history() -> None:
    text = _skill_text()
    commands = (
        "git status --short --branch",
        "git rev-parse HEAD",
        "git ls-remote --symref origin HEAD",
        "git log --oneline --branches --tags --not --remotes",
        "git ls-files -- '*.md'",
        "rg -n --glob '*.md' '^#{1,6} '",
        "git log --follow --",
        "git blame --",
    )

    for command in commands:
        assert command in text

    lower = text.lower()
    assert "checked-out revision" in lower
    assert "derive" in lower
    assert "inventory" in lower


def test_docs_map_classifies_document_roles_without_treating_roles_as_truth() -> None:
    text = _skill_text().lower()
    roles = (
        "governance and accepted intent",
        "current capability",
        "safety and limitations",
        "operations",
        "dated evidence",
        "history and context",
    )

    for role in roles:
        assert role in text

    assert "role is not truth" in text
    assert "executable source and exercising tests" in text
    assert "generated is not authorized" in text


def test_docs_map_requires_a_contradiction_proof_packet() -> None:
    text = _skill_text()
    fields = (
        "claim:",
        "document:",
        "authority_class:",
        "live_evidence:",
        "conflict:",
        "resolution_class:",
        "owner_gate:",
        "correction_sites:",
        "verification:",
    )

    for field in fields:
        assert field in text

    lower = text.lower()
    assert "stop and surface" in lower
    assert "never average" in lower
    assert "current executable facts" in lower


def test_docs_map_treats_diagnostics_as_candidate_findings() -> None:
    text = _skill_text().lower()

    assert "doc_drift_check.py" in text
    assert "candidate findings" in text
    assert "dated rule" in text
    assert "reverify" in text
    assert "bulk-edit" in text


def test_docs_map_preserves_correction_and_change_authority() -> None:
    text = _skill_text().lower()
    required = (
        "factual status correction",
        "historical supersession",
        "governance or authority proposal",
        "generated artifact regeneration",
        "canonical home",
        "in-place correction",
        "owner-gated",
        "gate_advanced: none",
        "scripts/build_current_state.py --check",
    )

    for fragment in required:
        assert fragment in text


def test_docs_map_routes_only_to_existing_skills() -> None:
    text = _skill_text()
    named_skills = set(re.findall(r"`(chronos-[a-z0-9-]+)`", text))

    assert "chronos-change-control" in named_skills
    assert "chronos-diagnostics" in named_skills
    assert "chronos-validation-and-qa" in named_skills
    for skill_name in named_skills:
        assert (ROOT / ".claude/skills" / skill_name / "SKILL.md").is_file(), skill_name


def test_docs_map_does_not_cache_point_in_time_state() -> None:
    text = _skill_text()
    forbidden_patterns = (
        r"\b20\d{2}-\d{2}-\d{2}\b",
        r"\b(?:branch tip|exact main|commit)\s+`?[0-9a-f]{7,40}\b",
        r"(?:src|tests|scripts|docs)/[^`\s)]+\.(?:py|md):\d+",
        r"\b\d[\d,]*\s+(?:markdown documents|adrs|tests|passed|skipped|warnings?)\b",
        r"\bLedger #\d+\b",
        r"\b(?:CURRENT|MIXED|STALE-UNBANNERED)\s+—",
        r"/home/(?:user|kevin-lee)/",
    )

    for pattern in forbidden_patterns:
        assert re.search(pattern, text, flags=re.IGNORECASE) is None, pattern

    assert "references/doc-inventory.md" not in text
    assert not STALE_INVENTORY.exists()


def test_docs_map_has_fresh_verification_and_review_closeout() -> None:
    text = _skill_text()

    assert ".venv/bin/python -m pytest -q tests/unit/test_docs_map_skill_contract.py" in text
    assert ".venv/bin/python scripts/build_current_state.py --check" in text
    assert "make gates" in text
    assert "non-author review" in text.lower()
    assert "changed-path" in text.lower()


def test_docs_map_description_routes_and_differentiates() -> None:
    text = _skill_text()
    frontmatter = text.split("---\n", 2)[1]
    metadata = yaml.safe_load(frontmatter)

    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "chronos-docs-map"
    description = metadata["description"].lower()
    assert "use for" in description
    assert "differentiator" in description
    assert "documentation" in description
    assert "checked-out" in description
