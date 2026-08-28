"""Contract tests for the source-driven Chronos Wheel/options skill."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude/skills/chronos-wheel-and-options/SKILL.md"


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_wheel_skill_points_to_live_authorities() -> None:
    text = _skill_text()
    authorities = (
        "AGENTS.md",
        "docs/AGENT_PROTOCOL.md",
        "DECISIONS.md",
        "RISK_REGISTER.md",
        "docs/safety.md",
        "docs/limitations.md",
        "docs/adr/ADR-0009-live-submission-branch.md",
        "docs/adr/ADR-0012-options-forward-capture.md",
        "docs/adr/ADR-0016-controlled-autonomous-model-authority.md",
        "docs/adr/ADR-0021-inert-economic-fields-on-the-decision-contract.md",
        "docs/adr/ADR-0030-deterministic-option-selection-and-evidence-receipts.md",
        "src/chronos/domain/enums.py",
        "src/chronos/domain/models.py",
        "src/chronos/orders/intent.py",
        "src/chronos/strategy/wheel_state.py",
        "src/chronos/services/reconciliation.py",
        "src/chronos/services/option_deliverable.py",
        "src/chronos/broker/base.py",
        "src/chronos/broker/official_ibkr.py",
        "src/chronos/broker/ibkr.py",
        "src/chronos/strategy/strike_resolver.py",
        "src/chronos/supervisor/option_selection.py",
        "src/chronos/api/option_selection.py",
        "src/chronos/api/autonomy_wiring.py",
        "src/chronos/supervisor/loop.py",
        "src/chronos/strategy/capital.py",
        "src/chronos/strategy/reservations.py",
        "src/chronos/orders/risk.py",
        "src/chronos/orders/submission.py",
        "src/chronos/config/settings.py",
        "src/chronos/strategy/basis.py",
        "src/chronos/persistence/schema.py",
        "src/chronos/persistence/repositories.py",
        "src/chronos/orders/state_machine.py",
        "src/chronos/orders/tracker.py",
        "src/chronos/orders/reconciliation_recovery.py",
        "src/chronos/api/reconciliation_loop.py",
        "src/chronos/runtime.py",
        "src/chronos/histdata/options_capture.py",
        "src/chronos/histdata/options_store.py",
    )

    for authority in authorities:
        assert authority in text, authority
        assert (ROOT / authority).exists(), authority


def test_wheel_skill_separates_state_owners_and_traces_callers() -> None:
    text = _skill_text()
    lower = re.sub(r"\s+", " ", text.lower())

    for fragment in (
        "wheel strategy state",
        "order lifecycle",
        "autonomous option selection",
        "derive_wheel_state",
        "assess_standard_deliverable",
        "assess_assignment_pressure",
        "project_strategy_basis",
        "production caller",
        "downstream consumer",
        "four invariables",
    ):
        assert fragment in lower, fragment

    assert "a selected contract is not a position" in lower
    assert "wheel stage is not permission to" in lower


def test_wheel_skill_requires_field_ownership_and_adapter_parity() -> None:
    text = _skill_text()
    lower = text.lower()

    for fragment in (
        "chronos-ibkr-boundary",
        "contractdetails",
        "both production adapters",
        "standard-deliverable detector",
        "authoritative adjustment schedule",
        "market-rule schedule",
        "realistic failing case",
        "adapter-shaped payload",
        "demo-only",
    ):
        assert fragment in lower, fragment


def test_wheel_skill_traces_admission_capital_and_persistence() -> None:
    text = re.sub(r"\s+", " ", _skill_text().lower())

    for fragment in (
        "vocabulary and mandate",
        "canonical selected or no-trade receipt",
        "risk evaluates",
        "submission rechecks",
        "tracker and reconciliation",
        "gross assignment obligation",
        "cash reservations",
        "settled shares",
        "trace fills and commissions",
        "schema's existence is not proof",
        "estimated and actual commission",
    ):
        assert fragment in text, fragment


def test_wheel_skill_keeps_ambiguity_and_gateway_actions_fail_closed() -> None:
    text = _skill_text().lower()

    for fragment in (
        "manual_review",
        "fail-closed result",
        "never bypass",
        "mitigated remains distinct from closed",
        "owner-gated real-gateway campaign",
        "nothing in this skill authorizes",
        "order preview or submission",
        "owner-gated safety/authority change",
        "fail closed without erasing semantics",
    ):
        assert fragment in text, fragment


def test_wheel_skill_distinguishes_operations_from_research() -> None:
    text = _skill_text().lower()

    for fragment in (
        "option chain used for a current decision is not an options-history corpus",
        "no option-path unit test",
        "proves profitability",
        "chronos-research-methodology",
        "promotion artifact authorizes only its exact",
        "assignment_pressure.py",
        "tested helper with no consumer is dormant",
    ):
        assert fragment in text, fragment


def test_wheel_skill_requires_exercised_red_green_proof() -> None:
    text = re.sub(r"\s+", " ", _skill_text().lower())

    for fragment in (
        "red-green prevention loop",
        "revert each conjunct",
        "distinct test failure",
        (
            "exercise good, missing, malformed, duplicate, stale, future, partial, "
            "conflicting, wrong-account, and restart forms"
        ),
        "make gates",
    ):
        assert fragment in text, fragment


def test_wheel_skill_discovery_symbols_exist_in_live_sources() -> None:
    expected = {
        "src/chronos/domain/enums.py": {"WheelStage", "OptionRight", "OrderIntent"},
        "src/chronos/domain/models.py": {"OptionContract", "BrokerOrder", "BrokerExecution"},
        "src/chronos/strategy/wheel_state.py": {"derive_wheel_state"},
        "src/chronos/services/option_deliverable.py": {"assess_standard_deliverable"},
        "src/chronos/strategy/assignment_pressure.py": {"assess_assignment_pressure"},
        "src/chronos/strategy/basis.py": {"project_strategy_basis"},
        "src/chronos/orders/risk.py": {"OrderRiskEngine"},
        "src/chronos/strategy/strike_resolver.py": {"StrikeResolver"},
        "src/chronos/api/option_selection.py": {"AutonomousOptionSelectionService"},
    }

    for relative_path, required_symbols in expected.items():
        module = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
        declared = {
            node.name
            for node in ast.walk(module)
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert required_symbols <= declared, relative_path


def test_wheel_skill_reports_source_derived_evidence() -> None:
    text = _skill_text()
    normalized = re.sub(r"\s+", " ", text)

    for fragment in (
        "Commit and mode:",
        "Decision owner:",
        "Broker and domain identity:",
        "State and timing:",
        "Capital and allocation:",
        "First failing layer:",
        "Downstream effect:",
        "Evidence commands:",
        "Gateway status:",
        "Owner gate:",
        "Unresolved:",
    ):
        assert fragment in normalized, fragment


def test_wheel_skill_cites_primary_option_sources() -> None:
    text = _skill_text()
    primary_sources = (
        "https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document",
        "https://infomemo.theocc.com/infomemo/search",
        "https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-doc/",
        "https://ibkrcampus.com/campus/trading-lessons/defining-contracts-in-the-tws-api/",
    )

    for source in primary_sources:
        assert source in text, source


def test_wheel_skill_routes_only_to_existing_skills() -> None:
    text = _skill_text()
    named_skills = set(re.findall(r"`(chronos-[a-z0-9-]+)`", text))

    required = {
        "chronos-ibkr-boundary",
        "chronos-research-methodology",
        "chronos-config-and-flags",
        "chronos-change-control",
        "chronos-real-gateway-campaign",
    }
    assert required <= named_skills
    for skill_name in named_skills:
        assert (ROOT / ".claude/skills" / skill_name / "SKILL.md").is_file(), skill_name


def test_wheel_skill_does_not_cache_point_in_time_claims() -> None:
    text = _skill_text()
    forbidden_patterns = (
        r"\bdate-stamped\b",
        r"\bas of 20\d{2}-\d{2}-\d{2}\b",
        r"\bverified (?:on|against|as of)\s+20\d{2}",
        r"\bcontent verified against\b",
        r"\bcommit\s+`?[0-9a-f]{7,40}`?",
        r"(?:src|tests|scripts|docs)/[^`\s)]+\.(?:py|md):\d+",
        r"\b\d[\d,]*\s+(?:stages|tests|test functions|passed|skipped|warnings?)\b",
        r"\bdefault\s+(?:is\s+)?(?:\d+(?:\.\d+)?|USD\s+\d+)\b",
        r"\b(?:USD\s*)?\d+(?:\.\d+)?\s+account\b",
        r"/home/(?:user|kevin-lee)/",
        r"\bproduction-unreachable today\b",
        r"\bzero production callers\b",
    )

    for pattern in forbidden_patterns:
        assert re.search(pattern, text, flags=re.IGNORECASE) is None, pattern


def test_wheel_skill_description_has_trigger_and_differentiator() -> None:
    text = _skill_text()
    frontmatter = text.split("---\n", 2)[1]
    metadata = yaml.safe_load(frontmatter)

    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "chronos-wheel-and-options"
    description = metadata["description"].lower()
    assert "use for" in description
    assert "differentiator" in description
    assert "chronos-ibkr-boundary" in description
    assert "chronos-research-methodology" in description
