# Five-Tool v3.6 research hypotheses and falsification contract

Status: **PREREGISTERED / BLOCKED BEFORE DATA ACCESS**
Scope: research only; no strategy selection, promotion, paper/live activation, or
performance claim
Manifest: `research/five_tool_v3_6_campaign_manifest.json`

## Claim boundary

The Five-Tool Pine program combines several economically different ideas. Agreement with
the Pine calculation would establish implementation fidelity, not alpha. A profitable
full-stack backtest would not identify which idea helped, and an attractive cell selected
after looking at all cells would be a multiple-testing artifact unless every attempt were
counted. This document therefore registers six separate hypotheses, a full-stack reference
cell, common rejection tests, and the identities whose change starts a new campaign.

No campaign result exists in this document. The checked-in manifest is blocked until the
certified dataset digest, power calculation, accessible partitions, code/criteria digests,
and owner risk limits are frozen. QQQ 2022-01 through 2024-01 is already consumed and is
not a clean holdout. The declared future holdout is inaccessible through the ordinary
Five-Tool trial API.

## Evidence the cited sources do and do not provide

These sources motivate tests; none validates Five-Tool v3.6 or its exact parameters.

| Source | What it supports | What it does **not** support |
|---|---|---|
| Moskowitz, Ooi & Pedersen, “Time Series Momentum,” *JFE* (the supplied [ScienceDirect record](https://www.sciencedirect.com/science/article/pii/S0304405X11002613)) | Historical evidence for continuation/trend effects across a broad futures panel, with explicit portfolio construction and volatility scaling in that study | The Pine z-score regime, EMA/AVWAP gates, Mansfield formula, RSI/MFI divergence, three-leg exits, current ETF panel, current costs, or future performance |
| Moreira & Muir, “Volatility-Managed Portfolios,” [NBER Working Paper 22208](https://www.nber.org/papers/w22208) | Empirical motivation to test state-dependent exposure scaled by observed variance in the paper's factor/portfolio setting | The Pine ATR/vol-percentile multiplier, a claim that scaling improves every strategy, intraday execution, or a causal guarantee |
| Antonacci, “Absolute Momentum,” [JPM 40(5)](https://doi.org/10.3905/jpm.2014.40.5.094) | Practitioner evidence and a concrete absolute-momentum/trend-overlay construction worth treating as a benchmark | Peer-reviewed validation of Five-Tool's score, cross-sectional Mansfield RS, divergence, Markov dwell gates, or any default threshold |
| Supplied [Econometrica DOI](https://doi.org/10.1111/1468-0262.00152) | Recorded as a starting citation for independent review | No Chronos gate relies on it yet: bibliographic metadata and relevance have not been independently verified in this offline implementation slice |
| TradingView [execution model](https://www.tradingview.com/pine-script-docs/language/execution-model/), [repainting](https://www.tradingview.com/pine-script-docs/concepts/repainting/), and [strategy FAQ](https://www.tradingview.com/pine-script-docs/faq/strategies/) | Platform authority for confirmed-bar execution, repaint/lookahead analysis, and strategy/fill semantics used by parity work | Any claim of predictive edge or economic validity |
| `research/pine/00_five_tool_confluence_aio.pine` at SHA-256 `e51d5a40d2e933bf86847c7432364ba8934fd2de653d6aec3d7205639248e45f` | Exact implementation authority for the strategy being translated | Independent evidence that any component works |

The source summaries are deliberately narrow. Broad evidence for “momentum” does not
authorize treating a proprietary combination of momentum, divergence, relative strength,
regime, and risk overlays as one already-supported hypothesis.

## Registered hypotheses

### H-5T-001-TREND — directional trend state

**Claim under test.** On a fixed, closed-bar information set, the strategy's trend
component produces positive post-cost out-of-sample expectancy and benchmark alpha more
often than a duration/exposure-matched direction-neutral baseline.

**Isolation.** Enable only the pinned trend state and direction rule. Disable momentum,
divergence, Mansfield relative strength, volatility scaling, and regime-conditioned entry
gates as alpha inputs. The full-stack default is a reference, never the attribution cell.

**Falsification.** Reject if either the post-cost expectancy or benchmark-alpha 95% lower
bound is not above zero; if the result fails on fewer than three instruments or two
materially different regimes; if the best trade or best month is required; if stressed
costs reverse the result; or if fewer than 67% of preregistered neighboring trend
lookback/EMA settings retain the same sign and pass the common floor.

**Source boundary.** Time-series-momentum literature motivates this test. It does not
validate the Pine classifier, lookback, thresholds, or ETF sample.

### H-5T-002-MOMENTUM — continuation/strength score

**Claim under test.** Conditional on the same eligible bars and exposure budget, the
momentum/strength component adds positive post-cost incremental expectancy relative to a
cell in which that component is disabled.

**Isolation.** Hold trend eligibility, fill policy, risk budget, and exit policy fixed.
Compare the exact momentum cell to its paired component-off cell. No result may be selected
from an unregistered threshold sweep.

**Falsification.** Reject when the paired incremental-expectancy 95% lower bound is not
above zero, turnover-adjusted gains disappear under base or stressed costs, performance is
confined to one instrument/regime, or a preregistered score-threshold neighborhood fails
the 67% plateau requirement. Remove the best trade and month separately and repeat.

**Source boundary.** The momentum sources support testing continuation in their own
populations. They do not establish that this composite score, sampling interval, or gate
has independent information.

### H-5T-003-VOL-SCALING — volatility-conditioned risk sizing

**Claim under test.** For an identical timestamp/direction signal stream, the pinned
volatility-scaling overlay reduces loss-CVaR and drawdown without producing a negative
post-cost expectancy lower bound or excessive turnover/concentration.

**Isolation.** Freeze entries, exits, fills, and total risk budget first; compare scaling
on versus off. Signal changes caused by a volatility **entry filter** belong to the regime
or momentum tests, not this sizing hypothesis.

**Falsification.** Reject unless the paired bootstrap interval supports lower loss-CVaR,
the owner-frozen drawdown limit is met, and post-cost expectancy remains non-negative at
its 95% lower bound. Also reject if improvement is one crash/month/instrument, if leverage
or turnover breaches its frozen bound, if doubled commission plus stressed slippage erases
the benefit, or if neighboring volatility lookbacks/targets form an isolated optimum.

**Source boundary.** NBER 22208 motivates variance-managed exposure. It does not validate
the Pine percentile/ATR mechanics or guarantee reduced tail loss for this signal stream.

### H-5T-004-DIVERGENCE — confirmed RSI/MFI pivot divergence

**Claim under test.** A divergence event known only after the pinned right-bar confirmation
lag adds positive post-cost incremental expectancy versus timestamp-matched pseudo-events
and versus the same setup with divergence disabled.

**Isolation.** Event time is the confirmation bar, never the pivot bar. RSI and MFI are
separate preregistered subcells; neither may replace the other after results are known.
Pivot-left, pivot-right, minimum-gap, and maximum-gap neighbors are all registered starts.

**Falsification.** Reject if the paired/event-study 95% lower bound is not above zero after
costs, any apparent gain disappears when aligned at the observable confirmation bar, the
effect depends on one oscillator/instrument/regime, fewer than 67% of registered pivot/gap
neighbors retain the sign, or best-trade/best-month removal makes the result non-positive.

**Source boundary.** None of the supplied primary sources directly supports RSI/MFI price
divergence. This is an unsupported prior receiving a deliberately hard falsification test.

### H-5T-005-RELATIVE-STRENGTH — Mansfield benchmark-relative strength

**Claim under test.** With same-timeframe benchmark data aligned without future values,
the exact Mansfield relative-strength state adds positive post-cost benchmark alpha to a
paired setup with the RS gate disabled.

**Isolation.** Benchmark is SPY and is immutable for this campaign. Missing benchmark bars
follow the executable spec's pinned rule. Initial missing history is not backfilled from a
future observation. Cross-sectional and absolute momentum are reported separately rather
than relabeled as Mansfield evidence.

**Falsification.** Reject if benchmark-alpha 95% lower bound is not above zero, if results
depend on benchmark gaps or one instrument/regime, if stressed costs erase the result, if
turnover or instrument concentration exceeds its frozen bound, or if Mansfield
lookback/threshold neighbors fail the 67% plateau rule. Remove the best trade and month.

**Source boundary.** Broad momentum evidence only motivates the direction of inquiry. No
supplied source validates the exact Mansfield ratio, 200-bar default, or zero-line gate.

### H-5T-006-REGIME-FILTER — hysteretic regime/Markov gating

**Claim under test.** Applying the exact pinned regime filter to a held-fixed underlying
entry opportunity stream improves post-cost benchmark alpha and tail risk out of sample,
without merely reducing exposure to a lucky interval.

**Isolation.** Pin history start because expanding Markov counts and completed-spell dwell
samples are path dependent. Compare gate-on versus gate-off on the same underlying setup
opportunities. Report z-score/hysteresis and Markov/dwell contributions separately; the
optional external Pine source is excluded unless independently content-addressed.

**Falsification.** Reject if paired benchmark-alpha 95% lower bound is not above zero, if
loss-CVaR/drawdown fails owner bounds, if benefit vanishes under equalized exposure, if it
is concentrated in one named regime/instrument/period, if a shifted history start changes
the conclusion, or if neighboring lookback/entry/exit thresholds fail the 67% plateau.
Best-trade and best-month removal, base/stressed costs, and global multiplicity still bind.

**Source boundary.** The supplied sources do not validate this z-score hysteresis,
transition estimator, maturity ceiling, dwell percentile, or Markov independence
assumption. They are strategy-specific mechanisms that must earn evidence from zero.

## Common campaign tests (all cells)

Every attempted parameterization, ablation, retry, reader failure, and evaluator failure
gets a unique durable `trial_started` record before the reader returns bytes.
Multiplicity is the count of unique start `attempt_id` values, not completed winners.

Raw evidence is collected first for every cell. Only after collection closes may the
campaign bind all candidates to one final ledger-derived N and one independently reviewed
cross-trial Sharpe variance. Candidate display names and evaluation order do not enter
the scoring identity.

A cell cannot pass unless all applicable checks below pass unchanged:

1. `max(preregistered power-required N, 100 OOS closed trades)` and sufficient bars for
   the declared warm-up/history start.
2. Post-cost expectancy and benchmark-alpha 95% lower bounds above zero.
3. Deflated-Sharpe probability at least 0.95 using final global N; FWER or FDR
   `q <= 0.05`; probability of backtest overfitting at most 10% when estimable.
4. Evidence on at least three instruments and two materially different regimes. Each
   instrument/regime result is shown; pooling cannot conceal a negative dominant cell.
5. Base commission, slippage, spread, funding/borrow, model, and data costs are applied
   when applicable. Doubled commission plus stressed slippage is a mandatory stress.
6. Turnover, gross/net exposure, leverage, and holding-period distributions stay within
   owner-frozen limits. No instrument, trade, month, or regime exceeds its frozen
   concentration bound.
7. Maximum drawdown and loss-CVaR remain inside owner-frozen limits on pooled and
   instrument-level evidence.
8. Result remains positive after removing the single best trade and, separately, the best
   calendar month. These are recomputations, not prose sensitivity claims.
9. At least 67% of the preregistered neighboring parameter cells retain the expected sign
   and satisfy their required gates; an isolated optimum fails.
10. Deterministic repeat, batch-versus-stream replay, timestamp audit, and no-lookahead
    tests pass before economic statistics are considered.

The exact owner drawdown, CVaR, turnover, leverage, and concentration limits are not yet
authorized. Their absence blocks first data access; it is not permission to choose them
after observing a result.

## Known semantics that cannot be hidden inside a hypothesis result

- TradingView bar magnifier can choose a different within-bar target/stop ordering than
  chart-timeframe OHLCV. Signal parity is reported separately; the Chronos approximation
  is conservative stop-first and labeled as such.
- Expanding Markov/dwell state changes with loaded history. `history_start_utc` is identity,
  and alternate starts are sensitivity trials, never a silent data-loader choice.
- A three-leg position is not three independent hypotheses or necessarily three
  independent trades. Statistics disclose leg-level versus position-level accounting.
- Exit reasons must be explicit events. A missing leg identifier is not by itself proof
  that target 1 or target 2 filled.
- Side switches and entry-bar costs require explicit sleeve attribution. Allocation cannot
  be inferred retrospectively from aggregate equity deltas.
- Long and short stops require symmetric positive-distance validation. Invalid/zero equity
  and invalid stops fail closed rather than producing a zero or unbounded position.
- Static and dynamic Pine alert paths are not independent signals. Chronos emits at most
  one semantic event identity per decision.
- Undefined profit factor remains undefined/infinite with an explicit reason; it is never
  converted to a large finite sentinel and ranked as ordinary data.
- A “daily” loss reset must bind to a named session/calendar. On daily bars, a reset on
  every bar can make the protection inert and cannot be represented as a passing halt test.

## Campaign invalidation and honest restart

Any change to one of the following ends this campaign identity: Pine SHA, executable
input-contract/config digest, certified dataset digest, history start, benchmark, fill
policy, cost model, criteria digest, or code commit. A changed identity receives a new
campaign id and its attempts add to global research multiplicity; it does not overwrite or
rename old attempts.

An untouched holdout is opened only through the separately owner-authorized guardian after
all development/validation choices are frozen. Failure on that one unchanged holdout means
rejection, not a threshold edit, new ablation, or second “final” window under the old id.
