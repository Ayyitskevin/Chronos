"""Structural enforcement of the single transmit site (ADR-0009 §1).

AST-level proofs over ``src/chronos/orders``:

1. exactly ONE ``transmit=True`` keyword argument exists in the whole package,
   and it lives in ``submission.py``;
2. no module outside ``submission.py`` calls ``to_order_request`` with a
   ``transmit`` argument that is not the literal ``False``;
3. ``chronos.orders`` imports nothing from the autonomous plane
   (``chronos.execution`` / ``chronos.risk``).

These are source-level facts, so a regression cannot hide behind mocking.
"""

from __future__ import annotations

import ast
from pathlib import Path

import chronos.orders

ORDERS_DIR = Path(chronos.orders.__file__).parent


def _module_asts() -> dict[str, ast.AST]:
    return {
        path.name: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(ORDERS_DIR.glob("*.py"))
    }


def test_exactly_one_transmit_true_keyword_in_the_package() -> None:
    sites: list[tuple[str, int]] = []
    for name, tree in _module_asts().items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.keyword) or node.arg != "transmit":
                continue
            if isinstance(node.value, ast.Constant) and node.value.value is True:
                sites.append((name, node.value.lineno))
    assert len(sites) == 1, (
        f"expected exactly one transmit=True keyword in chronos.orders; found: {sites}"
    )
    assert sites[0][0] == "submission.py", f"the single transmit site moved: {sites}"


def test_no_module_outside_submission_passes_transmit_non_false() -> None:
    # Widened to ALL of src/chronos (M7 review finding F8): a transmit=True
    # call in chronos.api / chronos.ui would be invisible to an orders-only
    # scan.
    import chronos

    src_root = Path(chronos.__file__).parent
    offenders: list[tuple[str, int]] = []
    for path in sorted(src_root.rglob("*.py")):
        name = str(path.relative_to(src_root))
        if name == "orders/submission.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if attr != "to_order_request":
                continue
            for keyword in node.keywords:
                if keyword.arg != "transmit":
                    continue
                value = keyword.value
                if not (isinstance(value, ast.Constant) and value.value is False):
                    offenders.append((name, node.lineno))
    assert offenders == [], (
        f"modules outside submission.py may only call to_order_request with the "
        f"literal transmit=False; offenders: {offenders}"
    )


def test_orders_package_imports_nothing_from_the_autonomous_plane() -> None:
    forbidden = ("chronos.execution", "chronos.risk")
    offenders: list[tuple[str, str]] = []
    for name, tree in _module_asts().items():
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            offenders.extend(
                (name, target)
                for target in imported
                if target == "chronos.risk"
                or target.startswith(("chronos.execution", "chronos.risk."))
            )
    assert offenders == [], f"chronos.orders must not import the autonomous plane: {offenders}"
    del forbidden
