# ADR-0033 — QQQ SMA control exact preregistration

Status: **accepted design — owner directed Chronos to select the Confluence-fit defaults,
2026-08-25; exact artifact remains owner-gated at merge. Research-only, blocked before
the first data read, and no trading authority.** Index entry: DECISIONS.md D-47.

## Context

ADR-0032 separated the simple SMA attribution control from the integrated Five-Tool
candidate, but left the control's equality/initialization, price reference, entry-gap,
revalidation, economic-trade, order, and parameter-neighbor semantics open. Those choices
must be frozen before certified data can be observed. The owner asked Chronos to choose
the remaining answers from the pinned Confluence and the current Chronos authority/risk
configuration rather than continuing the question loop.

The controlling machine artifact is `specs/qqq_sma_control_v1.json`, SHA-256
`a0ec83b3431016df0c599895ead65083fc72b5afb87073dfbdf046d68e23bb03`.
Those bytes define the unit-exposure CVaR observation and the complete composition of
CVaR, gross, leverage, affordability, and owner-policy caps into permitted target
notional; no sizing term is inherited only from unpinned prose.
`chronos.research.qqq_control` independently hashes both that artifact and its referenced
constitution bytes before compiling typed blocked metadata. It possesses no other Chronos
capability: no market-data, holdout, trial, broker, order, or promotion import.

## Decision

### 1. Initialization and equality

The control stays flat until a complete SMA window exists. On the first full window, a
strict close above the SMA initializes LONG and a strict close below initializes SHORT;
exact equality remains flat until the first strict inequality. After initialization,
equality holds the prior direction. This avoids inventing a startup direction and avoids
turnover from a tie without importing the Five-Tool's separate hysteresis engine.

### 2. Research and execution prices stay separate

The signal and CVaR use the point-in-time total-return series frozen by D-44. Signal-time
whole-share quantity uses the confirmed raw tradable close at `t`. At the next-session
handoff, a fresh executable quote produces Chronos's existing protected marketable limit
with a 1% collar. Quantity is recomputed against the more conservative of the confirmed
raw close and the protected buy limit and may only decrease. It may never increase because
of a favorable gap.

An entry event gets one DAY attempt. A gap, failed cost check, failed revalidation, or
unfilled order consumes the event; Chronos does not chase it later. A partial fill becomes
the managed position, the remainder expires or is cancelled, and no top-up is permitted.
On a direction flip, the old direction is flattened first and authoritative flat
reconciliation is required before any same-event opposite entry. While short execution is
blocked, a bearish flip can close a long but can only leave the account in cash.

### 3. Revalidation and minimum economic trade

Immediately before handoff, exact specification and signal identity, QQQ contract identity,
fresh quote/market rule, fresh reconciliation, mode/mandate/promotion binding, kill/loss/
drawdown status, NAV/CVaR/affordability/gross/leverage evidence, and the no-upsize invariant
must all pass. Missing or ambiguous evidence creates no new exposure. Risk-reducing exits
do not depend on entry-risk evidence, but still require identity, reconciliation, and safe
order construction.

The primary economic floor is one whole share **and** projected all-in round-trip cost no
greater than 10% of the applicable CVaR dollar budget. This relates the cost floor to the
account's already-frozen tail-risk scale without manufacturing an expected-return estimate.
Missing or unbounded cost evidence blocks entry.

The CVaR denominator is now part of the pinned artifact: one unit-exposure observation is
the one-session direction-specific loss fraction on USD 1 of unlevered QQQ exposure from
the point-in-time total-return close-to-close return. Long loss is `max(0, -return)`; the
estimator is the mean of the 13 greatest loss fractions in the completed 252-return
window. It must be finite and strictly positive. The CVaR notional is its USD loss budget
divided by that unit-exposure loss fraction. Permitted target notional is the non-negative
minimum of CVaR notional, 100% gross, 1x leverage, fresh post-floor/post-cost
affordability, and fresh owner-policy notional. Any missing, stale, non-finite, or
non-positive required input creates no new exposure.

### 4. Small, prospective robustness grid

Five cells are frozen, with no cross-product of axes:

1. primary SMA-200 immediate two-state;
2. SMA-150 immediate, the shorter horizon neighbor;
3. SMA-250 immediate, the longer horizon neighbor;
4. SMA-200 with a 1% three-state neutral band—strictly outside the band is directional,
   while the band and its exact boundaries are flat; and
5. SMA-200 with five consecutive strict closes—equality resets the pending streak and
   holds an already initialized state.

Cells cannot be added, combined, selected, or redefined after seeing results. Their
evidence cannot promote the Five-Tool candidate.

## Why this fits the Confluence

- It preserves SMA as a low-degree-of-freedom control instead of copying EMA-100,
  momentum, score, AVWAP, or regime mechanics into it.
- The separate neutral-band and confirmation cells test the same broad anti-whipsaw ideas
  that the Confluence expresses natively without double-filtering the integrated candidate.
- Conservative gap re-sizing, one-shot entries, no top-ups, direction-specific CVaR, and
  the 1% protected-market collar match Chronos's current fail-closed configuration.
- The 2%/USD 60 limit remains an observed daily/session circuit breaker. It is not
  converted into per-trade risk.

## Consequences and blockers

The strategy rules are now exact enough to identify a future control campaign, but the
campaign remains blocked. No power-required N, certified catalog/release, owner-approved
holdout map, cash-leg identity, content-addressed cost schedules, evaluator, criteria,
campaign/code binding, or TradingView parity evidence exists. Short execution additionally
lacks compiler, borrow, shortability, account, and owner evidence.

This ADR does not read data, register a trial, open a holdout, select a strategy, grant
funding, authorize paper/live operation, or claim edge. Changing any material rule requires
a new preregistration identity before more evidence is observed.
