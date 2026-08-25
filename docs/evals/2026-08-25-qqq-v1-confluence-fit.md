# QQQ v1 Confluence-fit reconciliation — risky-change evaluation

Date: 2026-08-25

## Scope

This evaluation checks a research-design decision involving financial risk logic. It
does not test strategy performance, alter executable code, read a market dataset, open
a holdout, or authorize an order. Success means the recorded choices match the pinned
Five-Tool implementation, retain stricter owner limits, preserve an interpretable
control, and state every remaining blocker.

## Live repository checks

| Claim | Evidence checked | Result |
|---|---|---|
| The audited source identity is current | `sha256sum research/pine/00_five_tool_confluence_aio.pine` | Matches `e51d5a40...e45f`. |
| The Confluence uses its own direction/regime logic | Pine lines 93–124 and 533–605 | Daily 20-bar 0.85/0.55 regime, two-bar confirmation, hysteresis, and EMA-100 filter. |
| The Confluence sizes from stop distance | Pine lines 313–329 and 1519–1600 | 1% risk, structural stop when available, 2-ATR fallback, 100% notional cap, whole shares. |
| Exit option 4 is a stack, not one close rule | Pine lines 1640–1720 | Initial stop, 1R/2R, breakeven, Chandelier runner, regime exit, and side-specific AVWAP exits. |
| Calendar top-ups match the source | Pine lines 1464–1474 | False: entry requires flat state and a new qualified trigger; no weekly top-up exists. |
| Long and short defaults are symmetric | Pine lines 199–258 | False: dedicated short v2/SHORT+ are on; dedicated long v2 is off. |
| QQQ currently has strategy or order authority | `research/qqq_v1_constitution.json` and its safety test | False: selected strategy is null, registered trials are zero, and all order/promotion authority is none. |

## Judgment

The safe reconciliation is two separately identified cells. The SMA-200 rule remains a
simple attribution control. The integrated candidate retains native Five-Tool mechanics.
For the integrated candidate, source-native stop-risk quantity is capped by the stricter
direction-specific CVaR, owner drawdown/daily-loss, gross/leverage, affordability, and
whole-share constraints. This follows the general risk principle that the tightest
credible constraint binds without relabeling an outer portfolio limit as the strategy's
native sizing rule.

The prior weekly-increase choice is the one direct mismatch and is superseded. Exposure
may be reduced after each confirmed close, but may increase only on a new qualified entry
while flat. The initial quantity may still be split into the source's same-event management
legs; it cannot be topped up later merely because risk capacity increased. The 2% owner
choice remains the observed daily/session circuit breaker; the Confluence's planned
per-trade base stop risk remains its pinned 1% default on D-41's capped base.

## External-source boundary

- TradingView's official Pine execution documentation confirms that historical bar data
  is final, realtime values are not confirmed until the closing tick, and strategies use
  confirmed-close calculations by default.
- TradingView's official strategy documentation confirms that, under default calculation
  behavior, the earliest fill after a close-generated order is the next available tick,
  ordinarily the following bar's open.
- Moreira and Muir's NBER paper motivates testing volatility-managed exposure; it does
  not validate this CVaR estimator, stop rule, QQQ implementation, or parameter set.

No external source validates the Five-Tool strategy's future performance. The Pine file
is implementation authority only. The full-stack and component cells must earn separate,
prospective, post-cost evidence.

## Result

**Pragmatic partial.** The choice audit is complete and internally coherent; no strategy
or phase gate advances. Exact control initialization/equality, sizing-reference price,
gap/revalidation/economic-trade/order semantics, integrated EMA/ATR/AVWAP/support
price-domain mapping, executable position management, certified data and costs, power, a
clean holdout, and short-side evidence remain blocking.

## Verification commands

```text
sha256sum research/pine/00_five_tool_confluence_aio.pine
sha256sum research/qqq_v1_constitution.json
.venv/bin/pytest -q tests/safety/test_qqq_v1_constitution.py tests/unit/test_five_tool_contract.py
git diff --check
```

Observed for this change: 14 focused tests passed. Ruff, format, and both mypy lanes
passed in `make gates`; full pytest reproduced the untouched-base result of 3,995 passed,
1 skipped, and the same 13 Streamlit 1.62 relative-path failures.
