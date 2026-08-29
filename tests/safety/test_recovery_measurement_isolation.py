"""Recovery measurement stays outside broker, network, and destructive surfaces."""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_RECOVERY_ROOT = _ROOT / "src/chronos/recovery"
_FORBIDDEN_IMPORT_PREFIXES = (
    "httpx",
    "requests",
    "socket",
    "subprocess",
    "urllib",
    "chronos.api",
    "chronos.broker",
    "chronos.execution.brokers",
    "chronos.orders.submission",
    "chronos.services",
    "chronos.supervisor",
)
_FORBIDDEN_MUTATIONS = {"cancelOrder", "placeOrder", "remove", "replace", "rmtree", "unlink"}


def _violations(source: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [f"{module}.{alias.name}" for alias in node.names]
        else:
            names = []
        for name in names:
            if name.startswith(_FORBIDDEN_IMPORT_PREFIXES):
                violations.append(f"forbidden import {name}")
        if isinstance(node, ast.Call):
            function = node.func
            called = function.attr if isinstance(function, ast.Attribute) else None
            if called in _FORBIDDEN_MUTATIONS:
                violations.append(f"forbidden mutation call {called}")
    return tuple(violations)


def test_recovery_measurement_has_no_network_broker_or_destructive_surface() -> None:
    violations: list[str] = []
    for path in sorted(_RECOVERY_ROOT.glob("*.py")):
        violations.extend(f"{path.name}: {item}" for item in _violations(path.read_text()))
    assert not violations, "\n".join(violations)


def test_the_isolation_scanner_detects_forbidden_import_forms_and_mutations() -> None:
    planted = (
        "import httpx\nfrom chronos import broker\nfrom pathlib import Path\nPath('x').unlink()\n"
    )
    assert _violations(planted) == (
        "forbidden import httpx",
        "forbidden import chronos.broker",
        "forbidden mutation call unlink",
    )
