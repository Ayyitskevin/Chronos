from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude" / "skills" / "chronos-debugging-playbook" / "SKILL.md"
HANDOFF = ROOT / "src" / "chronos" / "supervisor" / "handoff.py"
TERMINAL_ROUTES = ROOT / "src" / "chronos" / "api" / "routes" / "terminal.py"


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _string_enum_values(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            values: set[str] = set()
            for statement in node.body:
                if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                    continue
                target = statement.targets[0]
                if not isinstance(target, ast.Name):
                    continue
                try:
                    value = ast.literal_eval(statement.value)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, str):
                    values.add(value)
            return values
    raise AssertionError(f"{class_name} was not found in {path}")


def test_debugging_skill_points_to_live_authorities() -> None:
    text = _skill_text()
    required_paths = (
        "AGENTS.md",
        "docs/AGENT_PROTOCOL.md",
        "docs/VISION_COMPLETION_PLAN.md",
        "DECISIONS.md",
        "RISK_REGISTER.md",
        "src/chronos/orders/submission.py",
        "src/chronos/api/autonomy_wiring.py",
        "src/chronos/supervisor/handoff.py",
        "src/chronos/supervisor/loop.py",
        "src/chronos/api/reconciliation_loop.py",
        "src/chronos/api/routes/terminal.py",
        "src/chronos/persistence/database.py",
        "worker/",
        ".github/workflows/ci.yml",
    )

    for path in required_paths:
        assert path in text, f"debugging skill does not point to {path}"
        assert (ROOT / path).exists(), f"debugging skill points to missing source {path}"
    assert "chronos-diagnostics" in text


def test_debugging_skill_derives_volatile_state() -> None:
    text = _skill_text()
    required_probes = (
        "rg -n",
        "/openapi.json",
        "PYTHONPATH=src .venv/bin/alembic heads",
        ".venv/bin/python -m pytest -q",
        "make gates",
    )
    official_sources = (
        "https://fastapi.tiangolo.com/tutorial/metadata/#openapi-url",
        "https://alembic.sqlalchemy.org/en/latest/api/commands.html#alembic.command.heads",
    )

    for probe in (*required_probes, *official_sources):
        assert probe in text, f"debugging skill omits live derivation source {probe}"


def test_debugging_skill_tracks_typed_handoff_outcomes() -> None:
    text = _skill_text()
    dispositions = _string_enum_values(HANDOFF, "HandoffDisposition")

    assert dispositions, "handoff disposition discovery found no values"
    for disposition in dispositions:
        assert disposition in text, f"debugging skill omits handoff outcome {disposition}"
    assert "COMPLETE means a confirmed working, partially filled, or filled order" in text


def test_debugging_skill_tracks_terminal_authority_removal_routes() -> None:
    text = _skill_text()
    source = TERMINAL_ROUTES.read_text(encoding="utf-8")
    routes = {
        route
        for route in re.findall(r'@router\.post\("([^"]+)"', source)
        if route.startswith("/terminal/live/")
    }

    assert routes, "terminal authority-removal route discovery found no routes"
    for route in routes:
        assert f"POST {route}" in text, f"debugging skill omits terminal route {route}"


def test_debugging_skill_records_current_periodic_and_worker_shapes() -> None:
    text = _skill_text()

    assert "Periodic reconciliation exists" in text
    assert "The model worker exists" in text
    assert "python -m worker" in text
    assert "ships inert" in text


def test_debugging_skill_keeps_diagnosis_read_only() -> None:
    text = _skill_text()

    assert "Diagnosis is read-only" in text
    assert re.search(r"curl[^\n]*-X\s+POST", text) is None
    assert "alembic upgrade" not in text
    assert "make migrate" not in text


def test_debugging_skill_does_not_cache_point_in_time_claims() -> None:
    text = _skill_text()
    forbidden_patterns = (
        r"\bcompiled 20\d{2}-\d{2}-\d{2}\b",
        r"\bfacts dated 20\d{2}-\d{2}-\d{2}\b",
        r"\bverified against\s+`?[0-9a-f]{7,40}\b",
        r"\bSCHEMA_VERSION\s*=\s*\d+\b",
        r"\bschema v\d+\b",
        r"\balembic head\s+`?\d{4}\b",
        r"\b\d[\d,]*\s+(?:passed|skipped|collected)\b",
        r"(?:src|tests|worker)/[^`\s]+\.py:\d+",
        r"http://127\.0\.0\.1:8765",
        r"\bmissing periodic re-arm",
        r"\bno model worker exists",
        r"\bSTILL OPEN[^\n]*COMPLETE-on-refusal",
        r"\bNo arm/kill buttons",
        r"\bexactly ONE marker",
    )

    for pattern in forbidden_patterns:
        assert re.search(pattern, text, flags=re.IGNORECASE) is None, (
            f"debugging skill caches a stale claim matching {pattern!r}"
        )
