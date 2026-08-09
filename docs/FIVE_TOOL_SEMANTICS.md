# Five-Tool v3.6 executable semantics and evidence boundary

Status: **research-only; TradingView and execution parity `UNVERIFIED`**.

This document describes what the Catalog `00` vertical slice can establish today.
It is not a profitability claim, strategy promotion, or paper/live authorization.

## Frozen identity

| Artifact | Identity |
|---|---|
| Pine source | `research/pine/00_five_tool_confluence_aio.pine` |
| Pine SHA-256 | `e51d5a40d2e933bf86847c7432364ba8934fd2de653d6aec3d7205639248e45f` |
| Logical lines / inputs | 2443 / 219 source-ordered inputs |
| Input-contract digest | `93273762b1d01dade4133628a9a2cebf0a1364774fde654a9efc07c4ccf6d049` |
| Semantic-declaration digest | `1c9f5b386d63732e8e9fab3e3e3e7173721590f84884101ef78817b8b3ab1531` |
| Checkpoint schema | `five-tool-state-v2` |

`specs/five_tool_confluence_v3_6.yaml` freezes every Pine input, timing rule,
dependency stage, warm-up declaration, and known deviation. Its semantic section is
a reviewed declaration, not a second implementation of every formula and therefore
not an independent oracle for the Python engine.

## Executable layers

1. `chronos.research.five_tool.contract` verifies the pinned Pine bytes and parses the
   complete input contract. `FiveToolSettings` requires the current digest, all 219
   names in source order, correct types/options/bounds, a real IANA timezone, and
   positive point/tick values. Frozen `timestamp(...)` literals remain inspectable in
   the source contract, while executable Pine `input.time` values are normalized to
   their native signed UNIX-millisecond integers.
2. `align_five_tool_inputs` binds benchmark ticker and exchange, common feed source,
   higher timeframe and EMA length, sessions, and timezone directly from those
   settings. It accepts closed bars only, rejects source/exchange drift inside a series,
   carries benchmark gaps from the latest bar at or before the primary close, and
   exposes only a strictly prior completed higher-timeframe bar. Equal/lower requested
   timeframes are inert, matching the Pine guard. A higher requested timeframe requires
   an explicit primary-symbol/source/exchange-matched series; valid Pine resolutions
   outside Chronos's current `BarInterval` vocabulary fail immediately instead of
   silently disabling the filter.
3. `FiveToolEngine` emits one immutable closed-bar feature/gate/intent trace. It covers
   the internal/external regime selection, hysteresis and confirmation, Mansfield
   relative strength, benchmark/HTF filters, confirmed pivot divergence, flip-anchored
   VWAP/value zones, legacy and v2 long/short setups, Markov/dwell gates, entry
   protection halts, discretionary exit intents, block counters, and distinct regime
   flip versus entry/exit events.
4. `evaluate_batch` deliberately repeats the same causal `step` kernel. Arbitrary
   chunking plus `state_to_json`/`state_from_json` is required to reproduce the same
   traces. That proves deterministic replay and checkpoint integrity, not independent
   formula agreement.
5. `planning` and `validation` are separate pure research components for sizing,
   three-leg plans, explicit fill milestones, OHLC fill policies, sleeve accounting,
   closed-leg aggregation, and descriptive evidence. The signal engine does not drive
   these components in an end-to-end replay. Account position/equity and fill outcomes
   remain caller-supplied facts.

No Five-Tool module is registered as a runtime strategy or imports broker/order,
mandate, service, production persistence, or live promotion surfaces. The separate
trial-control module owns an append-only, fsynced research ledger; it is not imported
by the signal/planning package. That ledger is local to a caller-selected path and is
not the canonical ADR-0013 global experiment registry.

## Timing and causal identity

- `Bar.timestamp_utc` is a bar-close timestamp. The first consumed close must equal
  `settings.history_start_utc`; changing it changes settings identity and expanding
  Markov/dwell evidence.
- The manifest's current daily `2010-01-04T00:00:00Z` is an unresolved placeholder.
  Before any campaign can execute, it must be replaced or reconciled with the certified
  dataset's exact first bar-close timestamp; a calendar date is not sufficient.
- Primary symbol, source, exchange, and interval cannot change mid-stream. Benchmark
  ticker/exchange and feed source must match their configured identity; HTF
  symbol/source/exchange must match the primary series. Full source identities are
  retained in every trace, and HTF close and EMA must name the same completed source
  bar.
- Pivot events publish only after the configured right-bar confirmation delay. No
  value is shifted back to the visual pivot bar.
- Pine session/date gates use bar-open time. Chronos reconstructs this as close minus
  one fixed interval. That is exact for regular intraday bars but remains an explicit
  approximation for daily bars, holidays/half-days, exchange-calendar gaps, and other
  discontinuities.
- External regime/strength mappings are timestamp-keyed caller inputs but do not yet
  carry a content-addressed source identity. AVWAP `input.source` supports `hlc3` and
  `close`; any other external series fails rather than being guessed.

## Pine behavior preserved versus deliberately corrected

Preserved source behavior includes the daily-loss halt being inert on 1D bars, volume
expansion gates passing while their moving average is `na`, strict LONG+ age/maturity
ceilings even though nearby prose calls them optional, and positive-weight-observation
counting for the AVWAP stale guard.

The research evidence layer deliberately does not copy these unsafe or ambiguous Pine
behaviors:

- long and short sizing both require a positive stop price and directional distance;
- split geometry fails closed when target-bearing legs would be subminimum;
- T1/T2 and break-even state advance only from explicit leg/reason fill events;
- sleeve P&L and fees require explicit side-owned attribution, including reversals;
- static and dynamic alert paths collapse to one semantic event identity, while a
  simultaneous regime flip remains a separate event;
- legacy shorts keep their selected setup identity instead of Pine's unconditional
  SHORT+ alert label;
- profit factor uses tagged finite/unbounded/undefined states, never the `999` sentinel;
- OOS reports purge every position opened before the boundary when no boundary mark is
  available, rather than assigning a multi-leg position by final exit time.

Fill-policy details and additional deviations are in
`docs/FIVE_TOOL_EXECUTION_APPROXIMATIONS.md` and the semantic contract.

## Inputs outside the signal kernel

All 219 inputs are frozen and validated, but 39 are not consumed directly by the
signal engine/alignment path. They belong to Pine order enablement; sizing/leg policy;
display/HUD/JSON alerts; external `input.source` selectors; or the Pine validation
dashboard. Planning accepts explicit typed sizing/fill requests instead of silently
reading those settings, and validation accepts an explicit closed-leg ledger. Until an
end-to-end adapter binds those values and identities, they are not execution parity.

## TradingView evidence boundary

`chronos.research.tradingview` projects real engine traces into a strict, closed-bar
schema and reports the first exact/tolerance-aware divergence. The checked fixture is
`internal_spec`; its pinned golden digest is a Chronos regression only.

No owner TradingView export, detached attestation, reviewed decision-field normalizer,
or Strategy Tester trade reconciliation is present. The Pine `*_EXPORT` plots also do
not directly expose every v1 decision field. Consequently an exact trace match remains
`UNVERIFIED`; a trusted genuine reference can currently establish only a scoped
mismatch (`FAILED`). The trace has no fill time/price/quantity/commission lifecycle, so
execution parity is always separate and `UNVERIFIED`.

Official Pine references were sufficient to pin missing-slot behavior for
`ta.percentrank` and first-bar flow for `ta.mfi`. They do not settle flat-series RSI/MFI
zero-denominator results, the internal first-TR/degenerate behavior of `ta.dmi`, or
pivot selection across equal plateaus. Those edge cases remain `UNVERIFIED` and require
genuine TradingView rows; Chronos does not promote its current deterministic choices to
platform facts.

## Validation and campaign boundary

The report rejects partial positions, distinguishes Pine closed legs from complete
aggregated economic positions, and implements tagged PF, costs, turnover,
closed-position drawdown/CVaR, concentration,
slices, neighbor sign sensitivity, and best-position/month removal. Only OOS
count/net/PF currently use the strict OOS set; the other metrics are full-sample.
Benchmark-alpha intervals, DSR, FWER/FDR, PBO, power, and fully OOS-native risk/cost
gates are not implemented verdicts.

The checked campaign manifest remains blocked before any reader, and there is no
certified dataset capability or holdout guardian/unlock path. Public v1 validation
refuses `ready_for_certified_research` even if a caller clears blocker strings and fills
syntactically valid digests: readiness requires capabilities a manifest cannot grant.

The private synthetic lifecycle harness exists only to verify start-before-callback,
data-return hashing, terminal accounting, interruption recovery, local sealing, and
concurrency. Its reader and evaluator are arbitrary callbacks; data may have been
preloaded or additional sources may be touched outside its observation. Its
`ledger_trial_count` is path-local, evaluation artifact bytes are not retained in a
replay store, and supplied variance evidence has no reviewer attestation here.
Consequently it is neither a brokered data reader nor full-campaign replay evidence.
Integration with the canonical ADR-0013 registry and a certified reader is required
before Phase-3 multiplicity or final scores exist.

The engine currently retains complete observations/equity histories and recomputes
history-dependent indicators. Its time cost is approximately quadratic and checkpoint
state grows without a fixed bound. This is acceptable for small deterministic fixtures,
not yet for a large campaign.
