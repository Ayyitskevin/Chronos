"""End-to-end coverage for the operator CLI (`python -m chronos.cli`).

A reviewer flagged this module as untested. The CLI is a safety surface: every
command prints the mode banner, none can enable live trading, and there is no
``--force``. These tests drive each subcommand through ``main(argv)`` and pin
its exit code and durable side effects. They also assert the structural safety
property: no subcommand and no flag turns live trading on.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from pathlib import Path

import pytest

from chronos.cli.main import build_parser, main
from chronos.control.halt import HaltStore


def _write_uptrend_csv(path: Path, *, n: int = 60) -> None:
    lines = ["date,open,high,low,close,volume"]
    session = date(2024, 1, 1)
    prev_close = 100.0
    for i in range(n):
        while session.weekday() >= 5:
            session += timedelta(days=1)
        close = 100.0 + 0.5 * i
        open_ = prev_close
        high = max(open_, close) + 1.0
        low = min(open_, close) - 1.0
        lines.append(f"{session.isoformat()},{open_},{high},{low},{close},1000000")
        prev_close = close
        session += timedelta(days=1)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _permissive_policy(path: Path) -> Path:
    path.write_text(
        "policy_version: 'cli-test'\n"
        "allowed_symbols: ['SPY']\n"
        "allowed_strategy_ids: ['baseline_buy_hold']\n"
        "allow_long_entries: true\n"
        "max_bot_capital_usd: 3000\n"
        "max_position_notional_usd: 3000\n"
        "max_aggregate_exposure_usd: 3000\n"
        "max_symbol_exposure_fraction: 1.0\n"
        "max_risk_per_trade_fraction: 0.1\n"
        "max_simultaneous_positions: 1\n"
        "max_open_orders: 2\n"
        "max_daily_loss_usd: 300\n"
        "max_weekly_loss_usd: 600\n"
        "max_drawdown_fraction: 0.5\n"
        "max_consecutive_losses: 10\n"
        "max_quote_age_seconds: 300\n"
        "max_bar_age_seconds: 432000\n"
        "max_price_deviation_fraction: 0.05\n"
        "allow_overnight_positions: true\n",
        encoding="utf-8",
    )
    return path


def _base_args(tmp_path: Path) -> list[str]:
    return [
        "--halt-file",
        str(tmp_path / "halt.json"),
        "--audit-file",
        str(tmp_path / "audit.jsonl"),
    ]


def test_status_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([*_base_args(tmp_path), "status"]) == 0
    out = capsys.readouterr().out
    assert "CHRONOS PLATFORM" in out
    assert "LIVE TRADING" in out


def test_halt_then_rearm_roundtrip(tmp_path: Path) -> None:
    halt_file = tmp_path / "halt.json"
    args = ["--halt-file", str(halt_file), "--audit-file", str(tmp_path / "audit.jsonl")]
    assert main([*args, "halt", "--reason", "manual test"]) == 0
    assert HaltStore(halt_file).read().halted is True
    assert main([*args, "rearm", "--note", "cleared after review"]) == 0
    assert HaltStore(halt_file).read().halted is False


def test_risk_show_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    policy = _permissive_policy(tmp_path / "risk.yaml")
    assert main([*_base_args(tmp_path), "risk-show", "--policy", str(policy)]) == 0
    assert "policy version: cli-test" in capsys.readouterr().out


def test_verify_audit_log_on_empty_is_ok(tmp_path: Path) -> None:
    assert main([*_base_args(tmp_path), "verify-audit-log"]) == 0


def test_verify_corpus_detects_match_and_mismatch(tmp_path: Path) -> None:
    script = tmp_path / "script.pine"
    script.write_text("// pine\n", encoding="utf-8")
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        f"scripts:\n  - catalog_number: '01'\n    path: '{script}'\n    sha256: '{digest}'\n",
        encoding="utf-8",
    )
    assert main([*_base_args(tmp_path), "verify-corpus", "--registry", str(registry)]) == 0

    registry.write_text(
        f"scripts:\n  - catalog_number: '01'\n    path: '{script}'\n    sha256: 'deadbeef'\n",
        encoding="utf-8",
    )
    assert main([*_base_args(tmp_path), "verify-corpus", "--registry", str(registry)]) == 1


def test_verify_corpus_missing_registry_returns_one(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    assert main([*_base_args(tmp_path), "verify-corpus", "--registry", str(missing)]) == 1


def test_shadow_scan_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_uptrend_csv(data_dir / "SPY.csv")
    policy = _permissive_policy(tmp_path / "risk.yaml")
    code = main(
        [
            *_base_args(tmp_path),
            "shadow-scan",
            "--strategies",
            "baseline_buy_hold",
            "--symbols",
            "SPY",
            "--data-dir",
            str(data_dir),
            "--policy",
            str(policy),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "SHADOW MODE" in out
    assert "NO_ORDERS" in out


def test_shadow_scan_unknown_strategy_returns_one(tmp_path: Path) -> None:
    code = main(
        [
            *_base_args(tmp_path),
            "shadow-scan",
            "--strategies",
            "not_a_strategy",
            "--data-dir",
            str(tmp_path),
        ]
    )
    assert code == 1


def test_monitor_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    policy = _permissive_policy(tmp_path / "risk.yaml")
    code = main(
        [
            *_base_args(tmp_path),
            "monitor",
            "--mode",
            "shadow",
            "--policy",
            str(policy),
            "--symbols",
            "SPY",
        ]
    )
    assert code == 0
    assert "CHRONOS PLATFORM MONITOR" in capsys.readouterr().out


def test_backtest_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_uptrend_csv(data_dir / "SPY.csv")
    policy = _permissive_policy(tmp_path / "risk.yaml")
    code = main(
        [
            *_base_args(tmp_path),
            "backtest",
            "--strategy",
            "baseline_buy_hold",
            "--symbol",
            "SPY",
            "--data-dir",
            str(data_dir),
            "--policy",
            str(policy),
        ]
    )
    assert code == 0
    assert '"strategy"' in capsys.readouterr().out


def test_no_subcommand_exposes_live_trading() -> None:
    # Structural safety: no subcommand name enables live trading, and none of
    # the parser's option strings is a live/force override.
    parser = build_parser()
    subactions = [
        action for action in parser._actions if action.__class__.__name__ == "_SubParsersAction"
    ]
    assert subactions, "expected a subparsers action"
    names = set(subactions[0].choices)
    assert "live" not in names
    assert not any("live" in name for name in names)
    banned = {"--force", "--live", "--enable-live", "--transmit", "--yes-really"}
    for choice in subactions[0].choices.values():
        options = {opt for action in choice._actions for opt in action.option_strings}
        assert not (options & banned)
