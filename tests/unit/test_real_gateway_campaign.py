"""Executable contracts for the owner-gated real-gateway campaign helper."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from chronos.config.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude/skills/chronos-real-gateway-campaign/SKILL.md"
CAPTURE = ROOT / ".claude/skills/chronos-real-gateway-campaign/scripts/capture_readonly.py"


def _load_capture_module() -> ModuleType:
    module_name = "chronos_real_gateway_capture"
    spec = importlib.util.spec_from_file_location(module_name, CAPTURE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_demo_capture_retains_partial_qualification_for_market_rules() -> None:
    capture_module = _load_capture_module()
    args = argparse.Namespace(
        label="market-rule-contract",
        symbols=["AAPL"],
        max_symbols=1,
        skip_options=False,
        skip_bars=True,
        account_ids=set(),
    )

    capture = asyncio.run(capture_module.run_capture(Settings(broker_mode="demo"), args))
    steps = capture["steps"]

    assert "error" in steps["symbol:AAPL:qualify_option_contracts"]
    rules = steps["symbol:AAPL:option_market_rules"]
    assert rules == [
        {
            "con_id": 2002,
            "exchange": "SMART",
            "market_rule_id": 26,
            "price_increments": [
                {"increment": "0.01", "low_edge": "0"},
                {"increment": "0.05", "low_edge": "3"},
            ],
            "source": "demo-fixture-v1",
        }
    ]
    assert steps["active_subscription_count_before_disconnect"] == 0
    assert steps["disconnect"] == {"ok": True}


def test_gateway_skill_describes_the_executable_market_rule_capture() -> None:
    text = SKILL.read_text(encoding="utf-8")
    lowered = text.lower()

    assert "`option_market_rules`" in text
    assert "read nowhere" not in lowered
    assert "gateway-unobservable today" not in lowered
