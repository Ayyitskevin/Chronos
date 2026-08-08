"""A holdout is unmasked from exactly one site — the guardian (ADR-0013 §3).

The private selective helper preserves every mask except the exact durable grant's
window.  It must have one production caller, in the guardian.  The old public
``embargoed_view(..., unlocked=True)`` escape hatch must have no production callers.
This is an accidental-wiring guard; reflective/dynamic bypasses remain out of scope.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src" / "chronos"


def _call_name(node: ast.Call) -> str | None:
    return getattr(node.func, "attr", None) or getattr(node.func, "id", None)


def _selective_unmask_call_sites() -> list[tuple[str, int]]:
    sites: list[tuple[str, int]] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == (
                "_embargoed_view_with_window_unlocked"
            ):
                sites.append((path.relative_to(_SRC).as_posix(), node.lineno))
    return sites


def test_selective_unmask_helper_has_one_guardian_call_site() -> None:
    sites = _selective_unmask_call_sites()
    assert len(sites) == 1, f"selective holdout unmask must have one caller: {sites}"
    assert sites[0][0] == "registry/holdout_guardian.py"


def test_public_full_unmask_has_no_production_call_site() -> None:
    sites: list[tuple[str, int]] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) != "embargoed_view":
                continue
            if any(
                keyword.arg == "unlocked"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            ):
                sites.append((path.relative_to(_SRC).as_posix(), node.lineno))
    assert sites == [], f"public full holdout unmask used in production: {sites}"
