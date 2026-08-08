"""Five-Tool research modules must remain unreachable from real trading authority."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import chronos.research.five_tool as five_tool

_FORBIDDEN = (
    "chronos.broker",
    "chronos.orders",
    "chronos.mandates",
    "chronos.service",
)
_MODULES = ("contract", "models", "indicators", "alignment", "engine", "checkpoint")
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_five_tool_modules_have_no_live_authority_imports() -> None:
    package = Path(five_tool.__file__).parent
    for module in _MODULES:
        path = package / f"{module}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        for imported in imports:
            assert not any(
                imported == forbidden or imported.startswith(f"{forbidden}.")
                for forbidden in _FORBIDDEN
            ), f"{path.name} imports live-authority module {imported}"


def test_importing_five_tool_engine_does_not_load_live_authority() -> None:
    probe = (
        "import chronos.research.five_tool.engine, sys; "
        f"blocked={_FORBIDDEN!r}; "
        "bad=[name for name in sys.modules if any(name == prefix or "
        "name.startswith(prefix + '.') for prefix in blocked)]; "
        "print(';'.join(sorted(bad)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == ""


def test_five_tool_is_not_registered_as_runtime_strategy() -> None:
    from chronos.research.runner import STRATEGY_FACTORIES

    assert "five_tool_confluence_v3_6" not in STRATEGY_FACTORIES
