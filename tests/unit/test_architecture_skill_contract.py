"""Contract tests for the source-driven Chronos architecture skill."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude/skills/chronos-architecture-contract/SKILL.md"


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_architecture_skill_points_to_live_authorities() -> None:
    text = _skill_text()
    authorities = (
        "AGENTS.md",
        "docs/AGENT_PROTOCOL.md",
        "docs/ARCHITECTURE.md",
        "docs/VISION_COMPLETION_PLAN.md",
        "docs/generated/CURRENT_STATE.md",
        "docs/generated/capability-matrix.json",
        "DECISIONS.md",
        "docs/adr/",
        "RISK_REGISTER.md",
        "docs/safety.md",
        "docs/limitations.md",
        "pyproject.toml",
        "src/chronos/",
        "tests/safety/",
        "tests/integration/",
        "tests/unit/",
    )

    for authority in authorities:
        assert authority in text
        assert (ROOT / authority).exists()


def test_architecture_skill_derives_revision_topology_and_decisions() -> None:
    text = _skill_text()
    commands = (
        "git status --short --branch",
        "git rev-parse HEAD",
        "find src/chronos -mindepth 1 -maxdepth 1 -type d",
        "rg -n '^from chronos|^import chronos' src/chronos",
        "rg -n '^Status:' docs/adr",
        "git log --oneline --branches --tags --not --remotes",
    )

    for command in commands:
        assert command in text

    assert "! -name __pycache__" in text
    assert "derive" in text.lower()
    assert "checked-out revision" in text.lower()


def test_architecture_skill_answers_the_four_invariables() -> None:
    text = _skill_text().lower()
    questions = (
        "where does state live?",
        "where does feedback live?",
        "what breaks if i delete this?",
        "when does timing work?",
    )

    for question in questions:
        assert question in text


def test_architecture_skill_requires_a_proof_packet_per_claim() -> None:
    text = _skill_text()
    fields = (
        "state_owner:",
        "entrypoints:",
        "callers:",
        "feedback:",
        "timing:",
        "enforcing_tests:",
        "failure_if_changed:",
        "evidence_status:",
    )

    for field in fields:
        assert field in text


def test_architecture_skill_derives_boundaries_and_weak_points() -> None:
    text = _skill_text()
    required_fragments = (
        "chronos.orders",
        "chronos.execution",
        "chronos.autonomy",
        "chronos.supervisor",
        "git grep -n 'transmit=True\\|order.transmit = True'",
        "sed -n '/^## 6/,/^## 7/p' docs/VISION_COMPLETION_PLAN.md",
        "rg -n 'OPEN|MITIGATED|ACCEPTED|CLOSED' RISK_REGISTER.md",
        "MITIGATED",
        "CLOSED",
        "operational evidence",
    )

    for fragment in required_fragments:
        assert fragment in text


def test_architecture_skill_discovers_and_runs_structural_tests() -> None:
    text = _skill_text()
    referenced_tests = set(re.findall(r"tests/(?:safety|unit|integration)/[a-z0-9_]+\.py", text))

    assert referenced_tests
    for relative in referenced_tests:
        assert (ROOT / relative).is_file(), relative

    assert "rg -n" in text
    assert "tests/safety" in text
    assert "focused" in text.lower()
    assert "make gates" in text


def test_architecture_skill_routes_only_to_existing_skills() -> None:
    text = _skill_text()
    named_skills = set(re.findall(r"`(chronos-[a-z0-9-]+)`", text))

    assert "chronos-change-control" in named_skills
    assert "chronos-docs-map" in named_skills
    for skill_name in named_skills:
        path = ROOT / ".claude/skills" / skill_name / "SKILL.md"
        assert path.is_file(), skill_name


def test_architecture_skill_preserves_change_authority() -> None:
    text = _skill_text().lower()

    assert "owner-gated" in text
    assert "owner-independent" in text
    assert "task contract" in text
    assert "authority" in text
    assert "safety boundary" in text
    assert "gate_advanced: none" in text


def test_architecture_skill_does_not_cache_point_in_time_claims() -> None:
    text = _skill_text()
    forbidden_patterns = (
        r"\b(?:facts?|content) (?:dated|verified) 20\d{2}",
        r"\bverified (?:against|current) 20\d{2}",
        r"/home/(?:user|kevin-lee)/",
        r"\b(?:branch tip|exact main|commit)\s+`?[0-9a-f]{7,40}\b",
        r"\.py:\d+",
        r"\bADR-\d{4}\.\.\d{4}\b",
        r"\b(?:schema(?: version)?|migration head)\s+v?\d+\b",
        r"\b\d+\s+(?:zones|invariants|packages|visible commits)\b",
        r"\bno real gateway (?:ever|has ever)\b",
        r"\b(?:all|the \d+) (?:phase-\d+ )?findings? (?:are|still) open\b",
    )

    for pattern in forbidden_patterns:
        assert re.search(pattern, text, flags=re.IGNORECASE) is None, pattern


def test_architecture_skill_description_routes_and_differentiates() -> None:
    text = _skill_text()
    frontmatter = text.split("---\n", 2)[1]
    metadata = yaml.safe_load(frontmatter)

    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "chronos-architecture-contract"
    description = metadata["description"].lower()
    assert "use for" in description
    assert "differentiator" in description
    assert "cross-package" in description
    assert "chronos-change-control" in description
