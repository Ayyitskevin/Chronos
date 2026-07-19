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
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src" / "chronos"

# The automated planes, derived from the tree (every module under them), plus the
# top-level app-wiring module. The owner CLI is intentionally NOT here.
_AUTOMATED_DIRS = ("service", "services", "control", "execution", "orders")
_AUTOMATED_TOP_LEVEL = ("runtime.py",)

_FORBIDDEN_IMPORTS = ("chronos.registry", "chronos.registry.holdout_guardian")
_FORBIDDEN_CALLS = ("request_unlock", "mediated_holdout_read")
_PROSPECTIVE = ("chronos.copilot",)  # barred before it exists


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
    assert len(files) >= 15  # the whole tree, not a hand-picked few
    for path in files:
        assert path.exists(), f"expected automated module missing: {path}"
    assert not (_SRC / "copilot").exists()  # copilot plane still absent (bar is prospective)


def test_no_automated_module_imports_or_calls_the_unlock() -> None:
    for path in _automated_module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in _FORBIDDEN_IMPORTS, f"{path.name} imports {alias.name}"
                    assert alias.name not in _PROSPECTIVE, f"{path.name} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module not in _FORBIDDEN_IMPORTS, f"{path.name} imports {node.module}"
                assert node.module not in _PROSPECTIVE, f"{path.name} imports {node.module}"
            elif isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                assert name not in _FORBIDDEN_CALLS, (
                    f"{path.name}:{node.lineno} calls the holdout unlock {name!r}"
                )
