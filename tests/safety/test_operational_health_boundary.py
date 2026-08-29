from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = (
    REPO_ROOT / "src" / "chronos" / "control",
    REPO_ROOT / "src" / "chronos" / "execution",
    REPO_ROOT / "src" / "chronos" / "orders",
    REPO_ROOT / "src" / "chronos" / "portfolio",
    REPO_ROOT / "src" / "chronos" / "supervisor",
    REPO_ROOT / "src" / "chronos" / "risk",
    REPO_ROOT / "src" / "chronos" / "broker",
    REPO_ROOT / "src" / "chronos" / "services",
    REPO_ROOT / "src" / "chronos" / "runtime.py",
)


def _imports_health(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.startswith("chronos.operations.health") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "chronos.operations.health"
        ):
            return True
    return False


def test_operational_health_cannot_become_an_authority_dependency() -> None:
    offenders: list[str] = []
    for target in FORBIDDEN:
        paths = (target,) if target.is_file() else tuple(target.rglob("*.py"))
        offenders.extend(
            str(path.relative_to(REPO_ROOT)) for path in paths if _imports_health(path)
        )

    assert offenders == []
