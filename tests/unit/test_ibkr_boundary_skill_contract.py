"""Contract tests for the source-driven Chronos IBKR boundary skill."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude/skills/chronos-ibkr-boundary/SKILL.md"
WHEEL_SKILL = ROOT / ".claude/skills/chronos-wheel-and-options/SKILL.md"


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_ibkr_boundary_skill_points_to_live_authorities() -> None:
    text = _skill_text()
    authorities = (
        "AGENTS.md",
        "docs/AGENT_PROTOCOL.md",
        "DECISIONS.md",
        "RISK_REGISTER.md",
        "docs/safety.md",
        "docs/limitations.md",
        "docs/ibkr_setup.md",
        "docs/adr/ADR-0009-live-submission-branch.md",
        "docs/adr/ADR-0011-historical-data-plane.md",
        "docs/adr/ADR-0019-historical-bars-and-the-chart-panel.md",
        "docs/adr/ADR-0030-deterministic-option-selection-and-evidence-receipts.md",
        "pyproject.toml",
        "requirements-dev.lock",
        "src/chronos/broker/base.py",
        "src/chronos/broker/official_ibkr.py",
        "src/chronos/broker/ibkr.py",
        "src/chronos/broker/demo.py",
        "src/chronos/broker/connection.py",
        "src/chronos/broker/market_data.py",
        "src/chronos/config/settings.py",
        "src/chronos/runtime.py",
        "src/chronos/marketdata/pacing.py",
        "src/chronos/api/bars.py",
        "src/chronos/histdata/backfill.py",
        "src/chronos/histdata/official_client.py",
        "src/chronos/services/liquid_hours.py",
        "src/chronos/services/option_deliverable.py",
        "scripts/smoke_test_ibkr.py",
        "tests/integration/test_ibkr_smoke.py",
        "tests/safety/test_broker_mutation_inventory.py",
        "tests/safety/test_single_transmit_site.py",
    )

    for authority in authorities:
        assert authority in text, authority
        assert (ROOT / authority).exists(), authority


def test_ibkr_boundary_skill_derives_field_ownership_and_adapter_parity() -> None:
    text = _skill_text()
    required_fragments = (
        "details.contract",
        "ContractDetails",
        "marketRuleIds",
        "validExchanges",
        "reqMarketRule",
        "src/chronos/broker/official_ibkr.py",
        "src/chronos/broker/ibkr.py",
        "src/chronos/broker/demo.py",
        "Broker(Protocol)",
        "rg -n",
        "derive",
        "cross-adapter",
    )

    for fragment in required_fragments:
        assert fragment.lower() in text.lower(), fragment

    assert "read NOWHERE" not in text


def test_ibkr_boundary_skill_requires_exercised_fail_closed_proof() -> None:
    text = _skill_text().lower()
    required_fragments = (
        "red",
        "green",
        "revert each conjunct",
        "distinct test",
        "by fiat",
        "realistic adapter payload",
        "missing",
        "ambiguous",
        "partial",
        "unknown",
        "fail closed",
        "mitigated ≠ closed",
    )

    for fragment in required_fragments:
        assert fragment in text, fragment


def test_ibkr_boundary_skill_separates_pacing_postures_and_timing() -> None:
    text = _skill_text().lower()

    assert "backend bar provider never sleeps for historical-data pacing" in text

    for fragment in (
        "record the budget before",
        "histdata",
        "sleep",
        "backend",
        "cache",
        "never sleeps",
        "client id",
        "cross-process",
        "owner-gated",
    ):
        assert fragment in text, fragment


def test_ibkr_boundary_skill_keeps_gateway_mutation_owner_gated() -> None:
    text = _skill_text()
    lower = text.lower()

    for fragment in (
        "chronos-real-gateway-campaign",
        "owner action",
        "opt-in",
        "read-only",
        "preview_order",
        "submit_order",
        "modify_order",
        "cancel_order",
        "make gates",
    ):
        assert fragment.lower() in lower, fragment

    assert "nothing in this skill authorizes" in lower


def test_ibkr_boundary_skill_reports_source_derived_evidence() -> None:
    text = _skill_text()
    normalized = re.sub(r"\s+", " ", text)

    for fragment in (
        "Field owner:",
        "Adapter paths:",
        "Downstream consumer:",
        "Fail-closed outcome:",
        "Evidence commands:",
        "Gateway status:",
        "Owner gate:",
    ):
        assert fragment in normalized, fragment


def test_ibkr_boundary_skill_cites_primary_ibkr_documentation() -> None:
    text = _skill_text()
    primary_sources = (
        "https://ibkrcampus.com/campus/trading-lessons/defining-contracts-in-the-tws-api/",
        "https://interactivebrokers.github.io/tws-api/classIBApi_1_1ContractDetails.html",
        "https://interactivebrokers.github.io/tws-api/minimum_increment.html",
        "https://interactivebrokers.github.io/tws-api/historical_limitations.html",
        "https://interactivebrokers.github.io/tws-api/order_submission.html",
    )

    for source in primary_sources:
        assert source in text, source


def test_ibkr_boundary_skill_routes_only_to_existing_skills() -> None:
    text = _skill_text()
    named_skills = set(re.findall(r"`(chronos-[a-z0-9-]+)`", text))

    assert "chronos-real-gateway-campaign" in named_skills
    for skill_name in named_skills:
        assert (ROOT / ".claude/skills" / skill_name / "SKILL.md").is_file(), skill_name


def test_ibkr_boundary_skill_does_not_cache_point_in_time_claims() -> None:
    text = _skill_text()
    forbidden_patterns = (
        r"\bdate-stamped\b",
        r"\bas of 20\d{2}-\d{2}-\d{2}\b",
        r"\bverified (?:on|against)\s+20\d{2}",
        r"\bcontent verified against\b",
        r"\bcommit\s+`?[0-9a-f]{7,40}`?",
        r"(?:src|tests|scripts|docs)/[^`\s)]+\.(?:py|md):\d+",
        r"\b\d+\s+(?:sites|instances|tests|collected|passed|skipped|warnings?)\b",
        r"\bdefault\s+\d+\b",
        r"\{\d+(?:,\s*\d+)+\}",
        r"\bthe (?:fourth|fifth) instance\b",
        r"\bread nowhere\b",
        r"/home/(?:user|kevin-lee)/",
    )

    for pattern in forbidden_patterns:
        assert re.search(pattern, text, flags=re.IGNORECASE) is None, pattern


def test_ibkr_boundary_skill_description_has_trigger_and_differentiator() -> None:
    text = _skill_text()
    frontmatter = text.split("---\n", 2)[1]
    metadata = yaml.safe_load(frontmatter)

    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "chronos-ibkr-boundary"
    description = metadata["description"].lower()
    assert "use for" in description
    assert "differentiator" in description
    assert "chronos-real-gateway-campaign" in description
    assert "chronos-wheel-and-options" in description


def test_wheel_skill_points_to_the_source_derived_boundary_procedure() -> None:
    text = WHEEL_SKILL.read_text(encoding="utf-8")

    assert "Contract-vs-ContractDetails source-derivation procedure" in text
    assert "full touchpoint map" not in text
    assert "src/chronos/broker/official_ibkr.py:231-269" not in text
    assert "src/chronos/broker/ibkr.py:849, 860-861" not in text
