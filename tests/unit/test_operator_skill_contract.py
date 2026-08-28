from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude" / "skills" / "chronos-run-and-operate" / "SKILL.md"
TERMINAL_ROUTES = ROOT / "src" / "chronos" / "api" / "routes" / "terminal.py"


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_operator_skill_points_to_live_authorities() -> None:
    text = _skill_text()
    required_sources = (
        "Makefile",
        "docs/DEPLOYMENT.md",
        "docs/INCIDENT_RESPONSE.md",
        "docs/BACKUP_AND_RECOVERY.md",
        "docs/live_trading_runbook.md",
        "docs/IBKR_RUNBOOK.md",
        "scripts/run_backend.py",
        "src/chronos/api/reconciliation_loop.py",
        "src/chronos/persistence/database.py",
        "alembic heads",
        "make gates",
    )

    for source in required_sources:
        assert source in text, f"operator skill does not point to {source}"


def test_operator_skill_covers_every_terminal_mutation() -> None:
    text = _skill_text()
    route_source = TERMINAL_ROUTES.read_text(encoding="utf-8")
    post_routes = set(re.findall(r'@router\.post\("([^"]+)"', route_source))

    assert post_routes, "terminal route discovery found no mutating routes"
    for route in post_routes:
        assert route in text, f"operator skill omits terminal mutation {route}"


def test_operator_skill_does_not_cache_point_in_time_claims() -> None:
    text = _skill_text()
    forbidden_patterns = (
        r"\bfacts dated\b",
        r"\bcompiled 20\d{2}-\d{2}-\d{2}\b",
        r"\bschema v\d+\b",
        r"\bSCHEMA_VERSION\s*=\s*\d+\b",
        r"\balembic head\s+`?\d{4}\b",
        r"\bthere is NO periodic loop\b",
        r"\bknown doc defect\b",
        r"\bfile table OMITS\b",
        r"\bthe two writes \(acknowledge, revoke\)\b",
        r"FUTURE WORK — no such entry point exists",
        r"\b\d[\d,]*\s+(?:passed|skipped|collected)\b",
    )

    for pattern in forbidden_patterns:
        assert re.search(pattern, text, flags=re.IGNORECASE) is None, (
            f"operator skill caches a stale claim matching {pattern!r}"
        )
