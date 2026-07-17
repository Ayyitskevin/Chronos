"""Named-strategy backtest runner shared by the CLI and research scripts.

Every run emits a manifest capturing code commit, data hashes, policy hash,
and configuration so results are reproducible (Phase 6 requirement).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from chronos.backtest.engine import BacktestConfig, BacktestResult, run_backtest
from chronos.backtest.metrics import compute_metrics
from chronos.control.halt import HaltStore
from chronos.marketdata.csv_provider import load_daily_csv
from chronos.marketdata.quality import validate_series
from chronos.risk.policy import load_risk_policy
from chronos.strategies.base import Strategy
from chronos.strategies.baselines import (
    BuyAndHoldStrategy,
    DeterministicRandomEntries,
    SmaTrendBaseline,
)
from chronos.strategies.mean_reversion import Rsi2MeanReversionStrategy
from chronos.strategies.regime_trend import MarkovRegimeTrendStrategy

STRATEGY_FACTORIES: dict[str, Callable[[], Strategy]] = {
    "regime_trend_v1": MarkovRegimeTrendStrategy,
    "mean_reversion_v1": Rsi2MeanReversionStrategy,
    "baseline_buy_hold": BuyAndHoldStrategy,
    "baseline_sma_trend": SmaTrendBaseline,
    "baseline_random_entries": DeterministicRandomEntries,
}


def current_commit(repo_root: Path | None = None) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root or Path.cwd(),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except Exception:  # pragma: no cover - git may be absent in deployment
        return "unknown"


def run_named_backtest(
    *,
    strategy_name: str,
    symbol: str,
    data_dir: Path,
    policy_path: Path,
    initial_cash: float,
    slippage_bps: float,
    halt_path: Path | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
) -> dict[str, object]:
    if strategy_name not in STRATEGY_FACTORIES:
        raise ValueError(f"unknown strategy {strategy_name!r}; known: {sorted(STRATEGY_FACTORIES)}")
    csv_path = data_dir / f"{symbol.upper()}.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"no data file {csv_path}")
    loaded = load_daily_csv(csv_path, symbol=symbol.upper(), source="research_raw")
    series = loaded.series
    if date_start or date_end:
        bars = tuple(
            bar
            for bar in series.bars
            if (date_start is None or bar.session_date.isoformat() >= date_start)
            and (date_end is None or bar.session_date.isoformat() <= date_end)
        )
        from chronos.marketdata.bars import BarSeries

        series = BarSeries(symbol=series.symbol, interval=series.interval, bars=bars)
    quality = validate_series(series)
    policy = load_risk_policy(policy_path)
    store = HaltStore(
        halt_path
        if halt_path is not None
        else Path("data") / f"backtest_halt_{strategy_name}_{symbol}.json"
    )
    store.rearm("backtest run: simulated halt store")
    strategy = STRATEGY_FACTORIES[strategy_name]()
    result: BacktestResult = run_backtest(
        series=series,
        strategy=strategy,
        risk_policy=policy,
        config=BacktestConfig(initial_cash_usd=initial_cash, slippage_bps_per_side=slippage_bps),
        halt_store=store,
    )
    metrics = compute_metrics(result)
    return {
        "strategy": strategy_name,
        "strategy_version": strategy.version,
        "symbol": symbol.upper(),
        "bars": len(series),
        "date_range": [
            series[0].session_date.isoformat(),
            series[-1].session_date.isoformat(),
        ]
        if len(series)
        else [],
        "data_sha256": loaded.sha256,
        "data_quality_issues": len(quality.issues),
        "data_quality_blocking": quality.blocking,
        "policy_version": policy.policy_version,
        "policy_hash": policy.config_hash,
        "code_commit": current_commit(),
        "config": {
            "initial_cash_usd": initial_cash,
            "slippage_bps_per_side": slippage_bps,
        },
        "risk_rejections": result.risk_rejections,
        "skipped_conversions": result.skipped_conversions,
        "metrics": asdict(metrics),
    }
