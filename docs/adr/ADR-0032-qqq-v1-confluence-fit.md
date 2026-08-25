# ADR-0032 — QQQ v1 control and Five-Tool Confluence integration boundary

Status: **accepted — owner directive, 2026-08-25; research-only and no trading
authority.** Index entry: DECISIONS.md D-46.

## Context

The owner selected QQQ v1 design inputs one at a time in D-36 through D-45, then
selected exit option 4—the Five-Tool Confluence invalidation approach—and directed a
complete audit of every prior answer for fit with the actual Confluence.

The audit found that one blended cell would answer neither research question cleanly.
The SMA-200 cell was chosen as a low-degree-of-freedom trend attribution control. The
Five-Tool source is a materially different system: it has its own EMA-100 direction
filter, volatility-normalized regime with two-bar confirmation and hysteresis,
multi-trigger score-based entries, stop-distance sizing, and layered position
management. Replacing those mechanics with the control's settings would no longer be
the Five-Tool Confluence; adding all of them to the control would no longer isolate
simple trend.

The exact implementation authority audited here is
`research/pine/00_five_tool_confluence_aio.pine`, SHA-256
`e51d5a40d2e933bf86847c7432364ba8934fd2de653d6aec3d7205639248e45f`.
This is implementation evidence only, not evidence of edge.

## Decision

### 1. Keep two separately identified cells

1. **Simple trend control.** Confirmed point-in-time total-return QQQ close versus
   trailing SMA-200, with D-37's immediate two-state transition. A signal flip is the
   control's exit. It has no Five-Tool momentum, divergence, Mansfield relative
   strength, AVWAP, regime hysteresis, score gate, or setup-specific exit. D-38's
   CVaR sizing remains the control's primary sizing method.
2. **Five-Tool integrated reference/candidate.** Preserve the pinned source's native
   daily defaults rather than substituting the control: 20-bar volatility regime,
   0.85/0.55 enter/exit z thresholds, two confirmed bars, volatility-percentile
   adjustment, hysteresis, EMA-100 direction filter, the enabled entry triggers, and
   minimum score 55. Dedicated short v2 and SHORT+ stay on; dedicated long v2 stays
   off, exactly as the pinned source defaults specify. Changing that asymmetry is a
   separately identified variant, never an undocumented cleanup.

The integrated cell is not an attribution result. It must receive its own campaign
identity, trial count, evidence, and promotion decision. Evidence from the simple
control cannot promote it, and full-stack results cannot be attributed to SMA-200.

### 2. Exit option 4 means the exact Confluence protection stack

For the integrated cell, “Confluence invalidation” is not an AVWAP-only close. It is
the exact layered source behavior:

- an initial structural stop when the active side implements and supplies one;
  otherwise the source's 2.0 ATR fallback using ATR-14;
- 1R and 2R targets when quantity can be split, breakeven after Target 1, and a
  22-bar/3-ATR Chandelier runner activated after 1R;
- an opposite confirmed regime exit; neutral alone does not exit by default;
- a short AVWAP-reclaim exit; a long AVWAP-failure exit only when the separately
  controlled dedicated-long-v2 mode is enabled, matching the source rather than
  pretending the default is symmetric; and
- no added fixed time stop or SMA-200 exit inside the integrated cell.

The simple control retains its signal-flip exit. This is necessary for attribution
and is not a rejection of option 4; option 4 governs the Confluence cell for which it
was chosen.

### 3. Native stop-risk sizing sits inside the owner risk envelope

The integrated cell first computes the source-native quantity from stop distance and
a **1.0% per-trade base stop-risk budget**, using D-41's capped applicable base rather
than allowing strategy gains to compound it above the USD 3,000 reference. That is
distinct from the owner-frozen 2.0%
observed daily/session loss halt. At the initial USD 3,000 research reference these
are respectively USD 30 of planned stop risk and a USD 60 observed-loss halt.

The permitted integrated quantity is the minimum allowed by all applicable
constraints:

- native stop-distance sizing, including the pinned SHORT+ multiplier only if the
  short side later becomes certifiable and executable;
- D-38 through D-41's direction-specific 95% historical CVaR ceiling of 1.5% of the
  applicable base, never more than USD 45;
- the 100% gross, 1x leverage, capital/affordability, and owner-policy ceilings; and
- D-43's whole-share round-down rule.

Thus CVaR remains an independent outer veto and size cap; it does not replace the
Confluence's structural-stop risk engine. Missing or uncertifiable evidence in either
layer produces no new exposure. The owner-frozen 10% drawdown and 2%/USD 60 observed
daily/session halt override the Pine defaults, whose 25% entry pause and disabled 3%
daily-loss pause are too loose for this constitution.

### 4. Entry events, not a calendar, control increases

D-42's weekly increase schedule is superseded for these cells. Risk evidence is still
recomputed at every confirmed close, and a required reduction remains eligible next
session. New or increased exposure is allowed only on a new confirmed entry event
while flat:

- an SMA-200 direction transition for the simple control; or
- the pinned Confluence entry event for the integrated cell.

There is no weekly top-up or later in-position add-on. The source may split the initial
same-event quantity into as many as three management legs; that is not a later exposure
increase. A later increase in permitted CVaR capacity does not resize an existing position
upward. A halt, stale input, failed gate, or smaller risk allowance may still reduce or
close exposure promptly. This matches the source's `strategy.position_size == 0` entry
boundary while preserving Chronos's stricter safety response.

### 5. Prior-answer audit

| Prior choice | Verdict | Confluence-fit treatment |
|---|---|---|
| D-36, SMA-200 direction | Keep, scoped | Best simple control among the considered choices. It does not replace the integrated cell's native EMA-100. |
| D-37, immediate transition | Keep, scoped | Correct for the control. The integrated cell keeps native two-bar confirmation and hysteresis; adding the 1%/five-close variants there would double-filter it. |
| D-38, CVaR primary sizing | Reinterpret for integrated cell | Remains primary for the control and an outer cap/veto for the integrated cell, whose native engine is stop-distance sizing. |
| D-39, empirical 95% CVaR-252 | Keep | Transparent outer risk estimate; its sparse 13-loss tail remains a disclosed limitation. |
| D-40, separate long/short tails | Keep | Required by the Confluence's intentional long/short asymmetry. Short remains unavailable without borrow/cost/account evidence. |
| D-41, `min(NAV, USD 3,000)` base | Keep | Prevents risk expansion after gains and applies outside both cells. |
| D-42, daily reductions/weekly increases | Supersede | Daily reductions stay; calendar increases become new-entry-event-only, with no later add-on after the same-event entry legs. |
| D-43, whole-share floor | Keep | Matches the source's default quantity step and minimum quantity of one. |
| D-44, point-in-time total-return research/raw execution prices | Keep, with an open mapping | A deliberate Chronos integrity correction; no future corporate-action knowledge may leak. The integrated EMA/ATR/AVWAP/support price-domain mapping must be frozen before parity or data access. |
| D-45, include confirmed session | Keep | Matches confirmed-bar semantics while preserving next-session action. |

## Relationship to existing decisions

- **D-35/ADR-0031 remains the governing constitution.** QQQ-only execution target,
  USD 0 live allocation/risk, the evidence hurdles, 10% drawdown, 2%/USD 60 halt,
  1.5%/USD 45 CVaR, 100% gross, 1x leverage, and zero incremental recurring data
  budget are unchanged.
- **D-38 is narrowed, not erased.** CVaR remains the simple control's primary sizing
  method and an independent outer limit for every cell. It is not relabeled as the
  Confluence's native sizing engine.
- **D-42 is superseded on increase timing.** No unresolved weekly anchor remains.
- **D-12/ADR-0008 remains the executable boundary.** The short side is still refused,
  and this ADR does not enable dedicated long v2, shorting, orders, or live operation.
- **D-22(a) and the Five-Tool campaign blockers remain unchanged.** This semantic
  reconciliation does not resolve evaluator bindings, certified data, or trial
  authority and cannot make the current campaign executable.

## Consequences

- The design now distinguishes the rule used to identify trend evidence from the
  strategy intended to express the owner's Confluence.
- The 2% owner choice is preserved as a circuit breaker, not silently converted into
  2% risk on every position. The Confluence-native planned stop risk starts at 1%.
- Option 4 is selected without discarding the initial stop, profit-taking,
  breakeven, runner, or regime exits already present in the strategy.
- Full-stack complexity pays its own multiplicity and cannot borrow a favorable
  result from the simple control.
- The exact sizing-reference price, entry-gap handling, pre-handoff revalidation,
  minimum economic trade, cash-leg identity, integrated feature price-domain mapping,
  equality/initialization rules for the control, certified costs/data, power, clean
  holdout, and executable position lifecycle remain blocking.

No dataset was read, no trial was registered, no holdout was opened, no strategy was
selected for promotion, and no funding, short-selling, submission, paper, or live
authority is granted by this decision.
