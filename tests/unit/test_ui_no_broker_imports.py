"""The backend-driven UI must never reach a broker (M4 guarantee).

Two layers: (1) an AST walk asserting no forbidden direct import in any UI
client/page/app module, and (2) a subprocess probe asserting that importing
the api_client pulls no broker/network-adapter module into sys.modules —
this catches transitive reach the AST walk cannot see.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import chronos.ui.api_client as api_client_mod
import chronos.ui.backend_app as backend_app_mod
from chronos.ui import pages as pages_pkg

_FORBIDDEN_PREFIXES = (
    "chronos.broker",
    "chronos.runtime",
    "chronos.ui.session",
    "chronos.services",
    "chronos.orders",
    "ib_async",
    "ibapi",
)


def _module_files() -> list[Path]:
    files = [Path(api_client_mod.__file__), Path(backend_app_mod.__file__)]
    pages_dir = Path(pages_pkg.__file__).parent
    files.extend(sorted(pages_dir.glob("*.py")))
    return files


def _imported_names(source: str) -> list[str]:
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_ui_modules_have_no_forbidden_direct_imports() -> None:
    for path in _module_files():
        source = path.read_text(encoding="utf-8")
        for name in _imported_names(source):
            for forbidden in _FORBIDDEN_PREFIXES:
                assert not (name == forbidden or name.startswith(forbidden + ".")), (
                    f"{path.name} imports forbidden module {name!r}"
                )


def test_importing_api_client_pulls_no_broker_module() -> None:
    # Subprocess so this test process's own imports don't pollute the check.
    probe = (
        "import chronos.ui.api_client, sys; "
        "bad=[m for m in sys.modules if m.startswith(('chronos.broker','ib_async','ibapi',"
        "'chronos.runtime'))]; "
        "print(';'.join(sorted(bad)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent.parent),
        check=True,
    )
    leaked = [name for name in result.stdout.strip().split(";") if name]
    assert leaked == [], f"importing api_client leaked broker modules: {leaked}"
