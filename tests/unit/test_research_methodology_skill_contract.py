"""Contract tests for the source-driven Chronos research-methodology skill."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude/skills/chronos-research-methodology/SKILL.md"


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_methodology_points_to_live_authorities_and_exercising_tests() -> None:
    text = _skill_text()
    authorities = (
        "AGENTS.md",
        "docs/AGENT_PROTOCOL.md",
        "docs/VISION_COMPLETION_PLAN.md",
        "DECISIONS.md",
        "docs/adr/ADR-0013-experiment-registry-holdout-guardian.md",
        "docs/adr/ADR-0014-walkforward-and-statistics.md",
        "docs/adr/ADR-0015-revalidation-campaign.md",
        "research/selection_manifest.json",
        "research/data/raw/MANIFEST.json",
        "research/data/history/HOLDOUTS.json",
        "docs/RESEARCH_REPORT.md",
        "docs/STRATEGY_SELECTION.md",
        "RISK_REGISTER.md",
        "docs/limitations.md",
        "src/chronos/research/stats.py",
        "src/chronos/research/walkforward.py",
        "src/chronos/research/campaign.py",
        "src/chronos/research/repro.py",
        "src/chronos/research/certification.py",
        "src/chronos/research/certified_data.py",
        "src/chronos/research/dataset_release.py",
        "src/chronos/research/trial_runner.py",
        "src/chronos/research/five_tool_trials.py",
        "src/chronos/registry/runs.py",
        "src/chronos/registry/trials.py",
        "src/chronos/registry/holdout_guardian.py",
        "tests/unit/test_research_stats.py",
        "tests/unit/test_walkforward.py",
        "tests/unit/test_campaign.py",
        "tests/unit/test_research_repro.py",
        "tests/unit/test_registry_trials.py",
        "tests/unit/test_research_trial_runner.py",
        "tests/safety/test_research_isolation.py",
    )

    for authority in authorities:
        assert authority in text
        assert (ROOT / authority).exists(), authority


def test_methodology_derives_revision_inventory_and_current_state() -> None:
    text = _skill_text()
    commands = (
        "git fetch origin --prune",
        "git status --short --branch",
        "git rev-parse HEAD",
        "git ls-remote --symref origin HEAD",
        "git log --oneline --branches --tags --not --remotes",
        "git ls-files -- 'research/**' 'specs/**'",
        "rg -n '^Status:' docs/adr",
        ".venv/bin/python -m json.tool",
        ".venv/bin/python scripts/build_current_state.py --check",
    )

    for command in commands:
        assert command in text

    lower = text.lower()
    assert "checked-out revision" in lower
    assert "derive" in lower
    assert "current state" in lower


def test_methodology_classifies_effects_before_touching_data() -> None:
    text = _skill_text().lower()
    effect_classes = (
        "inspect-only interpretation",
        "local replay probe",
        "registered legacy run",
        "canonical brokered trial",
        "certification or release",
        "holdout consumption",
    )

    for effect_class in effect_classes:
        assert effect_class in text

    assert "no-order is not no-mutation" in text
    assert "state effects" in text
    assert "classify the requested action" in text


def test_methodology_uses_an_evidence_ladder_without_collapsing_claims() -> None:
    text = _skill_text().lower()
    ladder = (
        "accepted design",
        "implemented capability",
        "authenticated input identity",
        "registered data touch",
        "retained replay evidence",
        "statistical verdict",
        "promotion artifact",
    )

    for rung in ladder:
        assert rung in text

    assert "mechanism is not evidence" in text
    assert "strongest supported rung" in text
    assert "do not promote a claim" in text


def test_methodology_traces_distinct_trial_lifecycles_and_counts() -> None:
    text = _skill_text()
    required = (
        "experiment_run",
        "trial_started",
        "trial_terminal",
        "register_run",
        "CanonicalTrialRegistry",
        "FiveToolTrialBroker",
        "multiplicity_snapshot",
        "registered_trial_count",
        "ledger-local",
        "before bytes",
        "after the verdict",
    )

    for fragment in required:
        assert fragment in text

    lower = text.lower()
    assert "trace both trial lifecycles" in lower
    assert "canonical multiplicity" in lower
    assert "never substitute" in lower


def test_methodology_preserves_freezes_contamination_and_owner_gates() -> None:
    text = _skill_text().lower()
    required = (
        "freeze before observation",
        "failed holdout rejects",
        "seen or contaminated",
        "criteria change is a separate",
        "owner-gated",
        "holdout unlock",
        "never hand-edit",
        "no_trade",
        "insufficient_evidence",
    )

    for fragment in required:
        assert fragment in text

    assert "absence from one ledger" in text
    assert "does not make data clean" in text


def test_methodology_separates_reproducibility_from_validity() -> None:
    text = _skill_text().lower()

    assert "reproducibility is not validity" in text
    assert "deterministic replay can reproduce" in text
    assert "does not prove edge" in text
    assert "does not register a new trial" in text
    assert "selection-relevant" in text


def test_methodology_requires_a_research_evidence_packet() -> None:
    text = _skill_text()
    fields = (
        "question:",
        "revision:",
        "claim_rung:",
        "criteria_identity:",
        "hypothesis_identity:",
        "dataset_identity:",
        "partition_status:",
        "certification:",
        "code_config_identity:",
        "cost_model:",
        "trial_lifecycle:",
        "multiplicity_snapshot:",
        "sample_units:",
        "statistics:",
        "robustness:",
        "holdout_status:",
        "replay_status:",
        "verdict:",
        "promotion_status:",
        "owner_gate:",
        "residuals:",
        "verification:",
    )

    for field in fields:
        assert field in text

    assert "one falsifiable question" in text.lower()
    assert "missing" in text.lower()
    assert "contradictory" in text.lower()


def test_methodology_makes_execution_explicit_and_fail_closed() -> None:
    text = _skill_text()
    commands = (
        ".venv/bin/python -m chronos.cli research --help",
        ".venv/bin/python -m chronos.cli registry verify",
        ".venv/bin/python -m chronos.cli registry stats",
        ".venv/bin/python -m chronos.cli holdout status",
    )

    for command in commands:
        assert command in text

    lower = text.lower()
    assert "explicit authorization" in lower
    assert "declared state effects" in lower
    assert "disposable output directory" in lower
    assert "never execute `holdout unlock`" in lower
    assert "tests are the smoke path" in lower


def test_methodology_routes_only_to_existing_skills() -> None:
    text = _skill_text()
    named_skills = set(re.findall(r"`(chronos-[a-z0-9-]+)`", text))

    assert "chronos-change-control" in named_skills
    assert "chronos-validation-and-qa" in named_skills
    assert "chronos-docs-map" in named_skills
    assert "chronos-priorities-and-roadmap" in named_skills
    for skill_name in named_skills:
        assert (ROOT / ".claude/skills" / skill_name / "SKILL.md").is_file(), skill_name


def test_methodology_does_not_cache_point_in_time_state() -> None:
    text = _skill_text()
    forbidden_patterns = (
        r"\b20\d{2}-\d{2}-\d{2}\b",
        r"\b(?:commit|verified against)\s+`?[0-9a-f]{7,40}\b",
        r"(?:\.py|\.md|\.json):\d+",
        r"\b\d[\d,]*(?:\.\d+)?\s+(?:trades|tests|records|rows|symbols|sessions|windows)\b",
        r"\b(?:DSR|PBO)\s*(?:>=|<=|≥|≤)\s*\d",
        r"/home/(?:user|kevin-lee)/",
    )
    forbidden_claims = (
        "verified against the repo at commit",
        "current state 2026",
        "the ledger ships empty",
        "no dataset has been certified",
        "no campaign output committed",
        "zero selected candidates is the current",
        "strongest cell in the repo",
    )

    for pattern in forbidden_patterns:
        assert re.search(pattern, text, flags=re.IGNORECASE) is None, pattern
    for claim in forbidden_claims:
        assert claim not in text.lower()

    assert "grep -" not in text


def test_methodology_has_valid_routing_and_fresh_closeout() -> None:
    text = _skill_text()
    frontmatter = text.split("---\n", 2)[1]
    metadata = yaml.safe_load(frontmatter)

    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "chronos-research-methodology"
    description = metadata["description"].lower()
    for fragment in (
        "use for",
        "differentiator",
        "research",
        "backtest",
        "holdout",
        "trial",
        "checked-out revision",
    ):
        assert fragment in description

    assert (
        ".venv/bin/python -m pytest -q tests/unit/test_research_methodology_skill_contract.py"
        in text
    )
    assert ".venv/bin/python scripts/build_current_state.py --check" in text
    assert "git diff --check" in text
    assert "make gates" in text
    assert "non-author review" in text.lower()
    assert "changed-path" in text.lower()
