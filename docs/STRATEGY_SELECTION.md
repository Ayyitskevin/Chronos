# Strategy Selection (Phase 6 outcome, broadened-universe re-run)

Selection criteria were frozen in `research/selection_manifest.json` before
validation results existed, then **re-frozen unchanged** (only data inventory
and disclosures updated) before the three added symbols were computed. Full
evidence in `docs/RESEARCH_REPORT.md`; results in `research/results/`.

## Selected candidates: NONE

| Candidate | Derived from | Validation outcome (5 symbols) | Frozen-criteria result | Status |
|---|---|---|---|---|
| `regime_trend_v1` | Pine 01 BULL+ v1.1 core (regime engine + Markov stay gate + ATR stop) | Net-positive & cost/param-robust on QQQ (+33.0%, PF 2.64) and IWM (+11.0%, PF 1.70, 10/10 sensitivity); negative on SPY/GLD, fragile on TLT | Passes C1–C3 on QQQ & IWM, **fails C4 sample floor on every symbol** (max 18 trades) | `not_eligible` (research prototype; strongest re-test candidate) |
| `mean_reversion_v1` | Pine 11 MR Extremes Study v1.1 (daily reduction) | Was net-negative on SPY/QQQ; now net-positive on IWM (+16.2%, PF 3.35, 7/8 sensitivity) and marginally TLT (+2.9%); negative on SPY/QQQ/GLD | Passes C1–C3 on IWM, **fails C4 sample floor on every symbol** (max 12 trades) | `not_eligible` (small-cap re-test hypothesis only) |

## Why zero is the right answer

- The brief's own standard: "It is acceptable — and preferable — to conclude
  that none of the strategies is currently suitable for live trading rather
  than inventing confidence that the evidence does not support."
- The binding gate is C4's ≥ 20-closed-trade floor, and **no candidate reaches
  20 trades on any symbol** (max 18). Broadening from 2 to 5 symbols confirmed
  this is a structural low-trade-frequency property, not a QQQ artifact.
- Bending a frozen criterion after seeing a favorable result would be textbook
  selection bias; the floor exists exactly for this moment.
- `mean_reversion_v1`'s IWM result (+16.2%, PF 3.35) is the single strongest
  cell in the study and the most likely to be noise: 12 trades, one small-cap,
  a dividend-adjusted favorable window, best-of-ten cells. The frozen
  multiple-testing guard requires reading it as a hypothesis, not a result.

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

No portfolio is proposed. No candidate is even individually eligible, and the
candidate daily-return correlations, while low (IWM 0.23, GLD 0.03, TLT 0.003,
QQQ 0.20, SPY 0.38), only matter once at least one leg is validated — which
none is. Voting correlated indicators together was explicitly avoided.

## Current operating eligibility

- Platform: **shadow-capable engineering** (gates in
  docs/GO_LIVE_CHECKLIST.md; shadow-scan and monitor CLIs exist).
- Strategies: `research_prototype` only. Nothing is backtest-validated,
  shadow-eligible, paper-eligible, or live-eligible. The broadened data
  surfaced two re-test hypotheses (regime_trend on liquid equity indices,
  mean_reversion on small-caps) for a future run against uniformly-adjusted,
  full-history data — not promotions.
- QQQ v1 (ADR-0031) leaves the selected strategy as **NONE**. Its first intended campaign
  is a separately preregistered simple daily long/short trend hypothesis, with QQQ as the
  only execution target and five ETFs used only for robustness validation. No campaign
  identity, complete rule, trial, data release, holdout result, promotion rung, or
  short-side authority exists yet. D-36 selects confirmed close versus SMA-200 as the
  direction indicator. D-37 adds an immediate two-state primary transition and reserves a
  1% neutral band and five-close confirmation as robustness variants; price/window details,
  protective exits, and remaining parameter neighbors are still unresolved. D-38 selects
  CVaR-capped volatility sizing under the frozen 100% gross/1x ceilings, and D-39 selects a
  252-session empirical historical estimator; its return/window convention, capital base,
  and rebalance rule remain unresolved. D-40 gives long and short their own loss tails;
  short exposure remains zero until its borrow/cost evidence is certifiable. D-41 uses the
  lower of marked strategy NAV and USD 3,000 as the sizing base, so the dollar risk allowance
  shrinks after losses and cannot rise above USD 45 after gains.
