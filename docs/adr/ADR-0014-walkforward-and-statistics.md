# ADR-0014: Walk-forward + statistics upgrade (Milestone C3)

Status: proposed (design-review pending) — **see the status note below**
Date: 2026-07-19

> **Status note, 2026-08-02 (deliberately NOT flipped).** The design this ADR proposes is
> implemented and running: `src/chronos/research/walkforward.py` and
> `src/chronos/research/stats.py` ship the walk-forward harness, the deflated Sharpe ratio
> (threshold 0.95, trial count taken from the registry ledger), and the stationary
> block bootstrap. Unlike ADR-0012 (accepted via `DECISIONS.md` D-14), **this ADR has no
> row in `DECISIONS.md`** — `grep -c "ADR-0014" DECISIONS.md` returns 0 — so its formal
> acceptance is recorded nowhere, and the "design-review pending" clause may be accurate
> rather than stale. An agent cannot resolve this: marking an ADR accepted is an owner act
> (`AGENTS.md` precedence, tier 3). **Owner decision required:** either record acceptance
> in `DECISIONS.md` and flip this line, or run the pending design review. Until then, treat
> the code as authoritative for behavior and this ADR as a proposal it happens to match.

## Context

AI Quant plan C3: a rolling-window walk-forward loop with fixed reporting rules;
statistics scoped to what the samples support — **bar-level stationary/block bootstrap
CIs** (not IID trade-level resampling on autocorrelated series), **deflated Sharpe with
registry-derived trial counts**, **purged/embargoed CV** applied *only* to
fitted/parameter-search workflows, and **low-sample verdicts that stay blocking**. The
plan states the honest bound plainly: at current trade frequencies this upgrade
**formalizes rejection rather than enabling validation** — the binding fix is longer
uniform history (C1 populated) and/or higher-frequency families, not better statistics.

Discovery-confirmed facts that shape the design:

- The engine (`backtest.run_backtest -> BacktestResult`) is **deterministic** and already
  exposes the per-bar `equity_curve` (+ dates) and the `trades` list — the two series a
  bar-level bootstrap and a trade count need. `metrics.compute_metrics` computes
  Sharpe/Sortino/etc. in **stdlib `math`**; there is **no** significance test, bootstrap,
  CI, deflated Sharpe, walk-forward, or CV anywhere in the Python code today.
- The two executable strategies are **fixed-rule replays** — no per-fold parameter fit;
  the Markov transition matrix is accumulated causally bar-by-bar (already walk-forward by
  construction). So there is nothing to purge/embargo for them.
- **scipy is not installed and not a declared dependency**; numpy is only transitive via
  pandas; `metrics.py` is stdlib-only. C3 keeps that: the statistics are **stdlib** (normal
  CDF via `math.erf`; a *seeded* `random.Random` for resampling), adding no hard dep and
  keeping the bootstrap deterministic (CLAUDE.md: no RNG without a recorded seed).
- The C2 registry (`register_run` / `trial_count`) exists but the runner does not yet call
  it. C3's walk-forward loop registers **one trial per configuration**, making the
  multiple-testing N that deflates the Sharpe honest.
- Only **SPY (~20y)** and **QQQ (~24y)** have the daily span for multiple OOS folds; the
  2019–2021 symbols and the empty C1 store do not.

Research-plane, read-only w.r.t. trading: the new code opens no trading DB, holds no
writer lease, and imports no order/broker module (same isolation doctrine as
`registry`/`histdata`).

## Decision

A new `chronos.research` submodule set: `stats.py` (pure statistics), `walkforward.py`
(the loop + verdict), `purged_cv.py` (the fold capability), wired to the C2 registry.

### 1. Statistics — stdlib, deterministic, honest at small n (`research/stats.py`)

- **Probabilistic Sharpe Ratio (PSR).** `psr(sharpe, n_obs, *, benchmark=0.0, skew,
  kurtosis)` — the probability the true (per-observation) Sharpe exceeds `benchmark`,
  given sample length and non-normality:
  `PSR = Φ( (SR − SR*)·√(n−1) / √(1 − γ3·SR + ((γ4−1)/4)·SR²) )`, `Φ` via `math.erf`.
- **Deflated Sharpe Ratio (DSR).** `deflated_sharpe(sharpe, n_obs, *, trial_count,
  trial_sharpe_variance, skew, kurtosis)` — PSR against the **expected maximum** Sharpe of
  `trial_count` independent trials:
  `SR*₀ = √V · [ (1−γ)·Z⁻¹(1 − 1/N) + γ·Z⁻¹(1 − 1/(N·e)) ]`, `γ` = Euler–Mascheroni,
  `Z⁻¹` = inverse normal CDF (Acklam's rational approximation, stdlib), `V` =
  cross-trial Sharpe variance, `N` = `trial_count`. **`N` comes from the registry**, and
  `V` from the variance of the walk-forward's own per-configuration OOS Sharpes (the
  honest, available estimate; §5 discloses that trials from other sessions contribute to
  `N` but not to `V`, so `V` is a current-run estimate).
- **Bar-level stationary block bootstrap.** `block_bootstrap_ci(returns, statistic, *,
  block_size, n_resamples, seed, alpha)` — resamples **blocks of the bar-return series**
  (geometric block lengths, circular wrap) to preserve autocorrelation, returns a
  percentile CI for `statistic` (Sharpe, total return). `seed` is required and recorded;
  IID trade-level resampling is deliberately **not** offered.
- All functions return `None`/a `low_sample` flag rather than a number when `n` is below
  the minimum the statistic supports (e.g. `< 2` returns, `< ~20` trades) — never a
  falsely precise value.

### 2. Walk-forward loop + verdict (`research/walkforward.py`)

`walk_forward(series, strategy_factory, risk_policy, config, *, test_window, step,
warmup, ledger, criteria_ref, seed) -> WalkForwardReport`:

- Rolls a **fixed** out-of-sample window across the series: `[warmup | test_window]`
  advancing by `step`. What is labeled OOS is defined up front (the `test_window`
  segments, disjoint), never chosen after seeing results. For the fixed-rule strategies a
  "fold" is a causal **replay** over the OOS segment after a warm-up prefix (no fit).
- **Registers one trial per (window × configuration)** via `register_run(stage=VALIDATION,
  …)` — so `trial_count` reflects every OOS evaluation that touched data, and the DSR
  deflation is honest rather than self-reported.
- Pools the OOS per-bar returns and trades across folds and computes: total return, Sharpe
  with a **block-bootstrap CI**, PSR, and the **DSR** using `trial_count(ledger,
  strategy_id)` and the cross-fold Sharpe variance.
- **Verdict** `WalkForwardVerdict ∈ {PASS, INSUFFICIENT_EVIDENCE, FAIL}` with
  **INSUFFICIENT_EVIDENCE as the blocking default**: it is returned whenever pooled OOS
  trades `< min_trades` (default 20, the C4 floor) or the Sharpe bootstrap CI includes 0
  or the DSR probability `< 0.95`. `PASS` requires all three to clear; `FAIL` is a
  positive rejection (CI strictly ≤ 0). Low sample never yields PASS.

### 3. Purged/embargoed CV — capability, honestly inactive for fixed-rule (`research/purged_cv.py`)

`purged_kfold(n, *, folds, embargo)` yields train/test index splits with the test fold's
neighborhood **purged** and an **embargo** after it (López de Prado), for workflows that
fit a model or search parameters inside a fold. `requires_purging(strategy_spec) -> bool`
returns **False** for the current fixed-rule strategies (nothing is estimated in-fold), so
`walk_forward` uses the plain rolling OOS replay and records `purged_cv: "not applicable
(fixed-rule replay)"`. The machinery ships tested against synthetic fitted workflows so it
is ready when a parameter search (C4) or a fitted family arrives; it is not applied where
it would be theater.

### 4. CLI (owner-run) + placement

`chronos research walk-forward --strategy S --symbol Y [--test-window N --step M
--min-trades K --seed Z]` runs the loop against `research/data/raw` (or the C1 store when
populated), prints the report + verdict as JSON, and appends the trials to the registry.
Read-only; places no order. New settings for the defaults (`walkforward_test_window_bars`,
`walkforward_min_trades`) follow the existing `Annotated[..., Field(...)]` convention.

## Honesty bounds (report / limitations)

- **This formalizes rejection.** At daily-bar trade counts (tens of trades over 20 years),
  PSR/DSR and the bootstrap CI will almost always return **INSUFFICIENT_EVIDENCE** — that
  is the correct, honest output, not a failure of the tool. The deliverable says so up
  front. The binding fix is longer uniform history (populate the C1 store) or a
  higher-frequency family, not this statistics layer.
- **Purged/embargoed CV does not bind** for the current fixed-rule strategies — shipped as
  capability, reported as not-applicable rather than run as decoration.
- **DSR is an estimate.** `N` (trial count) is exact from the registry, but the cross-trial
  Sharpe variance `V` is estimated from the current run's folds (other-session trials
  contribute to `N`, not `V`); the report states this. When `V` cannot be estimated (fewer
  than two defined window Sharpes, i.e. a single OOS window) while `N > 1`, the deflated
  Sharpe is reported as **`null` (INSUFFICIENT_EVIDENCE)** rather than an *undeflated* PSR —
  the multiple-testing penalty is never silently skipped under the `deflated_sharpe` name
  (C3 review finding 1). A single registered trial (`N <= 1`) needs no deflation and reports
  the PSR directly. The stdlib inverse-normal is Acklam's approximation (**relative** error
  ≈1e-9; absolute ≈5e-9 in the deep tails), disclosed.
- **The trade floor is a validated safety gate.** `min_trades` must be `>= 1`; `walk_forward`
  rejects a sub-1 floor and `_verdict` additionally clamps it, so "low sample never PASSes"
  cannot be defeated by a `0`/negative floor (C3 review finding 2).
- **Data heterogeneity carries over** (ADR-0006): walk-forward runs honestly only on the
  long uniform series (SPY/QQQ); the 3-year 2019–2021 symbols are too short for daily
  folds and are excluded with a recorded reason, not silently run.
- **No new dependency, seeded determinism.** Statistics are stdlib; every bootstrap seed is
  recorded so a run reproduces byte-for-byte.

## What proves it

- `stats.py`: PSR/DSR pinned against hand-computed values and known limits (PSR→0.5 at
  SR=benchmark; DSR falls as `N` rises); the block bootstrap is deterministic under a fixed
  seed and its CI widens as block size grows; small-n returns `None`/low_sample.
- `walkforward.py`: OOS windows are disjoint and fixed up front; a run registers exactly
  one trial per configuration (asserted against `trial_count`); a thin-sample run returns
  **INSUFFICIENT_EVIDENCE** and never PASS; determinism (same seed → identical report).
- `purged_cv.py`: purged folds exclude the purge+embargo neighborhood; `requires_purging`
  is False for the fixed-rule specs; `walk_forward` records "not applicable" for them.
- Isolation: `chronos.research` walk-forward code imports no order/broker/persistence
  module (extends the research-plane isolation tests).

## Consequences

Research gains an honest walk-forward + statistics layer whose default verdict at current
sample sizes is a **blocking "insufficient evidence,"** with a deflated Sharpe that counts
every registered trial and bootstrap CIs that respect autocorrelation. It changes nothing
in the trading/live plane; it adds no hard dependency; and it is explicit that at daily
frequency it mostly rejects — the value is a rigorous, reproducible rejection instead of a
falsely confident number. C4's re-validation campaign consumes these statistics; a fitted
or higher-frequency family later activates the purged-CV path.
