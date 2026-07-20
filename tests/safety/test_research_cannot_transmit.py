"""Prove research configuration and research imports cannot reach broker transmit.

Structural + runtime proofs:
1. research risk policy YAML never mentions live/transmit flags;
2. default Settings under research-like env refuse paper and live transmission;
3. AST: research campaign/readiness/manifest never import chronos.orders/broker;
4. LIVE TRADING BLOCKED is the explicit outcome for default settings.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

import chronos.research as research_pkg
from chronos.config.settings import Settings
from chronos.orders.live_block import LIVE_TRADING_BLOCKED, assert_live_trading_blocked

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESEARCH_POLICY = REPO_ROOT / "config" / "risk.research.yaml"
_FORBIDDEN = ("chronos.orders", "chronos.broker")
_RESEARCH_EDGE_MODULES = (
    "stats",
    "walkforward",
    "purged_cv",
    "campaign",
    "manifest",
    "readiness",
    "runner",
)


def test_research_policy_yaml_has_no_transmit_or_live_keys() -> None:
    raw = RESEARCH_POLICY.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    assert isinstance(data, dict)
    forbidden_keys = {
        "allow_live_trading",
        "allow_order_transmit",
        "ALLOW_LIVE_TRADING",
        "ALLOW_ORDER_TRANSMIT",
        "broker_mode",
        "ib_environment",
        "transmit",
    }
    found = forbidden_keys.intersection(data)
    assert found == set(), f"research risk policy must not set transmit/live keys: {found}"
    # Non-comment lines must not enable transmission (comments may name the boundary).
    for line in raw.splitlines():
        stripped = line.split("#", 1)[0].strip().lower()
        if not stripped:
            continue
        assert "allow_live_trading" not in stripped
        assert "allow_order_transmit" not in stripped
        assert "transmit: true" not in stripped
        assert "transmit:true" not in stripped


def test_research_like_settings_cannot_transmit() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="demo",
        allow_order_transmit=False,
        allow_live_trading=False,
    )
    assert settings.transmission_possible is False
    assert settings.live_transmission_possible is False
    decision = assert_live_trading_blocked(settings)
    assert decision.blocked is True
    assert decision.outcome == LIVE_TRADING_BLOCKED


def test_research_modules_ast_forbid_orders_and_broker() -> None:
    package_dir = Path(research_pkg.__file__).parent
    for name in _RESEARCH_EDGE_MODULES:
        path = package_dir / f"{name}.py"
        assert path.exists(), path
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


def test_live_trading_blocked_token_is_stable() -> None:
    # Operator docs and tests pin this exact string — do not rephrase.
    assert LIVE_TRADING_BLOCKED == "LIVE TRADING BLOCKED"
