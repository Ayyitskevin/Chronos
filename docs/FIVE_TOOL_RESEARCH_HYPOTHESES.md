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
not a clean holdout. The checked manifest broker refuses all data while blocked, and no
Five-Tool campaign path can invoke the repository's separately owner-gated holdout
guardian. Public v1 validation also refuses any manifest changed to
`ready_for_certified_research`: the new canonical trial registry, manifest-bound ordinary
reader, replay object store, and causal fill adapter are infrastructure, not proof that
this campaign's dataset, evaluator, owner limits, or readiness locks have been certified.

## Evidence the cited sources do and do not provide

These sources motivate tests; none validates Five-Tool v3.6 or its exact parameters.

| Source | What it supports | What it does **not** support |
|---|---|---|
| Moskowitz, Ooi & Pedersen, “Time Series Momentum,” *JFE* (the supplied [ScienceDirect record](https://www.sciencedirect.com/science/article/pii/S0304405X11002613)) | Historical evidence for continuation/trend effects across a broad futures panel, with explicit portfolio construction and volatility scaling in that study | The Pine z-score regime, EMA/AVWAP gates, Mansfield formula, RSI/MFI divergence, three-leg exits, current ETF panel, current costs, or future performance |
| Moreira & Muir, “Volatility-Managed Portfolios,” [NBER Working Paper 22208](https://www.nber.org/papers/w22208) | Empirical motivation to test state-dependent exposure scaled by observed variance in the paper's factor/portfolio setting | The Pine ATR/vol-percentile multiplier, a claim that scaling improves every strategy, intraday execution, or a causal guarantee |
| Antonacci, “Absolute Momentum,” [SSRN 2244633](https://doi.org/10.2139/ssrn.2244633) | Practitioner evidence and a concrete absolute-momentum/trend-overlay construction worth treating as a benchmark | Peer-reviewed validation of Five-Tool's score, cross-sectional Mansfield RS, divergence, Markov dwell gates, or any default threshold |
| White, “A Reality Check for Data Snooping,” *Econometrica* ([publisher DOI](https://doi.org/10.1111/1468-0262.00152)) | Motivates treating selection across many tried rules as a multiple-testing problem and evaluating whether an apparent best rule survives a data-snooping-aware null | The exact Chronos DSR/FDR/PBO thresholds, independence of Five-Tool trials, the selected bootstrap design, or evidence that any Five-Tool component has edge |
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
Best-trade and best-month removal, base/stressed costs, and the future canonical ADR-0013
registry multiplicity still bind.

**Source boundary.** The supplied sources do not validate this z-score hysteresis,
transition estimator, maturity ceiling, dwell percentile, or Markov independence
assumption. They are strategy-specific mechanisms that must earn evidence from zero.

## Common campaign tests (all cells)

The private synthetic lifecycle harness gives every attempted parameterization, ablation,
retry, reader failure, and evaluator failure a unique durable `trial_started` record
before its callback reader is invoked. Its multiplicity is only the count of unique start
`attempt_id` values in that caller-selected ledger. It is deliberately named a
**ledger-local trial count**, not the canonical research multiplicity.

The future certified campaign must collect raw evidence for every cell before binding all
candidates to one final ADR-0013 registry-derived multiplicity and independently reviewed
cross-trial Sharpe variance. The current private harness can seal ledger-local evidence
and a supplied variance identity for lifecycle tests only. It cannot produce a Phase-3
score or final verdict. Candidate display names and evaluation order remain excluded from
the intended scoring identity.

Chronos now also provides a separate brokered evidence path:
`CanonicalTrialRegistry` writes a unique start to the fixed canonical registry,
`CertifiedDatasetCatalog` opens only the exact authenticated ordinary partition after that
start, `ReplayObjectStore` retains exact input/output bytes, and
`BrokeredResearchTrialRunner` writes the terminal outcome only after a replay envelope is
durable. Starts—not terminals—define the canonical multiplicity snapshot, so failed,
interrupted, and repeated attempts remain counted. The Five-Tool manifest is not yet wired
to this path, and the snapshot is not a final score seal or reviewed variance estimate.

A cell cannot pass unless all applicable checks below pass unchanged:

These are preregistered requirements, not an implemented campaign verdict. Benchmark-alpha
confidence intervals, DSR scoring, FWER/FDR, PBO, power, and fully OOS-native cost/risk
evidence remain unimplemented and therefore blocking.

1. `max(preregistered power-required N, 100 OOS economic positions)`; closed legs are
   reported separately, with sufficient bars for the declared warm-up/history start.
2. Post-cost expectancy and benchmark-alpha 95% lower bounds above zero.
3. Deflated-Sharpe probability at least 0.95 using the final canonical-registry trial
   multiplicity; FWER or FDR `q <= 0.05`; probability of backtest overfitting at most
   10% when estimable. The implemented ledger-local count cannot satisfy this gate.
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
- The entry bar resolves only the absolute signal-time stop pre-submitted for every leg;
  targets remain inactive until later bars, when the ladder is rebased to actual adverse
  next-open execution without resizing signal-time quantity or risk. This is frozen replay
  policy, not a fill-parity claim.
- Lower-timeframe magnifier mode requires complete, identity-matched sub-bar coverage that
  reproduces parent OHLC for every replay bar, even while flat. Missing or incomplete
  coverage fails closed; it never silently falls back to chart OHLC.
- Expanding Markov/dwell state changes with loaded history. `history_start_utc` is identity,
  and alternate starts are sensitivity trials, never a silent data-loader choice. The
  signal-to-ledger adapter is full-from-origin only and has no checkpoint/resume contract.
- Result identity binds all effective primary/companion identities and values, explicit
  primary open timestamps, and lower-timeframe identity/OHLC evidence. Caller account
  snapshots are excluded only because replay overwrites them with result-bound owned state.
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
policy, canonical replay-policy SHA-256 (including terminal policy, entry-stop/later-ladder
timing, target-limit slippage, and discretionary/protective priority), cost model, criteria
digest, or code commit. A changed identity receives a new campaign id. Before Phase 3 can
run, both old and new attempts must be recorded through the fixed canonical ADR-0013
capability so a new evidence path or restart cannot erase prior multiplicity. The private
Five-Tool lifecycle harness remains path-local; the new canonical runner exists, but this
blocked campaign has not been authorized or wired to it.

No Five-Tool evaluator imports or exposes a holdout unlock capability. The repository's
separate owner-authorized guardian may open an untouched holdout only after all
development/validation choices are frozen. Failure on that one unchanged holdout means
rejection, not a threshold edit, new ablation, or second “final” window under the old id.

The private lifecycle harness accepts arbitrary reader and evaluator callbacks. A callback
can preload data or touch undeclared sources, so its start-before-callback ordering is not
proof that every underlying data touch was brokered. Its evaluation artifacts are hashed
but not retained. The separate brokered runner removes the reader callback, retains its
declared input/output objects, and binds them to canonical start/terminal hashes; it is not
a Python sandbox, so only reviewed evaluators may be used. No campaign-ready Five-Tool
evaluator, owner attestation, final-N seal, or reviewed variance evidence exists yet.
