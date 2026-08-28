"""Contract tests for the source-driven Chronos build/environment skill."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude/skills/chronos-build-and-env/SKILL.md"


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_build_env_skill_points_to_live_authorities() -> None:
    text = _skill_text()
    authorities = (
        "AGENTS.md",
        "docs/AGENT_PROTOCOL.md",
        "pyproject.toml",
        "requirements-build.in",
        "requirements-build.lock",
        "requirements-dev.lock",
        "Makefile",
        ".github/workflows/ci.yml",
        "docs/DEPLOYMENT.md",
        "docs/SECURITY.md",
        "docs/limitations.md",
        "scripts/initialize_database.py",
        "scripts/verify_release_artifact.py",
        "src/chronos/persistence/database.py",
        "src/chronos/persistence/migrations/env.py",
        "src/chronos/persistence/migrations/versions/",
        "alembic.ini",
    )

    for authority in authorities:
        assert authority in text
        assert (ROOT / authority).exists()


def test_build_env_skill_derives_interpreter_install_and_gates() -> None:
    text = _skill_text()

    required_fragments = (
        "requires-python",
        "<python-that-satisfies-the-repo> -m venv .venv",
        ".venv/bin/python -m pip install --require-hashes -r requirements-build.lock",
        ".venv/bin/python -m pip install --require-hashes -r requirements-dev.lock",
        (
            ".venv/bin/python -m pip install -e . --no-deps "
            "--no-build-isolation --check-build-dependencies"
        ),
        "sed -n '/^gates:/p' Makefile",
        "make gates",
    )
    for fragment in required_fragments:
        assert fragment in text

    assert "derive" in text.lower()
    assert "workflow" in text.lower()


def test_build_env_skill_derives_lock_maintenance_process() -> None:
    text = _skill_text()

    required_fragments = (
        "sed -n '1,2p' requirements-build.lock requirements-dev.lock",
        "existing output file",
        (
            "git diff -- pyproject.toml requirements-build.in "
            "requirements-build.lock requirements-dev.lock"
        ),
        "owner review",
        "https://docs.astral.sh/uv/pip/compile/",
        "https://pip.pypa.io/en/stable/topics/secure-installs/",
    )
    for fragment in required_fragments:
        assert fragment in text

    assert "--upgrade" in text


def test_build_env_skill_separates_fresh_initialization_from_upgrade() -> None:
    text = _skill_text()
    lower = text.lower()

    required_fragments = (
        "scripts/initialize_database.py",
        "src/chronos/persistence/database.py",
        "alembic heads",
        "DATABASE_URL",
    )
    for fragment in required_fragments:
        assert fragment in text

    assert "fresh database" in lower
    assert "existing database" in lower
    assert "back up" in lower
    assert "explicit" in lower
    assert "follow `chronos-run-and-operate`" in text
    assert "the current upgrade command from `docs/DEPLOYMENT.md`" not in text


def test_build_env_skill_routes_only_to_existing_skills() -> None:
    text = _skill_text()
    named_skills = set(re.findall(r"`(chronos-[a-z0-9-]+)`", text))

    assert "chronos-change-control" in named_skills
    for skill_name in named_skills:
        skill_path = ROOT / ".claude/skills" / skill_name / "SKILL.md"
        assert skill_path.is_file(), skill_name


def test_build_env_skill_preserves_release_artifact_boundary() -> None:
    text = _skill_text()
    lower = text.lower()

    required_fragments = (
        "make release-gate",
        "scripts/verify_release_artifact.py",
        "git ls-files --cached --others --exclude-standard",
        "src/chronos/terminal/static/",
        "src/chronos/persistence/migrations/",
    )
    for fragment in required_fragments:
        assert fragment in text

    assert "editable install" in lower
    assert "installed wheel" in lower
    assert "entry point" in lower


def test_build_env_skill_cites_primary_documentation() -> None:
    text = _skill_text()

    primary_sources = (
        "https://docs.python.org/3.12/library/venv.html",
        "https://packaging.python.org/en/latest/specifications/pyproject-toml/",
        "https://setuptools.pypa.io/en/stable/userguide/datafiles.html#package-data",
        "https://alembic.sqlalchemy.org/en/latest/api/commands.html",
    )
    for source in primary_sources:
        assert source in text


def test_build_env_skill_covers_project_specific_failure_boundaries() -> None:
    text = _skill_text()
    lower = text.lower()

    for fragment in ("known pitfalls", "ibapi", "worker/", "safe CI environment"):
        assert fragment.lower() in lower

    assert "do not weaken" in lower
    assert "docs/limitations.md" in text


def test_build_env_skill_does_not_cache_point_in_time_claims() -> None:
    text = _skill_text()
    forbidden_patterns = (
        r"\bSCHEMA_VERSION\s*=\s*\d+\b",
        r"\bschema(?: version)?\s*(?:is|=|v)\s*\d+\b",
        r"\bhead\s+00\d+\b",
        r"\b(?:four|five|six|seven)\s+(?:CI\s+)?gates?\b",
        r"\b\d[\d,]*\s+(?:passed|skipped|warnings?|files?|modules?|revisions?)\b",
        r"\b(?:pytest|ruff|mypy|alembic|fastapi|sqlalchemy)==\d",
        r"actions/(?:checkout|setup-python)@v\d+",
        r"/home/(?:user|kevin-lee)/",
        r"\bpython3\.12\b",
        r"verified\s+(?:on|as of)\s+20\d{2}",
    )

    for pattern in forbidden_patterns:
        assert re.search(pattern, text, flags=re.IGNORECASE) is None, pattern


def test_build_env_skill_description_has_trigger_and_differentiator() -> None:
    text = _skill_text()
    frontmatter = text.split("---\n", 2)[1]
    metadata = yaml.safe_load(frontmatter)

    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "chronos-build-and-env"
    description = metadata["description"].lower()
    assert "use for" in description
    assert "differentiator" in description
    assert "chronos-run-and-operate" in description
    assert "chronos-validation-and-qa" in description
