# Parity Report — Pine to Python (Phase 4)

## Verification level: TRANSLATION VERIFIED AGAINST SPECIFICATION

No TradingView strategy-tester exports or indicator series exports were
provided for this build (ASSUMPTIONS A-03), and TradingView is not reachable
from this environment. Therefore **nothing in this repository claims
"verified against TradingView."** The verification chain actually
established is:

```
Pine source (SHA-256-pinned)
   → canonical specification (specs/*.yaml, schema-validated,
     every deviation enumerated)
   → deterministic Python implementation
   → parity tests: implementation == specification == batch indicator library
```

Upgrading to true TradingView parity requires the owner to export, from the
exact pinned script versions: bar-by-bar Data Window series for the
`*_EXPORT` plots and the strategy tester trade list (symbol, timeframe,
timestamps, side, prices, quantities). Fixture directories are prepared:
`fixtures/tradingview/` (empty, with README).

## What was verified

### Indicator layer (tests/parity/test_indicator_reference.py)

`sma`, `ema`, `rma`, `rsi`, `atr`, `stdev`, `percentrank`,
`highest/lowest` pinned against hand-computed reference sequences with
documented derivations, including RSI edge cases (all-gain window → 100,
flat window → 50 by implementation definition) and warm-up/NA semantics
(values before the window are `None`, mirroring Pine `na`).

### Incremental-vs-batch equivalence (tests/parity/test_incremental_vs_batch.py)

The strategies run incrementally (O(1)/bar recursions). Tests assert the
recursions equal the batch library on deterministic pseudorandom walks at
multiple probe points (relative tolerance 1e-9), and that the
continuity-reset path (non-contiguous history) rebuilds to identical
proposals as a fresh instance.

### Behavioral invariants

- Closed bars only; decisions at bar t can fill no earlier than bar t+1;
  same-bar entry+exit cannot occur (engine construction).
- Determinism: identical inputs → byte-identical equity curves and trade
  lists (asserted in backtest smoke and chaos determinism tests).

## Known, deliberate divergences from Pine semantics

| # | Divergence | Justification |
|---|-----------|---------------|
| 1 | `ema`/`rma` seeded with SMA(n) instead of Pine's first-value seeding for `ema` | Pine's own `rma` (and Wilder's definition) seeds with SMA; for `ema` the seeding difference decays geometrically (factor (1−α) per bar; <0.2% of the seed delta remains after ~3n bars). All warm-up-sensitive entries are additionally blocked until full `warmup_bars`. |
| 2 | Entries are next-bar marketable **limits** (close × 1.001), not Pine's next-bar-open market orders | The platform prohibits market orders (risk doctrine). Consequence: some Pine fills that gap up >0.1% are *missed* rather than filled worse — a conservative divergence, measured in research by the delayed-entry/missed-trade stress runs. |
| 3 | Protective stop modeled as limit-at-stop on the bar after the stop bar in backtests | No intrabar data; gap-through risk is real and is stress-tested via slippage scenarios; live/paper carries the stop on the intent for the risk engine. |
| 4 | `regime_trend_v1` implements the BULL+ *core gate only* (regime engine + Markov stay gate + ATR stop) | The AVWAP/RS/setup layers (L1P/R/A/B), pyramiding, micro-gates and in-script halts are omitted; the platform risk engine owns halts. Enumerated in `specs/regime_trend_v1.yaml` translation_notes. This is a derived strategy, not a clone. |
| 5 | `mean_reversion_v1` omits the session-VWAP sigma-stretch flag component | Impossible on daily bars; recorded in `specs/mean_reversion_v1.yaml`. The derived strategy is simpler than the study's intraday flags. |
| 6 | Pine float NaN (`na`) propagation vs Python `None` | The indicator library returns `None` during warm-up and strategies refuse to act on `None`; Pine would likewise gate on `na` checks in the audited sources. |

No tolerance is used anywhere except the 1e-9 relative tolerance for
float-recursion equivalence, which covers ordering-of-operations differences
between incremental and batch summation — not behavioral differences.

## Mismatch process

Any future TradingView-fixture mismatch must be recorded here with: first
divergent timestamp, Pine value, Python value, input bars, state, root
cause, and resolution. As of this build there are **zero unexplained
mismatches at the level verified** and **no TradingView-level verification
performed**.

## Consequence for eligibility

Because parity is specification-level only, both derived strategies carry
status `research_prototype` in their specs, and the promotion gates in
docs/GO_LIVE_CHECKLIST.md list "TradingView reference exports provided and
parity fixtures green, or limitation explicitly accepted by the owner" as an
open item for paper-mode entry.
