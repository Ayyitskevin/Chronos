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
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_import_matcher_sees_subpackage_aliases() -> None:
    assert "chronos.registry" in _imported_names("from chronos import registry\n")


def test_registry_modules_have_no_forbidden_ast_imports() -> None:
    names = {path.stem for path in _module_files()}
    assert {"ledger", "runs", "trials", "budget", "holdout_guardian"} <= names
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


def test_completed_terminal_seam_has_one_production_caller() -> None:
    completion_sites: list[tuple[str, int]] = []
    internal_terminal_sites: list[tuple[str, int]] = []
    source_root = _REPO_ROOT / "src" / "chronos"
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name == "_complete_with_retained_evidence":
                completion_sites.append((path.relative_to(source_root).as_posix(), node.lineno))
            elif name == "_terminalize":
                internal_terminal_sites.append(
                    (path.relative_to(source_root).as_posix(), node.lineno)
                )

    assert len(completion_sites) == 1, (
        f"completion authority must have one production caller: {completion_sites}"
    )
    assert completion_sites[0][0] == "research/trial_runner.py"
    assert len(internal_terminal_sites) == 2
    assert {path for path, _line in internal_terminal_sites} == {"registry/trials.py"}
