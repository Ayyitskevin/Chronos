"""The historical-data plane must never reach the trading plane (ADR-0011 §1).

Load-bearing safety proof, mirroring ``tests/unit/test_ui_no_broker_imports.py``:

1. an AST walk asserting no ``chronos.histdata`` module imports anything in the
   pinned forbidden set (order/broker/persistence/lease/DB planes) — ``ibapi`` /
   ``ib_async`` are deliberately excluded here, because the AST walk descends into
   the official client's *legitimate* lazy ``import ibapi`` and would false-fail;
2. a subprocess probe asserting that importing the package, the official-client
   module, and ``__main__`` leaks none of the forbidden set — **plus** ``ibapi`` /
   ``ib_async`` — into ``sys.modules`` (catches transitive reach + a non-lazy regression);
3. a positive probe asserting the official client's ``ibapi`` import stays lazy.

Because the store is file-based and ``sqlalchemy`` / ``sqlite3`` are forbidden, the
process cannot open the trading DB or mutate the ``writer_lease`` row — "never holds
the writer lease" is structural, not conventional.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import chronos.histdata as histdata_pkg

# The single source of truth (ADR-0011 §1). AST layer: everything here. Probe layer:
# everything here PLUS ibapi/ib_async (which must not appear in the AST layer).
_FORBIDDEN = (
    "chronos.orders",
    "chronos.api",
    "chronos.services",
    "chronos.service",
    "chronos.execution",
    "chronos.risk",
    "chronos.control",
    "chronos.persistence",
    "chronos.utils.locking",
    "chronos.broker",
    "chronos.runtime",
    "chronos.ui",
    "sqlalchemy",
    "sqlite3",
)
_PROBE_FORBIDDEN = (*_FORBIDDEN, "ibapi", "ib_async")
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _module_files() -> list[Path]:
    package_dir = Path(histdata_pkg.__file__).parent
    return sorted(package_dir.glob("*.py"))


def _imported_names(source: str) -> list[str]:
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _leaked(probe: str) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        check=True,
    )
    return [name for name in result.stdout.strip().split(";") if name]


def test_histdata_modules_have_no_forbidden_ast_imports() -> None:
    # Guard the guard: the package really does contain the modules we expect.
    names = {path.stem for path in _module_files()}
    assert {
        "official_client",
        "official_options_client",
        "store",
        "options_store",
        "backfill",
        "options_capture",
        "__main__",
    } <= names
    for path in _module_files():
        for name in _imported_names(path.read_text(encoding="utf-8")):
            for forbidden in _FORBIDDEN:
                assert not (name == forbidden or name.startswith(forbidden + ".")), (
                    f"{path.name} imports forbidden module {name!r}"
                )


def test_importing_histdata_leaks_no_forbidden_module() -> None:
    prefixes = repr(_PROBE_FORBIDDEN)
    probe = (
        "import chronos.histdata, chronos.histdata.official_client, "
        "chronos.histdata.official_options_client, chronos.histdata.__main__, sys; "
        f"bad=[m for m in sys.modules if m.startswith({prefixes})]; "
        "print(';'.join(sorted(bad)))"
    )
    leaked = _leaked(probe)
    assert leaked == [], f"histdata import leaked forbidden modules: {leaked}"


def test_official_clients_keep_ibapi_lazy() -> None:
    probe = (
        "import chronos.histdata.official_client, "
        "chronos.histdata.official_options_client, sys; "
        "bad=[m for m in sys.modules if m.startswith(('ibapi','ib_async'))]; "
        "print(';'.join(sorted(bad)))"
    )
    leaked = _leaked(probe)
    assert leaked == [], f"official client imported ibapi eagerly (must be lazy): {leaked}"
