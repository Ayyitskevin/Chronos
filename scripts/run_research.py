"""Phase 6 quantitative validation harness.

Chronological discipline:

- development window:  ..2017-12-31 (in-sample exploration)
- validation window:   2018-01-01..2021-12-31 (walk-forward + stress)
- final test window:   2022-01-01.. (touched ONCE, after candidates and
  parameters are frozen; the run records that ordering)

For every candidate and baseline, per symbol: net results under base costs;
slippage stress (2/5/10/25 bps per side); commission stress (1x/2x);
parameter sensitivity for load-bearing parameters; plus a portfolio
correlation check between the two candidates. Everything runs through the
production backtest engine (same risk engine, same execution path) with a
recorded research risk policy.

Outputs: research/results/<name>.json + research/results/SUMMARY.json.

Usage: .venv/bin/python scripts/run_research.py [--stage dev|val|final|all]

``--stage all`` runs dev + val only. The reserved final window is consumed
exclusively by an explicit ``--stage final`` (one-shot discipline).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from chronos.backtest.engine import BacktestConfig, run_backtest  # noqa: E402
from chronos.backtest.metrics import compute_metrics  # noqa: E402
from chronos.control.halt import HaltStore  # noqa: E402
from chronos.marketdata.bars import BarSeries  # noqa: E402
from chronos.marketdata.csv_provider import load_daily_csv  # noqa: E402
from chronos.marketdata.quality import validate_series  # noqa: E402
from chronos.risk.policy import RiskPolicy  # noqa: E402
from chronos.strategies.base import Strategy  # noqa: E402
from chronos.strategies.baselines import (  # noqa: E402
    BuyAndHoldStrategy,
    DeterministicRandomEntries,
    SmaTrendBaseline,
)
from chronos.strategies.mean_reversion import (  # noqa: E402
    MeanReversionParams,
    Rsi2MeanReversionStrategy,
)
from chronos.strategies.regime_trend import (  # noqa: E402
    MarkovRegimeTrendStrategy,
    RegimeTrendParams,
)

DATA_DIR = ROOT / "research" / "data" / "raw"
RESULTS_DIR = ROOT / "research" / "results"
SYMBOLS = ("SPY", "QQQ", "IWM", "DIA", "GLD", "TLT")

DEV_END = "2017-12-31"
VAL_START, VAL_END = "2018-01-01", "2021-12-31"
FINAL_START = "2022-01-01"

RESEARCH_POLICY = RiskPolicy(
    policy_version="research-1",
    allowed_symbols=SYMBOLS,
    allowed_strategy_ids=(
        "regime_trend_v1",
        "mean_reversion_v1",
        "baseline_buy_hold",
        "baseline_sma_trend",
        "baseline_random_entries",
    ),
    allow_long_entries=True,
    # Research latitude: caps must not distort long-horizon compounding
    # measurement; production policies re-impose real caps.
    max_bot_capital_usd=10_000_000,
    max_position_notional_usd=10_000_000,
    max_aggregate_exposure_usd=10_000_000,
    max_symbol_exposure_fraction=1.0,
    max_risk_per_trade_fraction=0.50,
    max_simultaneous_positions=1,
    max_open_orders=1,
    max_daily_loss_usd=3000,
    max_weekly_loss_usd=3000,
    max_drawdown_fraction=0.95,
    max_consecutive_losses=1000,
    max_quote_age_seconds=6 * 86400,
    max_bar_age_seconds=6 * 86400,
    max_price_deviation_fraction=0.05,
    max_order_rejections_per_day=100,
    allow_overnight_positions=True,
)

BASE = BacktestConfig(initial_cash_usd=3000.0, slippage_bps_per_side=2.0)


def slice_series(series: BarSeries, start: str | None, end: str | None) -> BarSeries:
    bars = tuple(
        bar
        for bar in series.bars
        if (start is None or bar.session_date.isoformat() >= start)
        and (end is None or bar.session_date.isoformat() <= end)
    )
    return BarSeries(symbol=series.symbol, interval=series.interval, bars=bars)


def make_strategy(name: str, variant: dict[str, float] | None = None) -> Strategy:
    if name == "regime_trend_v1":
        params = RegimeTrendParams(**variant) if variant else None
        return MarkovRegimeTrendStrategy(params)
    if name == "mean_reversion_v1":
        params_mr = MeanReversionParams(**variant) if variant else None
        return Rsi2MeanReversionStrategy(params_mr)
    if name == "baseline_buy_hold":
        return BuyAndHoldStrategy()
    if name == "baseline_sma_trend":
        return SmaTrendBaseline()
    if name == "baseline_random_entries":
        return DeterministicRandomEntries()
    raise ValueError(name)


def run_one(
    series: BarSeries,
    strategy_name: str,
    config: BacktestConfig,
    tag: str,
    variant: dict[str, float] | None = None,
) -> dict[str, object]:
    halt = HaltStore(
        Path("/tmp/claude-research-halt") / f"{strategy_name}_{series.symbol}_{tag}.json"
    )
    halt.rearm("research run")
    result = run_backtest(
        series=series,
        strategy=make_strategy(strategy_name, variant),
        risk_policy=RESEARCH_POLICY,
        config=config,
        halt_store=halt,
    )
    metrics = compute_metrics(result)
    daily_returns: list[float] = []
    curve = result.equity_curve
    for i in range(1, len(curve)):
        daily_returns.append(curve[i] / curve[i - 1] - 1.0 if curve[i - 1] > 0 else 0.0)
    return {
        "tag": tag,
        "strategy": strategy_name,
        "variant": variant or {},
        "symbol": series.symbol,
        "bars": len(series),
        "start": series[0].session_date.isoformat() if len(series) else None,
        "end": series[-1].session_date.isoformat() if len(series) else None,
        "config": asdict(config),
        "risk_rejections": result.risk_rejections,
        "metrics": asdict(metrics),
        "daily_returns": daily_returns,
    }


def correlation(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 30:
        return None
    xs, ys = a[-n:], b[-n:]
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx**0.5 * vy**0.5)


SENSITIVITY: dict[str, list[dict[str, float]]] = {
    "regime_trend_v1": [
        {"lookback": 15},
        {"lookback": 25},
        {"enter_z_base": 0.70},
        {"enter_z_base": 1.00},
        {"markov_min_stay": 0.55},
        {"markov_min_stay": 0.65},
        {"ema_filter_len": 50},
        {"ema_filter_len": 150},
        {"atr_stop_mult": 1.5},
        {"atr_stop_mult": 3.0},
    ],
    "mean_reversion_v1": [
        {"oversold": 5.0},
        {"oversold": 15.0},
        {"exit_level": 60.0},
        {"time_stop_bars": 5.0},
        {"time_stop_bars": 15.0},
        {"fail_atr_mult": 1.0},
        {"fail_atr_mult": 2.5},
        {"ema_filter_len": 50.0},
    ],
}


def _int_fields(name: str, variant: dict[str, float]) -> dict[str, float]:
    ints = {
        "regime_trend_v1": {
            "lookback",
            "confirm_bars",
            "ema_filter_len",
            "vol_percentile_len",
            "markov_min_row_n",
            "atr_len",
        },
        "mean_reversion_v1": {"rsi_len", "ema_filter_len", "atr_len", "time_stop_bars"},
    }[name]
    return {k: (int(v) if k in ints else v) for k, v in variant.items()}


def main() -> int:
    parser = argparse.ArgumentParser()
    # "all" deliberately EXCLUDES the final stage: the reserved holdout must
    # only ever be computed by an explicit `--stage final`, so it cannot be
    # consumed as a side effect of a routine re-run (M5 review finding — this
    # is exactly how QQQ's 2022-2024 holdout got burned).
    parser.add_argument("--stage", choices=["dev", "val", "final", "all"], default="all")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    candidates = ["regime_trend_v1", "mean_reversion_v1"]
    baselines = ["baseline_buy_hold", "baseline_sma_trend", "baseline_random_entries"]

    all_series: dict[str, BarSeries] = {}
    data_notes: dict[str, object] = {}
    for symbol in SYMBOLS:
        path = DATA_DIR / f"{symbol}.csv"
        if not path.exists():
            data_notes[symbol] = "MISSING — not acquired; symbol excluded"
            continue
        loaded = load_daily_csv(path, symbol=symbol, source="research_raw")
        report = validate_series(loaded.series)
        if report.blocking:
            data_notes[symbol] = f"EXCLUDED — blocking data-quality issues: {report.issues[:3]}"
            continue
        all_series[symbol] = loaded.series
        data_notes[symbol] = {
            "sha256": loaded.sha256,
            "bars": len(loaded.series),
            "start": loaded.series[0].session_date.isoformat(),
            "end": loaded.series[-1].session_date.isoformat(),
            "quality_issues_nonblocking": len(report.issues),
        }

    stages: list[tuple[str, str | None, str | None]] = []
    if args.stage in ("dev", "all"):
        stages.append(("dev", None, DEV_END))
    if args.stage in ("val", "all"):
        stages.append(("val", VAL_START, VAL_END))
    if args.stage == "final":  # never implied by "all"; explicit consumption only
        stages.append(("final", FINAL_START, None))

    runs: list[dict[str, object]] = []
    for stage_name, start, end in stages:
        for _symbol, series in all_series.items():
            window = slice_series(series, start, end)
            if len(window) < 300:
                continue
            for name in candidates + baselines:
                runs.append(run_one(window, name, BASE, stage_name))
            if stage_name == "val":
                for bps in (5.0, 10.0, 25.0):
                    for name in candidates:
                        runs.append(
                            run_one(
                                window,
                                name,
                                replace(BASE, slippage_bps_per_side=bps),
                                f"val_slip{int(bps)}",
                            )
                        )
                for name in candidates:
                    runs.append(
                        run_one(
                            window,
                            name,
                            replace(BASE, commission_minimum=2.0, commission_per_share=0.01),
                            "val_comm2x",
                        )
                    )
                for name in candidates:
                    for raw_variant in SENSITIVITY[name]:
                        runs.append(
                            run_one(
                                window,
                                name,
                                BASE,
                                "val_sens",
                                _int_fields(name, raw_variant),
                            )
                        )

    # Candidate return correlation on validation windows, per symbol.
    correlations: dict[str, float | None] = {}
    for symbol in all_series:
        a = next(
            (
                r["daily_returns"]
                for r in runs
                if r["tag"] == "val"
                and r["strategy"] == "regime_trend_v1"
                and r["symbol"] == symbol
            ),
            None,
        )
        b = next(
            (
                r["daily_returns"]
                for r in runs
                if r["tag"] == "val"
                and r["strategy"] == "mean_reversion_v1"
                and r["symbol"] == symbol
            ),
            None,
        )
        if a is not None and b is not None:
            correlations[symbol] = correlation(list(a), list(b))

    for run in runs:
        del run["daily_returns"]  # keep result files compact; curves reproducible

    summary = {
        "policy": RESEARCH_POLICY.model_dump(),
        "policy_hash": RESEARCH_POLICY.config_hash,
        "partitions": {
            "development_end": DEV_END,
            "validation": [VAL_START, VAL_END],
            "final_test_start": FINAL_START,
        },
        "data": data_notes,
        "candidate_return_correlation_validation": correlations,
        "run_count": len(runs),
        "runs": runs,
    }
    out = RESULTS_DIR / f"research_{args.stage}.json"
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out} with {len(runs)} runs over {sorted(all_series)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
