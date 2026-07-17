# ADR-0005 — Closed-bar deterministic engine; next-bar fill model

Status: Accepted (2026-07-17). Index entries: DECISIONS.md D-05 and D-06.

## Context

The strategies derive from a Pine Script corpus. Pine's repainting hazards concentrate in intrabar
recalculation (`calc_on_every_tick=true`) and same-bar fills. Backtests that cannot be replayed
exactly are unusable as promotion evidence.

## Decision

**One engine semantics for simulation and live paths (D-05):**

- Bars are processed only when closed. `Bar.status` must be `CLOSED`
  (`src/chronos/marketdata/bars.py`); there is no intrabar code path anywhere in the platform,
  which removes the `calc_on_every_tick` repainting class by construction. This matches Pine
  `barstate.isconfirmed` semantics with `calc_on_every_tick=false`.
- All timestamps are timezone-aware UTC internally; `session_date` carries the exchange trading
  day. Naive datetimes are rejected at construction.
- Indicator math is IEEE-754 float64 (Pine's numeric model); money and order prices cross into
  `Decimal` at the intent boundary and round to tick.

**Backtest fill model (D-06), implemented in `src/chronos/backtest/engine.py` and
`src/chronos/execution/brokers/simulated.py`:**

- Signals are computed on the close of bar t; orders can fill no earlier than bar t+1. There are no
  same-bar fills and no same-bar entry+exit.
- Limit fills are conservative and marketable: a buy fills at `min(open, limit)` when the bar's low
  trades through the limit; a sell fills at `max(open, limit)` when the high reaches it.
- Day orders expire at the end of the bar following submission.
- Costs: commission per share with a per-order minimum (defaults 0.005 USD/share, 1.00 USD minimum
  — the IBKR Pro fixed-tier assumption, ASSUMPTIONS.md A-20) plus configurable per-side slippage in
  basis points.
- Protective stops are modelled as resting stop-limits: a bar trading through the stop generates an
  exit through the normal proposal → risk → execution path, filling on the next bar.

## Consequences

- Backtest equals replay: repeated runs over identical inputs produce identical equity curves and
  trade lists (asserted in tests).
- Results are conservative: no mid-bar entries, no optimistic limit fills, costs on every trade.
- Intraday strategies cannot be expressed in this engine as built; only `DAY_1` bars are validated
  for research today (`BarInterval` docstring). This is deliberate (see ADR-0008).
- The same `ExecutionEngine`, `RiskEngine`, and state machine run in backtest and would run in
  shadow/paper, so simulation exercises the real order path, not a parallel one.
