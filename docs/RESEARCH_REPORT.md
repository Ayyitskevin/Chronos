# Quantitative Research Report (Phase 6)

## Verdict up front

**No strategy met the frozen validation criteria. Zero candidates are
selected for promotion beyond research.** The brief explicitly allows — and
prefers — this outcome over manufactured confidence. The platform, data
pipeline, and research harness are sound and reusable; the two derived
strategy cores, as specified from the corpus defaults, did not demonstrate a
defensible edge on the available data under the criteria frozen in
`research/selection_manifest.json` **before** validation results were
computed.

## Method

- **Engine**: every run goes through the production path (portfolio sizing →
  independent risk engine → execution engine → simulated broker), not a
  vectorized shortcut. Determinism is test-asserted.
- **Costs**: USD 0.005/share with USD 1.00 minimum per order (IBKR Pro fixed
  assumption) + 2 bps/side slippage baseline. On a USD 3,000 account the
  commission floor alone is ≈ 3.3 bps/side — material, and included.
- **Partitions** (chronological, frozen): development ≤ 2017-12-31;
  validation 2018-01-01..2021-12-31; final test 2022-01-01.. reserved.
- **Criteria**: C1–C6 frozen and committed before any validation-window
  computation (`research/selection_manifest.json`, commit history proves
  ordering).
- **Baselines**: buy-and-hold-with-49%-disaster-stop; SMA 50/200 trend; a
  deterministic random-entry twin (fixed-seed LCG, ~5%/bar entry
  probability, 10-bar hold) carrying identical costs.
- **Data**: SPY 2000-01..2019-11 (unadjusted), QQQ 1999-11..2024-01
  (unadjusted OHLC + adjusted close), both integrity-validated and
  cross-checked to the penny against an independent dataset lineage
  (`research/data/raw/MANIFEST.json`). IWM/DIA/GLD/TLT could not be
  trustworthily acquired in this environment and were excluded rather than
  fabricated.

### Metric conventions

`net_pnl_usd`/PF/win-rate derive from **closed** trades; `total_return`
includes marked open positions. The "buy & hold" baseline carries the
platform's mandatory protective stop (at the 49% cap), so in crash windows
it becomes stop-out-and-reenter; its returns are reported with that caveat
rather than pretending the platform can express a stopless position.

## Results — development window (exploration; ≤ 2017)

| Strategy | Sym | Trades | Net ret | MaxDD | PF | Sharpe | Exposure |
|---|---|---|---|---|---|---|---|
| regime_trend_v1 | SPY | 86 | +10.2% | 17.4% | 1.05 | 0.12 | 28% |
| regime_trend_v1 | QQQ | 88 | +34.1% | 15.5% | 1.37 | 0.24 | 30% |
| mean_reversion_v1 | SPY | 78 | +1.3% | 12.4% | 1.03 | 0.04 | 4% |
| mean_reversion_v1 | QQQ | 81 | +19.0% | 18.4% | 1.40 | 0.21 | 5% |
| baseline_sma_trend | SPY | 8 | +199.9% | 18.6% | 14.4 | 0.66 | 54% |
| baseline_sma_trend | QQQ | 12 | +209.8% | 26.4% | 3.01 | 0.55 | 56% |
| baseline_random_entries | SPY | 141 | +71.8% | 20.3% | 1.55 | 0.37 | 31% |
| baseline_random_entries | QQQ | 142 | +96.5% | 54.5% | 1.50 | 0.34 | 30% |

Already in development, both candidates trail the trivial SMA baseline by a
wide margin and roughly match or trail their random twin at comparable
exposure — i.e., most of their profit is market drift captured while long,
not signal skill.

## Results — validation window (2018–2021; SPY truncates 2019-11)

| Strategy | Sym | Trades | Net ret | MaxDD | PF | Sharpe |
|---|---|---|---|---|---|---|
| regime_trend_v1 | SPY | 5 | −2.5% | 8.5% | 0.42 | −0.27 |
| **regime_trend_v1** | **QQQ** | **18** | **+33.0%** | **11.7%** | **2.64** | **0.74** |
| mean_reversion_v1 | SPY | 8 | −7.0% | 7.5% | 0.20 | −1.20 |
| mean_reversion_v1 | QQQ | 15 | −9.8% | 13.1% | 0.36 | −0.67 |
| baseline_buy_hold | QQQ | 0 | +135.5% | 26.6% | — | 1.07 |
| baseline_sma_trend | QQQ | 2 | +84.1% | 26.6% | 2.79 | 0.86 |
| baseline_random_entries | QQQ | 30 | +6.9% | 17.8% | 1.08 | 0.19 |

Cost stress (QQQ, regime_trend_v1): +31.6% at 2× commissions; +29.9% at
10 bps slippage; +22.6% at 25 bps — cost-robust. Sensitivity: all ten
predefined variants remain positive (+31.9%..+50.6%) — no knife-edge.
Candidate daily-return correlation: SPY 0.38, QQQ 0.20.

## Criteria application (frozen order)

**mean_reversion_v1 — FAILS C1** (net-negative on both symbols). Sensitivity
confirms the failure is structural, not parametric: 14 of 16 variants are
negative. The derived daily RSI-2/EMA-20 core (which deliberately omits the
study's intraday VWAP-stretch component) has no demonstrated edge here.
Status: `not_eligible` (research prototype).

**regime_trend_v1 — passes C1, C2, C3 on QQQ; FAILS C4**: 18 closed trades
< the frozen floor of 20. C4's cost legs would have passed; C5 would have
passed. Per the manifest, failing any of C1–C5 → `not_eligible`. We do not
bend a frozen criterion by two trades after seeing the results — that is
precisely the selection bias the floor exists to prevent. The near-miss and
its strengths are recorded for the next iteration.

**C6 / final test**: with zero candidates passing C1–C5, the final window
(2022-01-01..) was **not consumed** and remains pristine for future research
with broader data.

## Honest interpretation

1. The BULL+ derived core shows a *plausible but unproven* regime-gated
   profile on QQQ: materially lower drawdown than baselines with respectable
   return, robust to costs and parameter perturbation — but with a thin
   trade sample, on one symbol, over one four-year window that was
   exceptionally kind to anything long tech. Its SPY result is negative.
   That is not evidence of a durable edge; it is grounds for a longer,
   broader re-test.
2. The mean-reversion derivation is not viable as a daily-bar system on
   these ETFs. The corpus study it derives from is an intraday flag tool;
   the daily reduction loses whatever made it interesting.
3. Simple baselines are hard to beat. The corpus's own doctrine ("the gate
   is the strategy"; "backtest is the autopilot floor, not a promise") is
   consistent with what we measured.
4. USD 3,000 + IBKR minimum commissions is a hostile cost environment:
   every marginal edge must clear ≈ 6.6 bps round-trip in commissions alone
   plus slippage.

## Confidence limitations

- Data: public mirrors (integrity-validated but research-grade); SPY ends
  2019-11; unadjusted prices understate long-side total return by the
  dividend yield; two symbols only. Re-run against IBKR historical data
  before any promotion (RISK_REGISTER R-08).
- Parameters came from the corpus author's defaults — they may embed that
  author's look at overlapping history (recorded, uncontrollable here).
- No intraday validation was possible; intraday corpus scripts are
  unassessed on merit (A-31).
- Single-path backtest per configuration; no bootstrap confidence intervals
  were computed for the thin validation samples (the thin samples are
  themselves the reason C4 failed).

## Recommended next research iteration (owner decisions)

1. Source IBKR historical daily data for 6–10 liquid ETFs (or provide
   another trusted feed) covering 2000–present, dividend-adjusted.
2. Re-run this harness unchanged; the final window is still unconsumed.
3. If regime_trend_v1 then clears all criteria including a ≥20-trade sample
   on ≥2 symbols, proceed to the shadow-mode gate in
   docs/GO_LIVE_CHECKLIST.md.
4. Consider retiring the daily mean-reversion derivation or re-deriving it
   as the intraday study it actually is (blocked on intraday data + PDT
   constraints for this account size).
