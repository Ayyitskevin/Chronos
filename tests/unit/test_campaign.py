"""Re-validation campaign harness (C4, ADR-0015).

Structural + honesty properties on a hermetic synthetic CSV: the verdict table matches the
per-cell walk-forward reports, exactly one trial is registered per cell, exclusions carry a
recorded reason (missing file, too-short series), the run is deterministic under a fixed
seed, and the 2022+ holdout is structurally sealed (a stage_end reaching it is refused).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from chronos.backtest.engine import BacktestConfig
from chronos.marketdata.bars import BarSeries
from chronos.marketdata.csv_provider import load_daily_csv
from chronos.registry import RegistryLedger, trial_count
from chronos.research.campaign import (
    FINAL_START,
    CampaignReport,
    _slice_to_cutoff,
    _window_fingerprint,
    run_campaign,
)
from chronos.risk.policy import load_risk_policy

REPO_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_POLICY_PATH = REPO_ROOT / "config" / "risk.research.yaml"
WARMUP = 20
TEST_WINDOW = 20
MIN_BARS = WARMUP + 2 * TEST_WINDOW  # 60


def _write_csv(data_dir: Path, symbol: str, n_bars: int, start: date = date(2005, 1, 3)) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    lines = ["date,open,high,low,close,volume"]
    session = start
    prev_close = 100.0
    for i in range(n_bars):
        while session.weekday() >= 5:
            session += timedelta(days=1)
        close = 100.0 + 0.5 * i
        open_ = prev_close
        high = max(open_, close) + 1.0
        low = min(open_, close) - 1.0
        lines.append(f"{session.isoformat()},{open_},{high},{low},{close},1000000")
        prev_close = close
        session += timedelta(days=1)
    (data_dir / f"{symbol}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _config() -> BacktestConfig:
    return BacktestConfig(initial_cash_usd=3000.0, slippage_bps_per_side=0.0)


def _run(
    data_dir: Path, ledger_path: Path, halt_dir: Path, seed: int, symbols: list[str]
) -> CampaignReport:
    return run_campaign(
        strategies=["baseline_buy_hold"],
        symbols=symbols,
        data_dir=data_dir,
        policy=load_risk_policy(RESEARCH_POLICY_PATH),
        ledger=RegistryLedger(ledger_path),
        halt_dir=halt_dir,
        config=_config(),
        stage_end="2021-12-31",
        warmup=WARMUP,
        test_window=TEST_WINDOW,
        min_trades=20,
        seed=seed,
        block_size=5,
        n_resamples=100,
    )


def test_campaign_runs_registers_one_trial_and_table_matches_cells(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_csv(data, "SPY", 120)
    ledger_path = tmp_path / "reg.jsonl"
    report = _run(data, ledger_path, tmp_path / "halts", seed=7, symbols=["SPY"])

    assert report.policy_version == "research-1"
    assert len(report.verdict_table) == 1
    row = report.verdict_table[0]
    assert row.symbol == "SPY" and row.strategy_id == "baseline_buy_hold"

    # The verdict table row is derived from the cell's walk-forward report.
    cell = next(c for c in report.cells if c.report is not None)
    assert cell.report is not None
    assert row.verdict == str(cell.report.verdict)
    assert row.trial_count == cell.report.trial_count

    # Exactly one VALIDATION trial recorded for this cell; the report reads it back.
    assert trial_count(RegistryLedger(ledger_path), strategy_id="baseline_buy_hold") == 1
    assert row.trial_count == 1


def test_campaign_is_deterministic_under_a_fixed_seed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_csv(data, "SPY", 120)
    a = _run(data, tmp_path / "a.jsonl", tmp_path / "ha", seed=11, symbols=["SPY"])
    b = _run(data, tmp_path / "b.jsonl", tmp_path / "hb", seed=11, symbols=["SPY"])
    # Fresh ledgers → identical N → byte-identical verdict table (registry ids/timestamps
    # do not enter the report).
    assert a.verdict_table == b.verdict_table


def test_missing_and_too_short_symbols_are_excluded_with_a_reason(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_csv(data, "SPY", 120)  # runs
    _write_csv(data, "SHORT", MIN_BARS - 5)  # too short for 2 OOS windows
    report = _run(
        data, tmp_path / "reg.jsonl", tmp_path / "h", seed=1, symbols=["SPY", "SHORT", "DIA"]
    )

    excluded = dict(report.excluded)
    assert "DIA" in excluded and "no data file" in excluded["DIA"]
    assert "SHORT" in excluded and "too short" in excluded["SHORT"]
    # No verdict row for an excluded symbol; the one that ran is present.
    ran = {row.symbol for row in report.verdict_table}
    assert ran == {"SPY"}


def test_campaign_refuses_a_stage_end_reaching_the_holdout(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_csv(data, "SPY", 120)
    with pytest.raises(ValueError, match="holdout"):
        run_campaign(
            strategies=["baseline_buy_hold"],
            symbols=["SPY"],
            data_dir=data,
            policy=load_risk_policy(RESEARCH_POLICY_PATH),
            ledger=RegistryLedger(tmp_path / "reg.jsonl"),
            halt_dir=tmp_path / "h",
            config=_config(),
            stage_end=FINAL_START.isoformat(),  # exactly the holdout wall
            warmup=WARMUP,
            test_window=TEST_WINDOW,
        )


def test_campaign_reads_no_bar_past_the_holdout_wall(tmp_path: Path) -> None:
    # A CSV that spans into 2022+ must contribute no bar at/after FINAL_START: the slice to
    # stage_end (2021-12-31) seals the holdout, so every OOS window ends before the wall.
    data = tmp_path / "data"
    _write_csv(data, "SPY", 300, start=date(2021, 6, 1))  # runs into 2022
    report = _run(data, tmp_path / "reg.jsonl", tmp_path / "h", seed=3, symbols=["SPY"])
    wall = FINAL_START
    cell = next(c for c in report.cells if c.report is not None)
    assert cell.report is not None
    assert cell.report.windows, "expected at least one OOS window"
    assert all(w.end < wall for w in cell.report.windows)


def test_campaign_rejects_unknown_strategy(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_csv(data, "SPY", 120)
    with pytest.raises(ValueError, match="unknown"):
        run_campaign(
            strategies=["not_a_strategy"],
            symbols=["SPY"],
            data_dir=data,
            policy=load_risk_policy(RESEARCH_POLICY_PATH),
            ledger=RegistryLedger(tmp_path / "reg.jsonl"),
            halt_dir=tmp_path / "h",
            config=_config(),
        )


# --- review remediation (robustness + provenance honesty) --------------------------------


def test_campaign_rejects_a_non_iso_stage_end(tmp_path: Path) -> None:
    # Finding 5: a non-ISO stage_end must fail closed, not silently yield an empty campaign.
    data = tmp_path / "data"
    _write_csv(data, "SPY", 120)
    with pytest.raises(ValueError, match="ISO date"):
        run_campaign(
            strategies=["baseline_buy_hold"],
            symbols=["SPY"],
            data_dir=data,
            policy=load_risk_policy(RESEARCH_POLICY_PATH),
            ledger=RegistryLedger(tmp_path / "reg.jsonl"),
            halt_dir=tmp_path / "h",
            config=_config(),
            stage_end="not-a-date",
        )


def test_campaign_refuses_a_null_git_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Finding 1: a null/unknown provenance commit must raise a clear campaign-level error
    # up front, not abort deep inside the first cell's register_run.
    data = tmp_path / "data"
    _write_csv(data, "SPY", 120)
    monkeypatch.setattr("chronos.research.campaign.current_commit", lambda: "unknown")
    with pytest.raises(ValueError, match="commit"):
        _run(data, tmp_path / "reg.jsonl", tmp_path / "h", seed=1, symbols=["SPY"])


def test_a_cell_error_is_recorded_and_does_not_abort_the_grid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Finding 2: one failing cell must be recorded and skipped, not abort the whole grid.
    data = tmp_path / "data"
    _write_csv(data, "SPY", 120)
    _write_csv(data, "BAD", 120)

    from chronos.research.walkforward import walk_forward as real_walk_forward

    def selective(series: BarSeries, *args: object, **kwargs: object) -> object:
        if series.symbol == "BAD":
            raise RuntimeError("synthetic cell failure")
        return real_walk_forward(series, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("chronos.research.campaign.walk_forward", selective)
    report = _run(data, tmp_path / "reg.jsonl", tmp_path / "h", seed=1, symbols=["SPY", "BAD"])

    # SPY still produced a verdict row; BAD's failure is recorded, not raised.
    assert {row.symbol for row in report.verdict_table} == {"SPY"}
    assert any("cell error" in reason for _, reason in report.excluded)
    errored = [c for c in report.cells if c.excluded_reason and "cell error" in c.excluded_reason]
    assert len(errored) == 1 and errored[0].symbol == "BAD"


def test_provenance_fingerprint_excludes_holdout_bars(tmp_path: Path) -> None:
    # Finding 3: the recorded provenance fingerprint must be independent of any bar past the
    # wall — holdout bytes never enter a recorded hash, even though the loader parses them.
    data = tmp_path / "data"
    _write_csv(data, "SPY", 300, start=date(2021, 6, 1))  # spans into 2022
    full = load_daily_csv(data / "SPY.csv", symbol="SPY", source="research_raw").series
    cutoff = date(2021, 12, 31)

    post = [b for b in full.bars if b.session_date > cutoff]
    assert post, "fixture must contain holdout bars to make this meaningful"
    only_pre = BarSeries(
        symbol=full.symbol,
        interval=full.interval,
        bars=tuple(b for b in full.bars if b.session_date <= cutoff),
    )
    # Fingerprinting the sliced full series == fingerprinting a pre-cutoff-only series:
    # the bars past the wall contribute nothing to the recorded provenance hash.
    assert _window_fingerprint(_slice_to_cutoff(full, cutoff)) == _window_fingerprint(only_pre)
