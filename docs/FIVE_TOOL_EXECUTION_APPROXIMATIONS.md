# Five-Tool execution and validation approximations

Status: **research-only; TradingView execution parity UNVERIFIED**.

This document covers the boundary implemented by
`chronos.research.five_tool.planning`, `chronos.research.five_tool.replay`, and
`chronos.research.five_tool.validation`. It does not authorize paper or live orders,
and none of these modules imports a broker or production order path.

## Exact arithmetic carried from Pine

Quantity is computed from the signal-bar reference price and sizing equity:

```text
risk_fraction = risk_pct / 100 * risk_scale * max(risk_multiplier, 0)
risk_quantity = equity * risk_fraction / (stop_distance * point_value)
cap_quantity  = equity * cap_pct / 100 / (entry_price * point_value)
quantity      = floor(min(risk_quantity, cap_quantity) / quantity_step) * quantity_step
```

The quantity is rejected below `minimum_quantity`. Long stop distance is
`entry - stop`; short stop distance is `stop - entry`. Both must be finite and
strictly positive. Equity, entry price, point value, quantity step, and minimum
quantity must also be finite and strictly positive. This deliberately removes the
Pine source's asymmetric final entry guard (`long: stop > 0`, `short: distance > 0`)
without changing valid-plan arithmetic.

Positions with at least three minimum lots use Pine's floored thirds and remainder
rule. Chronos fails closed if that geometry would leave target-bearing leg 1 or 2
below the minimum quantity; only a subminimum runner remainder may be merged into
leg 2. Smaller positions use one leg and target 2. Every closure carries a `LegId`
and `ExitReason`, plus the frozen planned-leg count. Validation rejects a position
until every planned leg has an explicit closure and all legs agree on entry identity;
an absent leg is never treated as evidence that a target filled.
In particular, breakeven arms only from an actual `TARGET_1` fill event. A stop,
manual exit, direct reversal, or a one-leg `TARGET_2` fill cannot arm it.

## Intrabar fill policies

The Pine strategy enables TradingView's bar magnifier. Chart-timeframe OHLC does not
contain enough information to reproduce the order in which a target and stop traded.
Every research replay must therefore record one of these policies:

| Policy | Behavior when stop and target both trade in one bar |
|---|---|
| `ohlc_stop_first` | Fill the stop. This is the conservative chart-OHLC approximation. |
| `ohlc_target_first` | Fill the target. This is an optimistic sensitivity bound. |
| `lower_timeframe_magnifier` | Traverse complete, chronological lower-timeframe bars. Within an ambiguous lowest-resolution bar, use stop-first. |

The magnifier policy requires this evidence for **every** replay bar, including bars
with no active position or executable order: explicit parent/sub-bar start and end
timestamps, one uniform lower resolution, chronological continuous coverage of the
complete parent interval, matching symbol/source identity, and exact reproduction of
parent open, high, low, and close. Any missing bar, gap, stale bar, or identity ambiguity
fails rather than falling back. It is still an approximation unless the supplied
resolution captures the true event sequence; it is not a claim of TradingView parity.

Stop orders use explicit stop-market gap semantics: an adverse gap through the stop
fills at the bar open. Target limits use price-improvement semantics: a favorable gap
through the limit fills at the better open. A stop and target for one leg are OCO; the
resolver emits at most one fill and records the cancelled sibling reason.

## Causal signal-to-ledger replay

`replay_five_tool` now connects the signal engine to the pure fill planner and the
closed-leg validator. It replaces every caller-supplied account snapshot with replay-owned
state, queues entries and discretionary exits at confirmed close, and fills them at the
next bar's explicit open timestamp. Signal-time quantity and risk distance stay frozen;
the later-bar stop and target ladder is rebased to the adverse next-open entry execution
price. Every entry leg and closing leg has an explicit fill receipt, cost attribution,
position id, setup/regime identity, and deterministic result digest.

The entry bar has a distinct frozen order set that follows the checked Pine source's
submission timing:

- the absolute signal-time `PROT_*` stop is eligible for every leg on the entry bar;
  a touch fills at the stop and an adverse gap fills at the explicit entry-bar open;
- targets are inactive on the entry bar; after that bar, the stop/target ladder uses the
  actual adverse next-open execution price without resizing signal-time quantity or risk;
- target-1 evidence may arm breakeven, and close-time trailing may tighten a runner, only
  for later bars. No within-bar order recalculation is inferred from chart OHLC.

The source pins `calc_on_order_fills=false`, `process_orders_on_close=false`, and
`use_bar_magnifier=true`. Chronos preserves the pre-submitted entry stop but remains an
OHLC approximation: entry/stop event ordering inside the smallest supplied bar, target
activation, and later stop updates have not been reconciled to TradingView fills.

A queued close-time discretionary exit executes at the next open before protective
resolution on that bar. On ordinary later bars, protective resolution precedes the new
close-time signal evaluation. These priorities, entry-stop geometry, fill-rebased ladder,
target-limit slippage toggle, terminal policy, costs, and full-origin replay scope are
serialized in `FiveToolReplayPolicy.canonical_payload`; its SHA-256 is the campaign-bound
replay-policy identity.

These are Chronos research policies, not TradingView parity claims. A replay must freeze
the OHLC priority policy, commission, slippage, tick/point/quantity settings, and terminal
position policy. `require_flat` rejects pending, open, or partially closed terminal state.
`exclude_incomplete` retains the partial evidence but excludes the entire affected
position—including already closed legs—from economic-position statistics.

Each result also binds an effective replay-input SHA-256 covering every primary and
companion value/identity, every explicit primary open timestamp, and every lower-timeframe
identity/OHLC record. Caller-supplied account snapshots are deliberately excluded from that
input digest because the adapter overwrites them; the adapter-owned snapshots are present
in the result and therefore in the result digest.

Replay is full-from-origin only. Every call constructs a fresh engine and must begin
exactly at `settings.history_start_utc`. There is no replay-ledger checkpoint/resume API;
suffix or chunk continuation is unsupported rather than treated as equivalent evidence.

## Side ownership and sleeve reconciliation

Every fill owns the side of the economic position that created it. Sleeve equity is
updated from explicit side-owned deltas, not from the previous or resulting aggregate
position sign. This matters on a direct reversal, where a long close and a short entry
cost can occur in the same account-equity observation. Reconciliation requires:

```text
sum(long-owned deltas, short-owned deltas) == new account equity - prior account equity
```

An attribution referencing a fill must match that fill's side. Unknown fills, duplicate
fill IDs, side disagreement, or an unreconciled account delta fail loudly. Mark-to-market
deltas without fills still require an explicit side label.

## Validation accounting

TradingView's `strategy.closedtrades` ledger counts closed entry legs. The Five-Tool
strategy can create three legs for one thesis, so those records are not independent
trades. The validation report always exposes both counts and aggregates all records
sharing `position_id` before calculating economic-position evidence. Aggregation does
not establish statistical independence between positions.

Strict OOS membership requires
`economic_position.entry_time_utc >= frozen_oos_start`. Boundary-straddling
positions and all their legs are purged because no frozen boundary mark exists.
This avoids leaking pre-boundary economics and the Pine dashboard's equal-count
fiction. The Pine-compatible equal-count closed-leg chunks remain available only
under the fixed label
`heuristic_equal_count_closed_legs_not_walk_forward`.

Profit factor never uses Pine's `999` sentinel:

- `finite`: gains and losses exist, with a numeric ratio;
- `unbounded_no_losses`: gains exist and losses do not, with no numeric infinity;
- `undefined_no_trades`: the sample is empty;
- `undefined_no_gains`: there is no positive payoff (numeric display value `0`, tagged
  so it cannot be confused with a normally estimated ratio).

Only `report.oos` counts, net P&L, and profit factor use the strict OOS set today.
Gross P&L/costs, turnover, concentration, risk, regime/instrument slices,
parameter sensitivity, and removal tests use the full sample and cannot satisfy
the preregistered OOS gates. Drawdown is cumulative closed-economic-position P&L
drawdown, not daily mark-to-market equity drawdown; CVaR is the lower tail of
economic-position dollar P&L. Parameter plateau support checks positive sign among
the best variant's declared neighbors, not every common statistical gate, and is
unknown without a frozen valid neighbor graph. Empty and small samples return
tagged unknowns and warnings; they do not manufacture reassuring zeros or a
promotion verdict.

## Known non-parity and deferred evidence

- Genuine owner-captured TradingView exports have not been provided. Signal and fill
  parity therefore remain **UNVERIFIED**.
- Commission and slippage values in validation are ledger inputs. They do not prove the
  Pine emulator used the same per-fill amounts.
- The report's CVaR is the lower tail of chronological economic-position net dollar P&L,
  not a daily return-series CVaR. The basis is embedded in the report.
- Lower-timeframe OHLC can narrow, but cannot eliminate, event-order ambiguity inside
  its smallest bar.
- The pure engine-to-fill-to-`ClosedLeg` replay is implemented and deterministically
  tested, but it is a Chronos approximation. It has not been reconciled to an
  owner-exported TradingView trade list, so fill/execution parity remains **UNVERIFIED**.
- Daily-loss halt behavior, alert delivery/de-duplication, and TradingView account-equity
  mark timing live outside this pure planning boundary and require separate parity
  traces.
