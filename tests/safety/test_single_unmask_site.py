"""A holdout is unmasked from exactly one site — the guardian (ADR-0013 §3, review safety-3).

`embargoed_view(..., unlocked=True)` returns held-out data with no grant/phrase/burn. The
guardian is meant to be the *only* caller that passes it. This AST test enforces that
across `src/chronos`: any `unlocked=True` keyword argument must appear only in
`holdout_guardian.py`. It is an accidental-wiring guard (a dynamic `unlocked=<var>` or a
direct `read_bars` bypass is out of scope and disclosed in ADR-0013 / limitations), the
same shape and limitation as `test_single_transmit_site.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src" / "chronos"


def _unmask_call_sites() -> list[tuple[str, int]]:
    sites: list[tuple[str, int]] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "unlocked"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    sites.append((path.name, node.lineno))
    return sites


def test_unlocked_true_is_passed_only_by_the_guardian() -> None:
    sites = _unmask_call_sites()
    offenders = [(name, line) for name, line in sites if name != "holdout_guardian.py"]
    assert offenders == [], f"holdout unmasked outside the guardian: {offenders}"
    # And the guardian really is a site (the guard is not vacuous).
    assert any(name == "holdout_guardian.py" for name, _ in sites)
