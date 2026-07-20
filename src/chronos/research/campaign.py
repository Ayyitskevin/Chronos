"""Re-validation campaign over the C3 walk-forward (AI Quant plan C4, ADR-0015).

Runs the sample-honest walk-forward (``research.walkforward``) for each
(strategy x symbol) cell under a **trade-permitting research policy**, on the dev+val
span only (the 2022+ holdout stays sealed behind the C2 guardian), registering exactly
one VALIDATION trial per cell in the C2 registry so the deflated Sharpe's multiple-testing
N is real and cumulative. It returns a reproducible verdict table.

Honest by construction: cells whose sliced series is too short for a warm-up plus two
disjoint OOS windows are **excluded with a recorded reason**, never silently skipped; a
cell that errors mid-run is recorded and skipped (the grid is not aborted); and no bar
dated on or after ``FINAL_START`` influences any statistic or the recorded provenance
fingerprint -- the slice discards it and the fingerprint covers only the sliced window
(the CSV loader still parses the whole file, but nothing past the wall reaches a result).
A caller that asks for a stage_end reaching into the holdout is refused (fail closed). At
daily-bar trade counts the table is expected to be dominated by INSUFFICIENT_EVIDENCE /
FAIL; that is the correct output, not a tool failure.

Research-plane and read-only: it drives the deterministic backtest (simulated broker) via
the tested ``walk_forward`` and imports no order/broker module
(``tests/safety/test_research_isolation.py``).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from chronos.backtest.engine import BacktestConfig
from chronos.control.halt import HaltStore
from chronos.marketdata.bars import BarSeries
from chronos.marketdata.csv_provider import load_daily_csv
from chronos.registry import RegistryLedger
from chronos.research.runner import STRATEGY_FACTORIES, current_commit
from chronos.research.walkforward import WalkForwardReport, walk_forward
from chronos.risk.policy import RiskPolicy

# The reserved one-shot holdout wall (mirrors scripts/run_research.py FINAL_START). The
# campaign never lets a bar on or after this date influence a statistic or a fingerprint; a
# holdout read is a separate, owner-typed, guardian-mediated event (C2). This is exactly the
# window whose burn M5 flagged. A date object so comparisons are chronological, not lexical.
FINAL_START = date(2022, 1, 1)
DEFAULT_STAGE_END = "2021-12-31"


@dataclass(frozen=True, slots=True)
class VerdictRow:
    strategy_id: str
    symbol: str
    windows: int
    pooled_trades: int
    pooled_sharpe: float | None
    sharpe_ci: tuple[float, float] | None
    psr: float | None
    deflated_sharpe: float | None
    trial_count: int
    verdict: str
    reason: str


@dataclass(frozen=True, slots=True)
class CampaignCell:
    strategy_id: str
    symbol: str
    report: WalkForwardReport | None
    excluded_reason: str | None
    data_fingerprint: str | None = None  # holdout-free sha of the sliced window (None if no run)


@dataclass(frozen=True, slots=True)
class CampaignReport:
    policy_version: str
    policy_hash: str
    stage_end: str
    seed: int
    code_commit: str
    warmup: int
    test_window: int
    min_trades: int
    cells: tuple[CampaignCell, ...]
    verdict_table: tuple[VerdictRow, ...]
    excluded: tuple[tuple[str, str], ...]  # symbol-level: (symbol, reason) — applies to all
    errored: tuple[tuple[str, str, str], ...]  # cell-level: (strategy_id, symbol, reason)


def _parse_stage_end(stage_end: str) -> date:
    """Parse the dev+val wall as an ISO date; reject non-ISO input (fail closed)."""

    try:
        return date.fromisoformat(stage_end)
    except ValueError as error:
        raise ValueError(
            f"stage_end must be an ISO date (YYYY-MM-DD), got {stage_end!r}"
        ) from error


def _slice_to_cutoff(series: BarSeries, cutoff: date) -> BarSeries:
    bars = tuple(bar for bar in series.bars if bar.session_date <= cutoff)
    return BarSeries(symbol=series.symbol, interval=series.interval, bars=bars)


def _window_fingerprint(series: BarSeries) -> str:
    """Deterministic content hash of exactly the (sliced) bars -- no holdout bytes."""

    digest = hashlib.sha256()
    for bar in series.bars:
        digest.update(
            f"{bar.session_date.isoformat()},{bar.open},{bar.high},"
            f"{bar.low},{bar.close},{bar.volume}\n".encode()
        )
    return digest.hexdigest()


def _cell_config_hash(
    strategy_id: str,
    symbol: str,
    policy_hash: str,
    config: BacktestConfig,
    warmup: int,
    test_window: int,
    min_trades: int,
    block_size: int,
    n_resamples: int,
    seed: int,
) -> str:
    payload = {
        "strategy_id": strategy_id,
        "symbol": symbol,
        "policy_hash": policy_hash,
        "config": dataclasses.asdict(config),  # the full backtest config, not a subset
        "warmup": warmup,
        "test_window": test_window,
        "min_trades": min_trades,
        "block_size": block_size,
        "n_resamples": n_resamples,
        "seed": seed,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def run_campaign(
    *,
    strategies: Sequence[str],
    symbols: Sequence[str],
    data_dir: Path,
    policy: RiskPolicy,
    ledger: RegistryLedger,
    halt_dir: Path,
    config: BacktestConfig,
    criteria_ref: str = "docs/adr/ADR-0015-revalidation-campaign.md",
    stage_end: str = DEFAULT_STAGE_END,
    warmup: int = 252,
    test_window: int = 252,
    min_trades: int = 20,
    seed: int = 0,
    block_size: int = 20,
    n_resamples: int = 1000,
) -> CampaignReport:
    """Run the (strategy x symbol) walk-forward grid on the dev+val span; sealed holdout."""

    # Fail closed: reject a non-ISO or holdout-reaching stage_end before touching data.
    cutoff = _parse_stage_end(stage_end)
    if cutoff >= FINAL_START:
        raise ValueError(
            f"stage_end {stage_end!r} reaches the reserved holdout (>= {FINAL_START.isoformat()}); "
            "a holdout read must go through the C2 guardian, not the campaign"
        )
    if not strategies:
        raise ValueError("strategies must be non-empty")
    if not symbols:
        raise ValueError("symbols must be non-empty")
    unknown = [s for s in strategies if s not in STRATEGY_FACTORIES]
    if unknown:
        raise ValueError(f"unknown strategies {unknown}; known: {sorted(STRATEGY_FACTORIES)}")

    # The registry rejects null provenance; surface that as a clear campaign-level error
    # up front rather than aborting deep inside the first cell's register_run.
    code_commit = current_commit()
    if code_commit in {"", "unknown"}:
        raise ValueError(
            "campaign requires a resolvable git commit for provenance "
            "(the registry rejects null provenance); run from a git checkout"
        )

    halt_dir.mkdir(parents=True, exist_ok=True)
    min_bars = warmup + 2 * test_window

    # Load + slice each symbol once; fingerprint the sliced window (holdout-free). Record
    # why any symbol is unusable (no silent skips).
    sliced: dict[str, tuple[BarSeries, str]] = {}
    symbol_exclusions: list[tuple[str, str]] = []
    for symbol in sorted({s.upper() for s in symbols}):
        path = data_dir / f"{symbol}.csv"
        if not path.exists():
            symbol_exclusions.append((symbol, f"no data file {path}"))
            continue
        loaded = load_daily_csv(path, symbol=symbol, source="research_raw")
        window = _slice_to_cutoff(loaded.series, cutoff)
        if len(window) < min_bars:
            symbol_exclusions.append(
                (
                    symbol,
                    f"only {len(window)} bars <= {stage_end} "
                    f"(< warmup+2*test_window = {min_bars}); too short for multiple OOS folds",
                )
            )
            continue
        sliced[symbol] = (window, _window_fingerprint(window))

    # Fixed iteration order (strategy-major, symbol-minor, both sorted) so the verdict
    # table -- and each cell's cumulative trial count -- is deterministic. Each cell's DSR
    # is deflated against the trial count at the moment it is evaluated (running cumulative
    # N); re-running the campaign deflates further, by design (ADR-0015). A cell that raises
    # is recorded and skipped so one bad symbol cannot abort the whole grid.
    cells: list[CampaignCell] = []
    errored: list[tuple[str, str, str]] = []
    for strategy_id in sorted(set(strategies)):
        for symbol in sorted(sliced):
            window, fingerprint = sliced[symbol]
            config_hash = _cell_config_hash(
                strategy_id,
                symbol,
                policy.config_hash,
                config,
                warmup,
                test_window,
                min_trades,
                block_size,
                n_resamples,
                seed,
            )
            halt_store = HaltStore(halt_dir / f"campaign_halt_{strategy_id}_{symbol}.json")
            halt_store.rearm("campaign run: simulated halt store")
            try:
                report = walk_forward(
                    window,
                    STRATEGY_FACTORIES[strategy_id],
                    policy,
                    config,
                    halt_store=halt_store,
                    ledger=ledger,
                    strategy_id=strategy_id,
                    config_hash=config_hash,
                    code_commit=code_commit,
                    data_hashes={symbol: {"window_sha256": fingerprint, "stage_end": stage_end}},
                    criteria_ref=criteria_ref,
                    test_window=test_window,
                    warmup=warmup,
                    min_trades=min_trades,
                    seed=seed,
                    block_size=block_size,
                    n_resamples=n_resamples,
                )
            except Exception as error:  # one bad cell must not abort the grid
                reason = f"cell error: {type(error).__name__}: {error}"
                errored.append((strategy_id, symbol, reason))
                cells.append(CampaignCell(strategy_id, symbol, None, reason, fingerprint))
                continue
            cells.append(CampaignCell(strategy_id, symbol, report, None, fingerprint))

    for symbol, reason in symbol_exclusions:
        for strategy_id in sorted(set(strategies)):
            cells.append(CampaignCell(strategy_id, symbol, None, reason))

    verdict_table = tuple(
        VerdictRow(
            strategy_id=cell.report.strategy_id,
            symbol=cell.report.symbol,
            windows=len(cell.report.windows),
            pooled_trades=cell.report.pooled_trades,
            pooled_sharpe=cell.report.pooled_sharpe,
            sharpe_ci=cell.report.sharpe_ci,
            psr=cell.report.psr,
            deflated_sharpe=cell.report.deflated_sharpe,
            trial_count=cell.report.trial_count,
            verdict=str(cell.report.verdict),
            reason=cell.report.reason,
        )
        for cell in cells
        if cell.report is not None
    )

    return CampaignReport(
        policy_version=policy.policy_version,
        policy_hash=policy.config_hash,
        stage_end=stage_end,
        seed=seed,
        code_commit=code_commit,
        warmup=warmup,
        test_window=test_window,
        min_trades=min_trades,
        cells=tuple(cells),
        verdict_table=verdict_table,
        excluded=tuple(symbol_exclusions),
        errored=tuple(errored),
    )
