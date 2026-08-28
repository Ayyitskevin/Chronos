from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude" / "skills" / "chronos-change-control" / "SKILL.md"
VISION_PLAN = ROOT / "docs" / "VISION_COMPLETION_PLAN.md"
MAKEFILE = ROOT / "Makefile"


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_change_control_skill_points_to_live_authorities() -> None:
    text = _skill_text()
    required_sources = (
        "AGENTS.md",
        "docs/AGENT_PROTOCOL.md",
        "docs/VISION_COMPLETION_PLAN.md",
        "DECISIONS.md",
        "docs/adr/",
        "docs/safety.md",
        "docs/limitations.md",
        "RISK_REGISTER.md",
        "Makefile",
        ".github/workflows/ci.yml",
    )

    for source in required_sources:
        assert source in text, f"change-control skill does not point to {source}"


def test_change_control_skill_tracks_the_live_task_contract() -> None:
    text = _skill_text()
    vision = VISION_PLAN.read_text(encoding="utf-8")
    section = vision.split("## 13. Agent task contract", maxsplit=1)[1].split("## 14.", maxsplit=1)[
        0
    ]
    fields = re.findall(r"^([a-z_]+):", section, flags=re.MULTILINE)

    assert fields, "task-contract field discovery found no fields"
    for field in fields:
        assert f"{field}:" in text, f"change-control skill omits task-contract field {field}"


def test_change_control_skill_derives_the_gate_and_repository_state() -> None:
    text = _skill_text()
    makefile = MAKEFILE.read_text(encoding="utf-8")
    gate_match = re.search(r"^gates:\s+(.+)$", makefile, flags=re.MULTILINE)

    assert gate_match is not None, "Makefile has no gates dependency declaration"
    assert gate_match.group(1).split(), "Makefile gates target has no dependencies"
    required_commands = (
        "make gates",
        "git ls-remote --symref origin HEAD",
        "git log --oneline --branches --tags --not --remotes",
        "git worktree add",
        "git merge-base --is-ancestor",
        "alembic heads",
    )
    for command in required_commands:
        assert command in text, f"change-control skill omits derivation command {command}"


def test_change_control_skill_distinguishes_merge_authority() -> None:
    text = _skill_text().lower()

    assert "owner-gated" in text
    assert "owner-independent" in text
    assert "owning seat" in text
    assert "owner_gate: required" in text


def test_change_control_skill_does_not_cache_point_in_time_claims() -> None:
    text = _skill_text()
    forbidden_patterns = (
        r"\bdated 20\d{2}-\d{2}-\d{2}\b",
        r"\bwritten 20\d{2}-\d{2}-\d{2}\b",
        r"\bcontent verified against\b",
        r"\bthe owner still merges every pr\b",
        r"\bowner performed every integration\b",
        r"\bthe four gates\b",
        r"\b(?:migration )?head\s+`?\d{4}\b",
        r"\bhighest adr number\b",
        r"\bshallow clone:\s*\d+\b",
        r"\b\d+\s+merge commits\b",
        r"feat/wheel-dashboard-mvp",
        r"\b\d[\d,]*\s+(?:passed|skipped|collected)\b",
        r"\bline numbers drift\b",
    )

    for pattern in forbidden_patterns:
        assert re.search(pattern, text, flags=re.IGNORECASE) is None, (
            f"change-control skill caches a stale claim matching {pattern!r}"
        )


def test_change_control_skill_has_integration_pitfalls() -> None:
    text = _skill_text()

    assert "## Known pitfalls" in text
    assert "green gate" in text.lower()
    assert "stacked" in text.lower()
    assert "squash" in text.lower()
    assert "hold" in text.lower()
