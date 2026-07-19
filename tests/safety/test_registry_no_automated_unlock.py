"""No automated path can invoke the holdout unlock (ADR-0013 §7).

The DoD requires that "no scheduled job, proposal-execution path, or copilot artifact
can invoke" the holdout unlock. This is enforced structurally, the way the codebase
already enforces the single transmit site (``test_single_transmit_site.py``): an AST
walk over the scheduler / service / proposal / promotion / submission modules asserts
none of them import the guardian module nor call its unlock functions by name.

The interactive-owner path is the CLI (``chronos holdout unlock``), which is *not* in
this set. A ``copilot`` package does not exist yet; its prospective path is included so
D3/D4 inherit the bar the moment it is added, and the guard asserts the concrete
targets exist so this test cannot silently pass on a moved file.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src" / "chronos"

_FORBIDDEN_IMPORTS = ("chronos.registry.holdout_guardian", "chronos.registry")
_FORBIDDEN_CALLS = ("request_unlock", "mediated_holdout_read")
# Future copilot plane — barred prospectively (no module exists yet).
_PROSPECTIVE = ("chronos.copilot",)


def _automated_module_files() -> list[Path]:
    files: list[Path] = []
    files.extend(sorted((_SRC / "service").glob("*.py")))
    files.extend(sorted((_SRC / "services").glob("*.py")))
    files.append(_SRC / "control" / "promotion.py")
    files.append(_SRC / "execution" / "engine.py")
    files.append(_SRC / "orders" / "submission.py")
    return files


def test_the_automated_targets_exist() -> None:
    # Guard the guard: a renamed/moved target must fail loudly, not silently skip.
    for path in _automated_module_files():
        assert path.exists(), f"expected automated-path module missing: {path}"
    # The copilot plane is still absent (its bar is prospective).
    assert not (_SRC / "copilot").exists()


def test_no_automated_module_imports_or_calls_the_unlock() -> None:
    for path in _automated_module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in _FORBIDDEN_IMPORTS, f"{path.name} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module not in _FORBIDDEN_IMPORTS, f"{path.name} imports {node.module}"
                assert node.module not in _PROSPECTIVE, f"{path.name} imports {node.module}"
            elif isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                assert name not in _FORBIDDEN_CALLS, (
                    f"{path.name}:{node.lineno} calls the holdout unlock {name!r}"
                )
