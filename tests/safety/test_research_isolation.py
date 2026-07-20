"""The walk-forward research code must never reach the real order/broker path (ADR-0014).

The research plane deliberately drives the deterministic backtest engine, which uses the
*simulated* execution broker — so unlike ``registry``/``histdata`` it legitimately reaches
``chronos.execution``/``chronos.control``/``chronos.risk`` transitively. The load-bearing
boundary for C3 is therefore narrower and exact: the new statistics/walk-forward/CV modules
import **no real order-submission or broker-adapter module** (``chronos.orders`` /
``chronos.broker``), and importing them leaks neither into ``sys.modules``.

Mirrors ``tests/safety/test_histdata_isolation.py``: an AST walk over the specific C3
modules plus a subprocess ``sys.modules`` probe.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import chronos.research as research_pkg

# The real trading edge C3 must not touch. NOT chronos.execution/control/risk: those are
# reached through the *simulated* backtest engine by design (see module docstring).
_FORBIDDEN = ("chronos.orders", "chronos.broker")
# C3 + readiness-gate modules: pure research plane, no order/broker edge.
_C3_MODULES = ("stats", "walkforward", "purged_cv", "campaign", "manifest", "readiness")
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _module_files() -> list[Path]:
    package_dir = Path(research_pkg.__file__).parent
    return [package_dir / f"{name}.py" for name in _C3_MODULES]


def _imported_names(source: str) -> list[str]:
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_c3_modules_exist() -> None:
    for path in _module_files():
        assert path.exists(), f"expected C3 module missing: {path}"


def test_c3_modules_have_no_forbidden_ast_imports() -> None:
    for path in _module_files():
        for name in _imported_names(path.read_text(encoding="utf-8")):
            for forbidden in _FORBIDDEN:
                assert not (name == forbidden or name.startswith(forbidden + ".")), (
                    f"{path.name} imports forbidden module {name!r}"
                )


def test_importing_walkforward_leaks_no_order_or_broker_module() -> None:
    prefixes = repr(_FORBIDDEN)
    probe = (
        "import chronos.research.stats, chronos.research.walkforward, "
        "chronos.research.purged_cv, chronos.research.campaign, "
        "chronos.research.manifest, chronos.research.readiness, sys; "
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
    assert leaked == [], f"research walk-forward import leaked forbidden modules: {leaked}"
