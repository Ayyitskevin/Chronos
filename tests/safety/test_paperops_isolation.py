"""Paperops pure modules must not import broker adapters or enable live transmit."""

from __future__ import annotations

import ast
from pathlib import Path

import chronos.paperops as paperops_pkg
from chronos.config.settings import Settings
from chronos.orders.live_block import LIVE_TRADING_BLOCKED, assert_live_trading_blocked

_FORBIDDEN = ("chronos.broker",)
_MODULES = (
    "reasons",
    "records",
    "ledger",
    "bootstrap",
    "data_quality",
    "controls",
    "control_memory",
    "decision",
    "session",
    "replay",
    "review",
    "reconcile",
)


def test_paperops_modules_exist() -> None:
    package_dir = Path(paperops_pkg.__file__).parent
    for name in _MODULES:
        assert (package_dir / f"{name}.py").exists()


def test_paperops_ast_avoids_broker_adapters() -> None:
    package_dir = Path(paperops_pkg.__file__).parent
    for name in _MODULES:
        path = package_dir / f"{name}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            for mod in imported:
                for forbidden in _FORBIDDEN:
                    assert not (mod == forbidden or mod.startswith(forbidden + ".")), (
                        f"{path.name} imports forbidden {mod!r}"
                    )


def test_paperops_does_not_enable_live() -> None:
    settings = Settings(_env_file=None)
    decision = assert_live_trading_blocked(settings)
    assert decision.outcome == LIVE_TRADING_BLOCKED
    assert settings.live_transmission_possible is False
