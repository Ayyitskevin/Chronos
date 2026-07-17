"""Coverage for the named-strategy backtest runner and its manifest.

The runner is the reproducibility seam: every run must emit code commit, data
hash, policy hash, and configuration alongside the metrics. These tests pin
that contract and the runner's input validation.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from chronos.research.runner import (
    STRATEGY_FACTORIES,
    current_commit,
    run_named_backtest,
)


def _write_uptrend_csv(path: Path, *, symbol: str = "SPY", n: int = 60) -> None:
    lines = ["date,open,high,low,close,volume"]
    session = date(2024, 1, 1)
    prev_close = 100.0
    for i in range(n):
        while session.weekday() >= 5:  # weekdays only, no weekend bars
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
        "policy_version: 'test-1'\n"
        "allowed_symbols: ['SPY']\n"
        "allowed_strategy_ids: ['baseline_buy_hold', 'baseline_sma_trend',"
        " 'baseline_random_entries', 'regime_trend_v1', 'mean_reversion_v1']\n"
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


def test_run_named_backtest_emits_reproducibility_manifest(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_uptrend_csv(data_dir / "SPY.csv")
    policy = _permissive_policy(tmp_path / "risk.yaml")

    summary = run_named_backtest(
        strategy_name="baseline_buy_hold",
        symbol="SPY",
        data_dir=data_dir,
        policy_path=policy,
        initial_cash=3000.0,
        slippage_bps=2.0,
        halt_path=tmp_path / "halt.json",
    )

    # The manifest fields that make a run reproducible must all be present.
    for key in (
        "strategy",
        "strategy_version",
        "symbol",
        "bars",
        "date_range",
        "data_sha256",
        "policy_version",
        "policy_hash",
        "code_commit",
        "config",
        "risk_rejections",
        "metrics",
    ):
        assert key in summary
    assert summary["strategy"] == "baseline_buy_hold"
    assert summary["symbol"] == "SPY"
    assert summary["bars"] == 60
    assert len(str(summary["data_sha256"])) == 64  # sha256 hex digest
    assert summary["config"] == {"initial_cash_usd": 3000.0, "slippage_bps_per_side": 2.0}
    # Identity fields must be real values, not empty placeholders: the
    # manifest is the reproducibility seam (M5 review).
    assert summary["strategy_version"]
    assert summary["policy_version"] == "test-1"
    policy_hash = str(summary["policy_hash"])
    assert len(policy_hash) == 16 and all(c in "0123456789abcdef" for c in policy_hash)
    assert summary["code_commit"]  # a hash in-repo; "unknown" only if git is absent


def test_run_named_backtest_is_deterministic(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_uptrend_csv(data_dir / "SPY.csv")
    policy = _permissive_policy(tmp_path / "risk.yaml")
    kwargs = dict(
        strategy_name="baseline_sma_trend",
        symbol="SPY",
        data_dir=data_dir,
        policy_path=policy,
        initial_cash=3000.0,
        slippage_bps=2.0,
        halt_path=tmp_path / "halt.json",
    )
    first = run_named_backtest(**kwargs)  # type: ignore[arg-type]
    second = run_named_backtest(**kwargs)  # type: ignore[arg-type]
    assert first["metrics"] == second["metrics"]
    assert first["data_sha256"] == second["data_sha256"]


def test_date_window_narrows_the_series(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_uptrend_csv(data_dir / "SPY.csv", n=60)
    policy = _permissive_policy(tmp_path / "risk.yaml")
    full = run_named_backtest(
        strategy_name="baseline_buy_hold",
        symbol="SPY",
        data_dir=data_dir,
        policy_path=policy,
        initial_cash=3000.0,
        slippage_bps=2.0,
        halt_path=tmp_path / "halt.json",
    )
    windowed = run_named_backtest(
        strategy_name="baseline_buy_hold",
        symbol="SPY",
        data_dir=data_dir,
        policy_path=policy,
        initial_cash=3000.0,
        slippage_bps=2.0,
        halt_path=tmp_path / "halt2.json",
        date_start="2024-01-15",
        date_end="2024-02-15",
    )
    assert 0 < int(windowed["bars"]) < int(full["bars"])  # type: ignore[call-overload]


def test_unknown_strategy_raises(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_uptrend_csv(data_dir / "SPY.csv")
    with pytest.raises(ValueError, match="unknown strategy"):
        run_named_backtest(
            strategy_name="does_not_exist",
            symbol="SPY",
            data_dir=data_dir,
            policy_path=_permissive_policy(tmp_path / "risk.yaml"),
            initial_cash=3000.0,
            slippage_bps=2.0,
            halt_path=tmp_path / "halt.json",
        )


def test_missing_data_file_raises(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        run_named_backtest(
            strategy_name="baseline_buy_hold",
            symbol="NOPE",
            data_dir=data_dir,
            policy_path=_permissive_policy(tmp_path / "risk.yaml"),
            initial_cash=3000.0,
            slippage_bps=2.0,
            halt_path=tmp_path / "halt.json",
        )


def test_all_registered_strategies_instantiate() -> None:
    for name, factory in STRATEGY_FACTORIES.items():
        strategy = factory()
        assert strategy.version
        assert isinstance(name, str)


def test_current_commit_returns_a_string() -> None:
    # In-repo this is a hash; if git is unavailable it degrades to "unknown".
    assert isinstance(current_commit(), str)
