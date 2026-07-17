# Strategy Selection (Phase 6 outcome)

Selection criteria were frozen in `research/selection_manifest.json` before
validation results existed; full evidence in `docs/RESEARCH_REPORT.md`.

## Selected candidates: NONE

| Candidate | Derived from | Validation outcome | Frozen-criteria result | Status |
|---|---|---|---|---|
| `regime_trend_v1` | Pine 01 BULL+ v1.1 core (regime engine + Markov stay gate + ATR stop) | QQQ: +33.0%, PF 2.64, maxDD 11.7%, cost-robust, all sensitivity variants positive — but 18 trades < 20 floor; SPY: −2.5% | Passes C1–C3, **fails C4 sample floor** | `not_eligible` (research prototype; strongest re-test candidate) |
| `mean_reversion_v1` | Pine 11 MR Extremes Study v1.1 (daily reduction) | Net-negative both symbols; 14/16 sensitivity variants negative | **Fails C1** | `not_eligible` (recommend retiring the daily derivation) |

## Why zero is the right answer

- The brief's own standard: "It is acceptable — and preferable — to conclude
  that none of the strategies is currently suitable for live trading rather
  than inventing confidence that the evidence does not support."
- Bending the frozen 20-trade floor by two trades after seeing a favorable
  result would be textbook selection bias; the floor exists exactly for
  this moment.
- The favorable QQQ window (2018–2021) rewarded anything long tech; a
  regime gate showing PF 2.64 over 18 trades in that window is promising,
  not proven.

## What was NOT selected and why (corpus-wide)

The other 40 corpus artifacts were not translation candidates for execution
in this build: the majority are indicators/studies/readouts with no order
rules (`NON_EXECUTABLE_INDICATOR` — by design, not defect), several are
strategy add-ons/validators, the intraday tools cannot be validated without
intraday data (A-31), and the two large confluence strategies (00 AIO v3.6,
16 Pullback-to-Value, plus 02 BEAR+ short-side and archived 0A) were
classified for future work: 02 requires short selling (disabled for this
account), and 00/16 embed multi-layer discretionary-confluence logic whose
faithful translation was out of scope for a first deterministic build (see
docs/PINE_AUDIT.md per-script feasibility notes).

## Portfolio conclusion

No portfolio is proposed: the only net-positive candidate stands alone, and
its correlation partner is net-negative. Voting correlated indicators
together was explicitly avoided.

## Current operating eligibility

- Platform: **shadow-capable engineering** (gates in
  docs/GO_LIVE_CHECKLIST.md; shadow-scan CLI exists).
- Strategies: `research_prototype` only. Nothing is backtest-validated,
  shadow-eligible, paper-eligible, or live-eligible.
