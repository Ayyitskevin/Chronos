"""Five-Tool research modules must remain unreachable from real trading authority."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

_DIRECT_FORBIDDEN = (
    "chronos.orders",
    "chronos.broker",
    "chronos.execution",
    "chronos.risk",
    "chronos.autonomy",
    "chronos.api",
    "chronos.persistence",
    "chronos.service",
    "chronos.services",
    "chronos.control",
    "chronos.paperops",
    "chronos.supervisor",
    "chronos.strategies",
    "chronos.strategy",
    "chronos.registry.holdout_guardian",
    "ib_async",
    "ibapi",
    "sqlalchemy",
)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PACKAGE = _REPO_ROOT / "src" / "chronos" / "research" / "five_tool"
_MODULE_NAMES = (
    *(
        "chronos.research.five_tool"
        if path.name == "__init__.py"
        else f"chronos.research.five_tool.{path.stem}"
        for path in sorted(_PACKAGE.glob("*.py"))
    ),
    "chronos.research.five_tool_trials",
    "chronos.research.tradingview",
)
_MODULE_PATHS = (
    *sorted(_PACKAGE.glob("*.py")),
    _REPO_ROOT / "src/chronos/research/five_tool_trials.py",
    _REPO_ROOT / "src/chronos/research/tradingview.py",
)
_MODULE_SPECS = tuple(zip(_MODULE_PATHS, _MODULE_NAMES, strict=True))


def _imported_names(source: str, *, module_name: str) -> list[str]:
    """Resolve direct, from, relative, and literal dynamic import paths."""

    tree = ast.parse(source)
    names: list[str] = []
    package = (
        module_name
        if module_name == "chronos.research.five_tool"
        else module_name.rsplit(".", 1)[0]
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative_name = "." * node.level + (node.module or "")
                base = importlib.util.resolve_name(relative_name, package)
            elif node.module:
                base = node.module
            else:
                continue
            names.append(base)
            names.extend(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id == "__import__")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
        ):
            if not node.args or not isinstance(node.args[0], ast.Constant):
                names.append("<dynamic-import>")
                continue
            requested = node.args[0].value
            if not isinstance(requested, str):
                names.append("<dynamic-import>")
            elif requested.startswith("."):
                names.append(importlib.util.resolve_name(requested, package))
            else:
                names.append(requested)
    return names


def test_import_matcher_sees_subpackage_aliases() -> None:
    assert "chronos.broker" in _imported_names(
        "from chronos import broker\n", module_name="chronos.research.five_tool.engine"
    )
    assert "chronos.research.five_tool.models" in _imported_names(
        "from . import models\n", module_name="chronos.research.five_tool.engine"
    )
    assert "chronos.broker" in _imported_names(
        "importlib.import_module('chronos.broker')\n",
        module_name="chronos.research.five_tool.engine",
    )


def test_five_tool_modules_have_no_live_authority_imports() -> None:
    for path, module_name in _MODULE_SPECS:
        for imported in _imported_names(path.read_text(encoding="utf-8"), module_name=module_name):
            assert imported != "<dynamic-import>", f"{path.name} has an unresolved dynamic import"
            assert not any(
                imported == forbidden or imported.startswith(f"{forbidden}.")
                for forbidden in _DIRECT_FORBIDDEN
            ), f"{path.name} imports live-authority module {imported}"


def test_importing_five_tool_research_surface_does_not_load_live_authority() -> None:
    probe = (
        "import importlib, sys; "
        f"modules={_MODULE_NAMES!r}; "
        "[importlib.import_module(name) for name in modules]; "
        f"blocked={_DIRECT_FORBIDDEN!r}; "
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
