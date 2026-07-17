"""Daemon-level resilience for the service entry point.

Proves the ``python -m chronos.service`` shell fails closed: a fresh (unarmed)
deployment refuses to run cycles, a configuration error halts and exits
nonzero, and an exception raised inside a decision cycle persists a halt and
exits nonzero rather than continuing.
"""

from __future__ import annotations

from pathlib import Path

from chronos.control.halt import HaltReason, HaltStore
from chronos.service.__main__ import main


def _args(tmp_path: Path, **overrides: str) -> list[str]:
    halt = overrides.get("halt", str(tmp_path / "halt.json"))
    audit = overrides.get("audit", str(tmp_path / "audit.jsonl"))
    data = overrides.get("data", str(tmp_path / "data"))
    policy = overrides.get("policy", "config/risk.example.yaml")
    return [
        "--halt-file",
        halt,
        "--audit-file",
        audit,
        "--data-dir",
        data,
        "--policy",
        policy,
        "--symbols",
        "SPY",
    ]


def test_fresh_deployment_refuses_and_returns_one(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    code = main(_args(tmp_path))
    assert code == 1  # halted (NEVER_ARMED); no cycles


def test_invalid_policy_halts_and_returns_two(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    code = main(_args(tmp_path, policy=str(tmp_path / "nonexistent.yaml")))
    assert code == 2
    state = HaltStore(tmp_path / "halt.json").read()
    assert state.reason is HaltReason.INVALID_CONFIGURATION


def test_cycle_exception_halts_and_returns_three(tmp_path: Path) -> None:
    # A malformed CSV makes the data source raise inside the cycle. The daemon
    # must persist a halt and exit nonzero, never continue.
    data = tmp_path / "data"
    data.mkdir()
    (data / "SPY.csv").write_text("date,open,high,low,close,volume\nnot,a,valid,row,x,y\n")
    HaltStore(tmp_path / "halt.json").rearm("armed for chaos test")
    code = main(_args(tmp_path))
    assert code == 3
    state = HaltStore(tmp_path / "halt.json").read()
    assert state.halted
    assert state.reason is HaltReason.STRATEGY_EXCEPTION
