# ADR-0012: Options chain/IV/greeks forward capture (Milestone C0)

Status: proposed
Date: 2026-07-19

## Context

AI Quant plan C0: scheduled snapshot capture of option chains / IV / greeks for
allowlisted underlyings into the local store, with provenance manifests and
**staleness recorded, not hidden**. It is flagged *deploy ASAP* because **IBKR
provides no historical data for expired options — there is no backfill path at any
spend level**, so every week without capture is unrecoverable history. $0-tier
capture is delayed / EOD-snapshot quality (real-time OPRA is a paid subscription
IBKR gates on account minimums).

C0 rides on the C1 topology (ADR-0011): it runs in the **same read-only data
process**, inherits its structural isolation (no orders / broker-write / persistence
/ writer-lease / `sqlalchemy` / `sqlite3`; `ibapi` lazy), reuses its pacing
controller and manifest discipline, and the real fetch stays **owner-gated**
(invariant 8) — CI exercises a fake client only.

The trading plane already has a full options vocabulary (`domain.OptionChainParameters`,
`OptionContract`, `MarketQuote`, `ModelGreeks`, `DataQuality`), but C0 does **not**
reuse it: following the C1 precedent (histdata defined its own `CorporateAction`),
the persisted snapshot is a **histdata-local, versioned schema** decoupled from the
live domain models, so a domain refactor can never silently change a stored snapshot
or its provenance hash, and the isolation allow-list stays unchanged.

## Decision

### 1. Snapshot schema — histdata-local, staleness first-class

`OptionQuoteRow` (frozen): `expiration`, `strike`, `right` (CALL|PUT), `bid`, `ask`,
`last`, `close`, `volume`, `open_interest`, `delta`, `gamma`, `theta`,
`implied_volatility`, `data_quality` — every market field **Optional**, `None` when
the gateway did not return it (**never fabricated**). `OptionChainSnapshot` (frozen):
`underlying`, `captured_at` (the capture clock, passed in — not generated in pure
code), `source`, `spot` (the underlying mark at capture, Optional), `expiry_horizon_days`
and `strike_window_pct` (the **bounds actually applied** — see §3), and the `rows`.

Staleness is **first-class**: each row carries its quote's `data_quality`
(`LIVE`/`FROZEN`/`DELAYED`/`DELAYED_FROZEN`/`STALE`/`UNKNOWN`), and the snapshot
exposes a `quality_histogram` + `worst_quality`. A delayed/frozen snapshot is
captured and **labeled**, never dropped or silently upgraded.

### 2. Store layout — append-only immutable daily snapshots

Extends the C1 tree: `research/data/history/options/<SYMBOL>/<YYYY-MM-DD>.json`, one
EOD snapshot per underlying per day, **immutable**. A re-capture of an existing date
fails closed unless the caller passes `--allow-correction` (the same deliberate,
logged supersede as bars, ADR-0011 §3). SHA-256 + provenance (source, capture time,
row count, quality histogram, bounds) recorded in `MANIFEST.json`; the store ships
**empty** — C0 is the pipeline, the first real snapshot is an owner-run step.

### 3. Bounded selection — recorded, never silently truncated

The full chain is unbounded; C0 captures a **bounded window**: expirations within
`option_capture_expiry_horizon_days` (setting, default 120) and strikes within
`option_capture_strike_window_pct` of spot (setting, default 0.20), both rights. The
applied bounds are **stored in the snapshot**, so a reader knows exactly what was and
was not captured — a strike outside the window is *absent by policy*, not missing
data. If no expiration falls in the horizon (or spot is unavailable so the strike
window can't be centered), the snapshot is written **empty with a recorded reason**,
never a silent no-op.

### 4. Port + fake + owner-gated official client

`OptionSnapshotClient` port: `fetch_chain(underlying) -> ChainParams` (expirations +
strikes + multiplier) and `fetch_quotes(underlying, contracts) -> tuple[OptionQuoteRow]`.
`FakeOptionSnapshotClient` (in `tests/support/`) returns deterministic rows for CI.
`OfficialIBKROptionClient` lazily imports `ibapi` and issues `reqSecDefOptParams`
(chain) + `reqContractDetails` (qualify) + `reqMktData` with the model-greeks /
delayed tick types; **structurally present, unexercised in CI** (invariant 8). Option
requests reuse the C1 `PacingController` (they are rate-limited too).

### 5. Isolation inherited + extended, scheduling is an owner step

The new modules live in `chronos.histdata` and stay inside its forbidden-import
boundary; the existing subprocess + AST isolation tests already walk **every** module
in the package, so they cover C0 automatically (a new test asserts the options
modules specifically import nothing forbidden). C0 adds a `capture-options`
entrypoint (`python -m chronos.histdata capture-options`); the actual *scheduling*
(cron/systemd-timer against the owner's gateway) is a documented deployment step in
the runbook — C0 ships no daemon and starts no clock in pure code.

## Honesty bounds (README / limitations / runbook)

- Real `reqSecDefOptParams` / `reqMktData` capture is **owner-gated and unexercised**
  in CI (invariant 8); the store ships empty; no greeks/IV are fabricated.
- $0-tier data is **delayed / EOD-snapshot quality**, recorded per-row as
  `DataQuality` and per-snapshot as a histogram — never presented as live.
- There is **no expired-options backfill at any price** (forward capture only); the
  captured surface accrues at calendar speed and will span few volatility regimes for
  years. Frozen-criteria Wheel validation therefore remains gated on either a paid
  vendor (owner decision N2) or an accepted multi-year horizon (C5 restates this).
- Capture bounds are recorded; an out-of-window strike is absent *by policy*.

## What proves it

Snapshot schema + JSON round-trip (incl. `None` market fields, quality histogram);
store append-only + fail-closed conflict + logged `allow_correction`; capture
coordinator selects within the configured bounds and records the applied bounds +
staleness histogram; a `DELAYED`/`FROZEN` snapshot is captured-and-labeled; an
empty-horizon capture writes an empty snapshot with a reason; the fake-client happy
path; and the isolation test covers the new modules.

## Consequences

The research plane gains a provenance-clean, owner-runnable options-capture pipeline
that is structurally incapable of placing an order, ready to start accruing the
otherwise-unrecoverable options history the moment the owner points it at a gateway.
Nothing in the trading/live plane changes; the single `transmit=True` boundary and
the C1 isolation are untouched; the store ships empty and honest about $0-tier
staleness.
