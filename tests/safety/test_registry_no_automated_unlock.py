"""No shipped automated path invokes the holdout unlock (ADR-0013 §7, review safety-4).

The DoD requires that "no scheduled job, proposal-execution path, or copilot artifact
can invoke" the holdout unlock. This is an **accidental-wiring guard** enforced the way
the codebase enforces the single transmit site: an AST walk over the automated planes —
derived from the whole package tree, not a hand-picked list — asserts none of them import
the registry or call its unlock functions by name.

The owner CLI (`chronos holdout unlock`, in `chronos.cli`) legitimately imports the
guardian and is deliberately excluded. A determined runtime evasion (importlib
string-dispatch, `unlocked=<var>`) is out of scope and disclosed in ADR-0013 §7 /
limitations; this test stops the realistic failure — someone wiring the unlock into the
service loop or an order/execution/control module.

**Model-plane bar, retargeted (ADR-0016 / D-16, 2026-07-25).** This bar was written
prospectively against `chronos.copilot` back when D-11 forbade any generative model in a
runtime path. D-16 supersedes D-11 and the model plane now exists for real, as
`chronos.autonomy`. The bar is **retargeted, not relaxed**: `chronos.autonomy` joins the
automated tree, so every module in it is scanned and none may import the registry or call
the unlock. The model plane therefore cannot reach a research holdout, which is the
guarantee ADR-0013 §7 actually makes. `chronos.autonomy` is deliberately *not* added to
the forbidden-import list, because D-16's deterministic supervisor must be able to import
the `AITradeDecision` contract in order to judge it — barring that would forbid the very
gate the model is judged by. D-15/ADR-0013's guarantee is unchanged.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src" / "chronos"

# The automated planes, derived from the tree (every module under them), plus the
# top-level app-wiring module. The owner CLI is intentionally NOT here.
# `autonomy` joins the list under D-16: the model plane is an automated plane.
_AUTOMATED_DIRS = ("service", "services", "control", "execution", "orders", "autonomy")
_AUTOMATED_TOP_LEVEL = ("runtime.py",)

_FORBIDDEN_IMPORTS = ("chronos.registry", "chronos.registry.holdout_guardian")
_FORBIDDEN_CALLS = ("request_unlock", "mediated_holdout_read")
# `chronos.copilot` was never built and stays barred prospectively. The real
# model plane, `chronos.autonomy`, is deliberately NOT listed here: D-16's
# deterministic supervisor must be able to import the AITradeDecision contract
# to judge it. Instead the plane is added to _AUTOMATED_DIRS above, so it is
# itself scanned for registry imports and unlock calls — the model plane is
# barred from reaching holdouts, which is what ADR-0013 §7 actually protects.
_PROSPECTIVE = ("chronos.copilot",)


def _automated_module_files() -> list[Path]:
    files: list[Path] = []
    for directory in _AUTOMATED_DIRS:
        files.extend(sorted((_SRC / directory).rglob("*.py")))
    files.extend(_SRC / name for name in _AUTOMATED_TOP_LEVEL)
    return files


def test_the_automated_tree_is_covered() -> None:
    files = _automated_module_files()
    names = {path.name for path in files}
    # Guard the guard: the key automated modules the reviewers named are in scope.
    assert {"submission.py", "promotion.py", "engine.py", "runtime.py"} <= names
    # The D-16 model plane is scanned like any other automated plane.
    assert {"decision.py", "mandate.py"} <= names
    assert len(files) >= 15  # the whole tree, not a hand-picked few
    for path in files:
        assert path.exists(), f"expected automated module missing: {path}"
    assert not (_SRC / "copilot").exists()  # copilot plane still absent (bar is prospective)


def _imported_names(tree: ast.Module) -> list[str]:
    """Every module path an import can name.

    ``from chronos import registry`` puts the subpackage in the *alias*, not in
    ``node.module``, so checking ``node.module`` alone missed it entirely — the
    same blind spot the M1 adversarial review found in the autonomy isolation
    test. Both halves are emitted here.
    """

    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_the_import_matcher_sees_subpackage_aliases() -> None:
    """Guard the guard: `from chronos import registry` must be visible."""

    assert "chronos.registry" in _imported_names(ast.parse("from chronos import registry\n"))


def test_no_automated_module_imports_or_calls_the_unlock() -> None:
    for path in _automated_module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in _imported_names(tree):
            assert name not in _FORBIDDEN_IMPORTS, f"{path.name} imports {name}"
            assert name not in _PROSPECTIVE, f"{path.name} imports {name}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                assert name not in _FORBIDDEN_CALLS, (
                    f"{path.name}:{node.lineno} calls the holdout unlock {name!r}"
                )
