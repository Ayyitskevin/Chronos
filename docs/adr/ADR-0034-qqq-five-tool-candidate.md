# ADR-0034 — QQQ Five-Tool candidate causal integration overlay

Status: **accepted design — owner directed Chronos to select the Confluence-fit defaults,
2026-08-25; exact artifact remains owner-gated at merge. Research-only, blocked before
the first data read, and no trading authority.** Index entry: DECISIONS.md D-48.

## Context

ADR-0032 preserved the pinned Five-Tool Confluence as a separate integrated candidate and
left one material translation open: the price domains for EMA, ATR, AVWAP, support,
resistance, stops, and execution under D-44's point-in-time total-return rule. The owner
asked Chronos to choose the remaining answers from the actual Confluence and current
Chronos configuration.

The candidate must avoid two failures at once. Feeding raw price history directly into
technical features creates false split/dividend discontinuities. Feeding synthetic
total-return levels directly to a broker creates non-tradable order prices. A single
causal, current-price-normalized decision domain separates these concerns without future
corporate-action knowledge.

The controlling overlay is `specs/qqq_five_tool_candidate_v1.json`, SHA-256
`59348ca3da9e9b68ec4edd1fc54572783e9256ae9c55ac18ffe844c0b4b78054`.
`chronos.research.qqq_confluence` authenticates the exact constitution, Pine source,
219-input contract, semantic contract, and blocked campaign bytes before compiling only
typed blockers.

## Decision

### 1. Preserve the pinned source, including asymmetry

The overlay applies no Pine override. On daily bars, `Auto` resolves to the native
20-session, 0.85/0.55, two-confirmed-close regime profile with volatility-percentile
adjustment, hysteresis, EMA-100, all four default triggers, and minimum score 55.

The source's full default asymmetry stays visible: the master `allow_shorts` switch is
**off**; dedicated short v2 and SHORT+ modules are on behind it; dedicated long v2 and
LONG+ are off. Turning on master shorts or either long module creates a separate candidate
identity. This is both more faithful to the source and consistent with Chronos's current
long-only executable boundary.

### 2. One causal decision-price domain

At each confirmed close `t`, Chronos derives point-in-time total-return OHLC using only
corporate actions known by `t`, then rebases the history so adjusted close at `t` equals
the raw tradable close at `t`. All four OHLC values receive the same per-bar factor.
Historical volume is converted to current share units using only the inverse causal split
factor; cash distributions do not alter share volume.

The resulting domain feeds regime returns/oscillators, EMA-100, ATR-14, gaps, pivots,
support/resistance, structural stops, and AVWAP. AVWAP uses adjusted `hlc3` or close with
current-share-unit volume. Benchmark/Mansfield relative strength uses independently causal,
identity-bound total-return indices aligned by completed close.

Because the decision series is current-price-normalized, every level produced at `t` is in
current raw-price units. Quotes, limits, fills, positions, cash, and broker reconciliation
remain raw. Mixing domains or silently falling back is forbidden. A corporate action or
factor change between signal and handoff invalidates and consumes the entry event; the
system waits for a new confirmed signal rather than rolling a level forward.

### 3. Native management remains inside stricter owner limits

The source keeps its structural stop or 2x ATR-14 fallback, 1R/2R targets, breakeven after
T1, and 22-session/3x ATR Chandelier runner activated at 1R. Opposite confirmed regime and
source-default side-specific AVWAP exits remain; no SMA-200, fixed time, or neutral-only
exit is added.

Native stop-distance sizing risks 1% of `min(marked strategy NAV, USD 3,000)`, at most USD
30 at the reference base. Whole-share quantity is the floor of the minimum permitted by
native stop risk, direction-specific 95% CVaR-252 (1.5%/USD 45), 100% gross/1x leverage,
affordability, and owner policy. The observed 2%/USD 60 daily/session limit and 10% drawdown
remain circuit breakers. No in-position increase or later top-up is permitted.

The pinned bytes carry the complete calculation. A unit CVaR observation is the
one-session direction-specific loss fraction on USD 1 of unlevered QQQ exposure from the
point-in-time total-return close-to-close return. Long loss is `max(0, -return)` and the
estimator is the arithmetic mean of the 13 greatest losses in the completed 252-return
window; a finite, strictly positive value is mandatory. At signal time, quantity is the
non-negative whole-share floor of the minimum native-stop, CVaR, gross, leverage,
post-cost affordability, and owner-policy quantities. At handoff every price-sensitive
component is recomputed with the greater of the confirmed close and protected buy limit,
the stop distance may only widen, and the final quantity is additionally capped at the
signal-time quantity. Missing, stale, non-finite, non-positive, or uncertifiable input
creates no new exposure.

Parity-only replay retains the base campaign's costs. Economic validation requires the
owner constitution's content-addressed all-in schedule, and projected round-trip cost must
not exceed 10% of the smaller native-stop and CVaR dollar budgets.

### 4. Entry/handoff behavior

A pinned confirmed entry while flat receives one protected marketable DAY attempt using
Chronos's existing 1% collar. Fresh next-session evidence may only reduce quantity. A
partial fill becomes the position; the remainder expires or is cancelled and cannot be
topped up. Identity, adjustment factor, signal event, contract, quote/market rules,
reconciliation, authority/promotion, kill/loss/drawdown state, stop/CVaR/cost/affordability,
and the no-upsize rule are all revalidated immediately before handoff.

## Consequences and blockers

The QQQ integration mapping is exact, but the candidate is still not a runnable campaign.
The base Five-Tool campaign's ablation and execution-binding blockers remain. History
start, exact settings, certified data/catalog, unopened holdout map, benchmark, costs,
power, evaluator, criteria, code, TradingView parity, and durable paper lifecycle identities
are unresolved. Short compiler/borrow/shortability/account/legal/tax/owner evidence is
absent.

This ADR does not read data, register a trial, open a holdout, select a strategy, grant
funding, authorize paper/live operation, or claim edge. Evidence from the SMA control
cannot promote this candidate.
