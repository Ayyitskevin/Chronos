"""Contract tests for the source-driven Chronos configuration skill."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude/skills/chronos-config-and-flags/SKILL.md"
STALE_REFERENCE = SKILL.parent / "references/settings-reference.md"


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_config_skill_points_to_live_authorities() -> None:
    text = _skill_text()
    authorities = (
        "AGENTS.md",
        "docs/AGENT_PROTOCOL.md",
        "DECISIONS.md",
        "docs/adr/",
        "docs/safety.md",
        "docs/limitations.md",
        "RISK_REGISTER.md",
        "pyproject.toml",
        "src/chronos/config/settings.py",
        "src/chronos/config/limits.py",
        "src/chronos/risk/policy.py",
        "config/risk.example.yaml",
        ".env.example",
        "src/chronos/cli/main.py",
        "src/chronos/service/__main__.py",
        "src/chronos/histdata/__main__.py",
        "src/chronos/bridge/__main__.py",
        "src/chronos/bridge/config.py",
        "worker/__main__.py",
        "worker/config.py",
        "tests/unit/test_settings.py",
        "tests/safety/test_env_example_has_no_phantom_settings.py",
        "Makefile",
        ".github/workflows/ci.yml",
    )

    for authority in authorities:
        assert authority in text
        assert (ROOT / authority).exists()


def test_config_skill_derives_settings_and_direct_env_reads() -> None:
    text = _skill_text()
    required_fragments = (
        "Settings.model_fields",
        "RiskPolicy.model_fields",
        "validation_alias",
        "field.metadata",
        "NoDecode",
        "BeforeValidator",
        "model_config",
        "rg -n 'os\\.(environ|getenv)' src/chronos scripts worker",
        "Settings(_env_file=None)",
        "read sites",
    )

    for fragment in required_fragments:
        assert fragment in text

    assert "derive" in text.lower()
    assert "direct-read" in text.lower()
    assert "monkeypatch" in text
    assert "live_transmission_possible" in text


def test_config_skill_derives_each_cli_branch_from_help() -> None:
    text = _skill_text()
    required_fragments = (
        ".venv/bin/python -m chronos.cli --help",
        ".venv/bin/python -m chronos.service --help",
        ".venv/bin/python -m chronos.histdata --help",
        ".venv/bin/python -m chronos.bridge --help",
        "src/chronos/service/__main__.py",
        "src/chronos/histdata/__main__.py",
        "src/chronos/bridge/__main__.py",
        "src/chronos/bridge/config.py",
        "nested subcommand",
    )

    for fragment in required_fragments:
        assert fragment in text


def test_config_skill_distinguishes_configuration_surfaces_from_authority() -> None:
    text = _skill_text()
    lower = text.lower()
    categories = (
        "settings-backed environment",
        "direct-read environment",
        "risk policy",
        "cli argument",
        "hard limit",
        "runtime state",
    )

    for category in categories:
        assert category in lower

    assert "configuration is not authority" in lower
    assert "owner-gated" in lower
    assert "chronos-change-control" in text
    assert "chronos-run-and-operate" in text


def test_config_skill_protects_secrets_and_reports_evidence() -> None:
    text = _skill_text()
    lower = re.sub(r"\s+", " ", text.lower())

    for fragment in (
        "never read `.env`",
        "never print the process environment",
        "never instantiate `Settings` against the operator environment",
        "account identifiers",
        "Evidence commands:",
        "State owner:",
        "Current value source:",
        "Validators and coupled invariants:",
        "Consumers and restart semantics:",
        "Safety classification:",
    ):
        assert fragment.lower() in lower


def test_config_skill_uses_a_red_green_change_loop() -> None:
    text = _skill_text()
    lower = text.lower()

    required_fragments = (
        "tests/unit/test_settings.py",
        "tests/safety/test_env_example_has_no_phantom_settings.py",
        "git diff --",
        ".env.example config src/chronos/config",
        "src/chronos/service src/chronos/histdata src/chronos/bridge scripts worker tests",
        "make gates",
    )
    for fragment in required_fragments:
        assert fragment in text

    assert "red" in lower
    assert "green" in lower
    assert "fail-closed" in lower


def test_config_skill_routes_only_to_existing_skills() -> None:
    text = _skill_text()
    named_skills = set(re.findall(r"`(chronos-[a-z0-9-]+)`", text))

    assert "chronos-change-control" in named_skills
    for skill_name in named_skills:
        skill_path = ROOT / ".claude/skills" / skill_name / "SKILL.md"
        assert skill_path.is_file(), skill_name


def test_config_skill_cites_primary_documentation() -> None:
    text = _skill_text()
    primary_sources = (
        "https://docs.pydantic.dev/latest/concepts/pydantic_settings/#usage",
        "https://docs.pydantic.dev/latest/api/base_model/#pydantic.BaseModel.model_fields",
        "https://docs.python.org/3.12/library/argparse.html#sub-commands",
    )

    for source in primary_sources:
        assert source in text


def test_config_skill_does_not_cache_point_in_time_claims() -> None:
    text = _skill_text()
    forbidden_patterns = (
        r"\bfacts dated\b",
        r"\bverified (?:on|as of|against the repo)\s+20\d{2}",
        r"\bSCHEMA_VERSION\s*(?:=|\|)\s*\d+\b",
        r"\bschema(?: version)?\s*(?:is|=|v)\s*\d+\b",
        r"\balembic head\s+`?\d{4}\b",
        r"\b\d+\s+(?:settings fields|environment variables|cli flags|subcommands)\b",
        r"\b\d[\d,]*\s+(?:passed|skipped|warnings?|files?|revisions?)\b",
        r"(?:src|tests|scripts|docs|config)/[^`\s)]+\.(?:py|md|yaml):\d+",
        r"/home/(?:user|kevin-lee)/",
        r"\bcomplete configuration surface\b",
    )

    for pattern in forbidden_patterns:
        assert re.search(pattern, text, flags=re.IGNORECASE) is None, pattern

    assert not STALE_REFERENCE.exists()


def test_config_skill_description_has_trigger_and_differentiator() -> None:
    text = _skill_text()
    frontmatter = text.split("---\n", 2)[1]
    metadata = yaml.safe_load(frontmatter)

    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "chronos-config-and-flags"
    description = metadata["description"].lower()
    assert "use for" in description
    assert "differentiator" in description
    assert "chronos-run-and-operate" in description
    assert "chronos-change-control" in description
