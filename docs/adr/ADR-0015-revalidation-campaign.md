# ADR-0015: Re-validation campaign (Milestone C4)

Status: proposed (design-review pending)
Date: 2026-07-19

## Context

AI Quant plan C4: run the C3 walk-forward + sample-honest statistics across the strategy
families and the long uniform series, accumulate the multiple-testing trial count in the
C2 registry, and produce a **reproducible verdict table**. This is the first real consumer
of C2 (registry) and C3 (walk-forward, PSR/DSR/bootstrap) together.

Discovery-confirmed facts that shape the design:

- **The example risk policy trades nothing by design.** `config/risk.example.yaml` is a
  deny-by-default template (`allowed_symbols: []`, `allow_long_entries: false`, all caps
  `0`). Every strategy on every symbol produces **0 trades** under it — `baseline_buy_hold`
  is risk-rejected on all 5000 SPY bars. That is a *config* fact, not a research result, and
  the C3 walk-forward CLI defaults to it (the safe, fail-closed default). A campaign must run
  under a **trade-permitting research policy**.
- **A vetted research policy already exists**, inline in `scripts/run_research.py`
  (`RESEARCH_POLICY`, `policy_version="research-1"`): generous caps so long-horizon
  compounding is measured without distortion, `allowed_symbols = SPY,QQQ,IWM,DIA,GLD,TLT`,
  the five strategy ids, `allow_long_entries`, overnight allowed. Under it the candidates
  trade: regime_trend_v1 = 99 (SPY)/120 (QQQ); mean_reversion_v1 = 87/106; baselines fewer.
  It is an inline constant today — not importable, not hash-reviewable as a file.
- **Only SPY (~2000–2019) and QQQ (~1999–2024) have the daily span for multiple OOS folds.**
  IWM/GLD/TLT are 2019–2021 (757 bars) and DIA is absent from `research/data/raw`. The
  short series cannot support a warm-up plus several disjoint test windows.
- **The reserved final/holdout window is 2022-01-01.. .** `run_research.py` already enforces
  one-shot discipline: `--stage all` deliberately EXCLUDES `final`, because that is exactly
  how QQQ's 2022–2024 holdout got burned once (M5 finding). C4 must honor the same wall and
  route any holdout read through the C2 guardian, never as a campaign side effect.
- **The Sharpes are weak** (per-run 0.08–0.39, one negative). Combined with tens of trades
  over 20 years, the honest expected outcome is a table dominated by INSUFFICIENT_EVIDENCE
  and FAIL. A PASS at daily frequency would itself warrant scrutiny.

Research-plane, read-only w.r.t. trading: the campaign drives the deterministic backtest
(simulated broker), opens no trading DB, holds no writer lease, imports no order/broker
module — same isolation doctrine as C3, reusing C3's tested `walk_forward`.

## Decision

A campaign harness `chronos.research.campaign` + a checked-in research policy file + a CLI
subcommand, producing a verdict table registered in the C2 ledger.

### 1. A checked-in research risk policy (`config/risk.research.yaml`)

Promote the inline `RESEARCH_POLICY` to a reviewed, hash-stable YAML file mirroring
`risk.example.yaml`'s field set with research values (generous caps, the research symbols
and strategy ids, `allow_long_entries: true`, `allow_overnight_positions: true`). It is
**research-only** and labeled so in a header comment: it permits trades in BACKTEST/SHADOW
and **cannot** reach paper/live — that path is a separate strict conjunction (ADR-0009), and
this policy sets none of those flags. `run_research.py` keeps working; the campaign and the
walk-forward CLI load this file via `load_risk_policy`. (The walk-forward CLI *default* stays
the deny-all example — a research policy is opt-in via `--policy`, never the default.)

### 2. Campaign harness (`research/campaign.py`)

`run_campaign(*, strategies, symbols, data_dir, policy, ledger, stage_end, warmup,
test_window, min_trades, seed) -> CampaignReport`:

- For each (strategy × symbol) with sufficient span, load the series, **slice to the
  dev+val span** (`session_date <= stage_end`, default `2021-12-31`) so the reserved
  holdout is never touched, and call the C3 `walk_forward` — which registers exactly one
  VALIDATION trial per configuration and returns a `WalkForwardReport` (PSR, DSR, bootstrap
  CI, verdict).
- Symbols/series too short for `warmup + 2*test_window` are **excluded with a recorded
  reason**, not silently skipped (ADR-0006 heterogeneity discipline).
- Aggregate into a `CampaignReport`: the per-cell `WalkForwardReport`s + a compact verdict
  table (strategy, symbol, windows, pooled OOS trades, pooled Sharpe, CI, PSR, DSR,
  trial_count, verdict, reason) + provenance (policy hash, data fingerprints, code commit,
  seed, stage wall). Deterministic: same seed → identical table (registry timestamps/ids do
  not enter the table).

### 3. Holdout stays sealed (C2 × C3 × C4)

The campaign is a VALIDATION-stage activity. It computes nothing on `>= FINAL_START` and
performs no holdout read. A holdout evaluation remains available **only** through the C2
`mediated_holdout_read` behind an owner-typed, single-use, logged unlock — never from the
campaign. A safety test asserts the campaign path invokes no guardian unlock and reads no
bar dated `>= FINAL_START`.

### 4. CLI (owner-run) + outputs

`chronos research campaign [--strategies … --symbols … --policy config/risk.research.yaml
--stage-end 2021-12-31 --warmup N --test-window M --min-trades K --seed Z --ledger …]`
runs the campaign, prints the verdict table + JSON, writes
`research/results/campaign_<stage-end>.json`, and appends the trials to the registry.
Read-only; places no order.

## Honesty bounds (report / limitations)

- **This formalizes rejection.** The table is expected to be dominated by
  INSUFFICIENT_EVIDENCE (thin OOS trade counts) and FAIL (non-positive Sharpe CI). That is
  the correct output, not a tool failure. The binding fix is longer uniform history (the C1
  store) or a higher-frequency family, not more runs.
- **Multiple testing is now real and cumulative.** Every cell registers a trial, so the DSR
  deflation hardens across the campaign (and across sessions). Re-running the campaign
  deflates *further* — by design; the registry is the honest memory. The report states the N
  each verdict was deflated against.
- **Research policy ≠ tradeable policy.** `config/risk.research.yaml` removes caps to measure
  compounding cleanly; it is not an endorsed paper/live limit set and structurally cannot
  transmit an order (ADR-0009). Stated in the file header and the report.
- **Data heterogeneity carries over** (ADR-0006): only SPY/QQQ run; the 2019–2021 symbols
  and absent DIA are excluded with recorded reasons, not silently run.
- **Holdout remains sealed.** The campaign never touches `>= 2022-01-01`; any holdout read is
  a separate, owner-typed, guardian-mediated, single-use event (C2).

## What proves it

- The campaign registers exactly one trial per (strategy × symbol × config) — asserted
  against `trial_count`; the verdict table matches per-cell `WalkForwardReport`s.
- Determinism: same seed → byte-identical verdict table.
- Every excluded symbol carries a recorded reason; no silent skips.
- Isolation: the campaign imports no order/broker module; a safety test asserts it reads no
  bar dated `>= FINAL_START` and triggers no guardian unlock (holdout stays sealed).
- The research policy file loads and round-trips to the same `config_hash` as the vetted
  `research-1` profile.

## Consequences

The strategy families get a rigorous, reproducible, sample-honest verdict table whose
default at current sample sizes is a blocking rejection, with a deflated Sharpe that counts
every registered trial and CIs that respect autocorrelation. It changes nothing in the
trading/live plane, adds no hard dependency, and ties C2+C3+C4 together with the holdout
sealed. C5 (wheel/options research) and any fitted or higher-frequency family later reuse
this harness.
