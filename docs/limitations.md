# Chronos — limitations

The honest, consolidated list of what Chronos does NOT do, cannot yet prove, or defers to an
owner action. Chronos is pre-release, local-first decision-support software; it is not an
autonomous trading bot, an investment adviser, or a promise of profitable trading. Options and
crypto can produce rapid, substantial losses. This document is the single source of truth for
limitations referenced by the README and the runbooks.

## Broker integration

- **The official `ibapi` package is not installable in this build/CI environment.** The
  `OfficialIBKRBroker` order path (`placeOrder`/`cancelOrder`, order-object construction) is
  therefore validated against fake-ibapi objects and a recording spy, not a live gateway.
  **Owner gateway verification against a running paper/live TWS or IB Gateway is an owner
  action** and is the one remaining live integration seam. The complete pipeline drives any
  `Broker` implementation, so this seam is narrow and well-typed.
- **Live trading has never been exercised from this codebase.** No test, CI run, or development
  path places an order. Any live acceptance is an owner action through the finished app.
- The real-network IBKR smoke test is opt-in (`CHRONOS_RUN_IBKR_SMOKE=1`), read-only, and
  skipped by default.
- Modify-in-place is not implemented on the official live adapter: re-price by cancel +
  re-propose (the full gate walk re-applies).

## Crypto family (built, disabled by default)

- **IBKR paper accounts do not support crypto.** There is no paper dry-run for this family. Its
  validation is (1) deterministic demo fixtures, (2) the recording-spy pipeline walk, and (3) an
  owner-performed minimal-size live acceptance. This limitation is disclosed, not papered over.
- Crypto is **deny-by-default**: an empty `CRYPTO_ALLOWLIST` disables the entire family. It never
  trades unless the owner explicitly allowlists symbols on a live account. There is no dedicated
  "crypto is live-only" code gate; the live-only effect is enforced by multiple fail-closed layers
  — IBKR paper has no crypto, so on a paper gateway a crypto order fails closed at qualification /
  market-data / venue-conformance and would be rejected by the venue regardless.
- Spot only (no crypto options ⇒ no crypto wheel), long-only, limit orders only, no margin, no
  shorting, no staking/transfer features.
- Venue min-size / size-increment / min-tick come **only** from the qualified IBKR
  ContractDetails. When absent, the dependent checks are UNKNOWN and fail closed — never assumed.
  There is no min-notional check (IBKR ContractDetails carries no such field); the venue's own
  minimum-order rejection plus the per-order MAX notional cap are the guards.
- Owner gateway items to verify before enabling crypto live: TWS API ≥ 10.10 (the Decimal
  `totalQuantity` precondition) and the exact `minSize`/`sizeIncrement` ContractDetails field
  names; the Paxos/Zero Hash routing exchange; the permitted time-in-force (`CRYPTO_TIME_IN_FORCE`);
  crypto market-data permissions; crypto `whatIf` behavior; and jurisdiction/account eligibility.

## Order pipeline and reconciliation

- Reconciliation runs on the portfolio page render and inside explicit symbol workflows.
  Scheduled/periodic reconciliation on a timer is not implemented; startup, reconnect, and
  order/fill-event reconciliation are.
- The local reader conservatively marks persisted cycles, strategy state, drafts, fills, and
  basis symbols unresolved, so only locally-empty flat symbols can publish `RECONCILED`;
  positions and owned working orders stay `MANUAL_REVIEW` until complete allocation provenance
  exists. `MANUAL_REVIEW` is the safe outcome for any ambiguity.
- A `SUBMISSION_UNKNOWN` order (an ambiguous failure after a send may have started) blocks
  further live submissions until reconciliation resolves it from broker truth, or an audited
  operator resolution (`POST /orders/{id}/resolve`) concludes it — never an auto-retry.
- The confirmation summary hash and idempotency key canonicalize the quantity but **not** the
  limit price, so two economically-identical spellings of a limit price (e.g. trailing zeros)
  would produce distinct hashes. This is a recorded, low-impact limitation: changing the
  limit-price serialization would alter every existing hash, so it is deliberately left as-is.
- Covered-call scenarios remain blocked on complete stock-allocation provenance; strategy basis,
  arbitrary quantities, real-broker margin, and IBKR order what-if beyond the demo path are not
  fully wired. Stock allocation valuation requires a current underlying quote at the service layer.
  Dividend, borrow, and corporate-action inputs are optional because the broker port does not
  provide them yet.

## Persistence and migrations

- Fresh databases are built with SQLAlchemy `create_all` (always the current models); the Alembic
  chain exists to upgrade legacy v2/v3 databases, with `0001` a no-op baseline. Migration
  completeness is guarded in CI (via pytest) by a frozen table manifest and a
  from-baseline-upgrade check, so a **table** added to the models without a migration fails CI.
  A new **column** on an existing table added without a migration is not automatically caught for
  the legacy-upgrade path (fresh DBs get it via `create_all`); add the migration explicitly.
- Chronos never upgrades a v1 database in place, adopts account-specific rows from an unscoped
  database, or fabricates provenance for legacy rows. Preserve and back up any existing file and
  configure a fresh `DATABASE_URL` until an explicit operator-reviewed import exists.

## Historical-data plane (C1, `chronos.histdata`)

- **The real IBKR fetch is owner-gated and unexercised here.** `reqHistoricalData`
  runs only against a live gateway with the official TWS API (`ibapi`, not a
  dependency — invariant 8). CI proves the store, adjustment, pacing, quality gate,
  and process isolation against a fake client only; the official client's behavior
  (volume units, exact bar-date formatting, accepted pacing) is confirmed by the
  owner on first real backfill.
- **No corporate-action data is fabricated.** The store ships with empty bars and
  empty action files; adjustment correctness is proven with synthetic split/dividend
  fixtures. Real splits/dividends are captured or entered by the owner, in **native
  as-of-ex-date basis** (never restated to a later split's terms, or the read-time
  factor double-counts).
- **`TOTAL_RETURN` is the CRSP/vendor adjusted-close approximation**, exact for splits
  and first-order for dividends — not an exact reinvested total-return index.
- **Pacing is coded to documented limits, not measured.** Cross-process coordination
  with the trading backend (a shared pacing budget) is not wired; the data process
  self-paces conservatively under its own client id.
- **The holdout embargo is a default-masked accessor, not a structural guardian.** A
  caller that reads `bars/<SYMBOL>.csv` directly bypasses it. The once-only,
  owner-typed, logged unlock and registry-brokered reads are Phase C2's job.
- **The legacy `research/data/raw/` corpus is unchanged.** C1 stands up a separate
  go-forward store (`research/data/history/`) and does not migrate or reconcile the
  heterogeneous 5-ETF CSVs.

## Strategy / research honesty

- The regime-context panel (EMA/RSI/vol-percentile) is a Pine-derived heuristic, explicitly
  labeled "not a validated signal," and has no pathway into order transmission.
- The autonomous strategy platform (`chronos.execution`/`chronos.risk`) starts halted, refuses
  every live-capable mode in code, and is never imported by the Live Wheel order pipeline. No
  generative/model output is used in any runtime trading decision.
- Backtests and shadow scans describe would-be intents only; paper fills do not prove live
  execution quality, and past behavior does not predict future results.
