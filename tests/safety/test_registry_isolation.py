"""The experiment registry must never reach the trading plane (ADR-0013 §7).

Mirrors ``tests/safety/test_histdata_isolation.py``: an AST walk asserts no
``chronos.registry`` module imports anything in the forbidden set, and a subprocess
probe asserts importing the package leaks nothing forbidden into ``sys.modules``. The
registry is research-plane only — it opens no trading DB and reaches no order path.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import chronos.registry as registry_pkg

_FORBIDDEN = (
    "chronos.orders",
    "chronos.api",
    "chronos.services",
    "chronos.service",
    "chronos.execution",
    "chronos.risk",
    "chronos.control",
    "chronos.broker",
    "chronos.runtime",
    "chronos.ui",
    "sqlalchemy",
    "sqlite3",
)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _module_files() -> list[Path]:
    return sorted(Path(registry_pkg.__file__).parent.glob("*.py"))


def _imported_names(source: str) -> list[str]:
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_registry_modules_have_no_forbidden_ast_imports() -> None:
    names = {path.stem for path in _module_files()}
    assert {"ledger", "runs", "budget", "holdout_guardian"} <= names
    for path in _module_files():
        for name in _imported_names(path.read_text(encoding="utf-8")):
            for forbidden in _FORBIDDEN:
                assert not (name == forbidden or name.startswith(forbidden + ".")), (
                    f"{path.name} imports forbidden module {name!r}"
                )


def test_importing_registry_leaks_no_forbidden_module() -> None:
    prefixes = repr(_FORBIDDEN)
    probe = (
        "import chronos.registry, sys; "
        f"bad=[m for m in sys.modules if m.startswith({prefixes})]; "
        "print(';'.join(sorted(bad)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        check=True,
    )
    leaked = [name for name in result.stdout.strip().split(";") if name]
    assert leaked == [], f"registry import leaked forbidden modules: {leaked}"
