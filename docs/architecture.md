# Chronos architecture

## Decisions

Chronos uses a ports-and-adapters boundary around brokerage access. Strategy and UI code depend
on the typed `Broker` protocol, never directly on `ib_async`. `DemoBroker` is a deterministic
adapter; `IBKRBroker` is the strictly read-only networked adapter in Milestone 2. This keeps a
future official IB API adapter from requiring strategy or UI rewrites.

Money, premium, fees, strikes, basis, and allocation calculations use `Decimal`. UTC-aware
timestamps are stored internally; the UI converts them to `America/New_York` for display.
Safety comparisons run in explicit local Decimal contexts instead of inheriting mutable process
precision. Option DTE uses the configured exchange-calendar timezone, which defaults to
`America/New_York`.

SQLite stores the Chronos ledger and decision evidence. It does not override broker positions,
orders, executions, or account values. SQLAlchemy provides explicit schema initialization and
repository seams. Schema version 2 enables SQLite foreign keys and binds each database file to one
broker mode, environment, and pseudonymous account fingerprint. Chronos never mutates an older
schema during application startup; an existing v1 database must be preserved and replaced with a
fresh v2 `DATABASE_URL` until an explicit, operator-reviewed import exists. Likewise, the first
account binding refuses any pre-existing account-scoped rows. A different or ambiguous account
must use a different database file; demo metadata cannot silently become paper-account metadata.
SQLite ledger files and rotating log files are created with owner-only permissions.

Streamlit's rerun model is isolated from broker connection ownership. A cached runtime owns one
dedicated asyncio loop in one background thread plus one bounded market-data manager. Page reruns
reuse both resources instead of creating broker sockets or bypassing subscription cleanup.
Candidate market work is additionally gated by an explicit operator button; unrelated reruns do
not refresh the option universe.

## Four operational invariants

### Where state lives

- Brokerage truth: broker positions, open orders, fills, executions, and account values.
- Chronos truth: wheel-cycle links, candidate evidence, guardrail evidence, notes, and the
  explicitly labeled strategy-adjusted basis.
- UI state: navigation and one historical, presentation-safe candidate result only. It never
  determines the Wheel stage or authorizes an action.

### Where feedback lives

Connection changes, exceptional market-data lifecycle events, reconciliation capture/read
failures, and successfully completed resolver outcome counts are logged to the console and a
rotating local file. Locked early returns remain observable in their UI result without logging raw
broker details.
Candidate, guardrail, and basis repositories retain evidence only for legitimate persisted Wheel
cycles. Read-only reconciliation and flat-symbol candidate evaluation return in-memory
presentation models and deliberately do not manufacture cycles or write audit rows yet.

### What breaks if a component is removed

- Removing a broker adapter removes that data source without changing strategy code.
- Removing SQLite removes Chronos metadata and audit history; it must force reconciliation and
  keep order actions locked, never infer missing ledger state.
- Removing the connection service would reintroduce rerun-created sockets and is prohibited.
- Removing quote fields makes candidates ineligible; no model substitutes fabricated values.

### When timing works

The broker service serializes adapter work on its event loop. A portfolio render submits one
coordinator coroutine containing the complete double-read observation window, so another Chronos
broker call cannot interleave. Startup, reconnect, order/fill-event, and periodic triggers remain
planned. For streaming top-of-book data, quote age starts only after a price-bearing update from
the current subscription is received; it is checked at every decision and must be checked again
immediately before a future paper submit.

The symbol workspace starts candidate work only when the operator presses the explicit read-only
evaluation button. It stores at most one sanitized result for historical display, clears it on a
symbol change or raised refresh error, and never supplies that object to strategy or order
services.

## Package boundaries

- `domain`: immutable vocabulary and broker-neutral models.
- `broker`: protocol, deterministic demo adapter, IBKR adapter, connection ownership, and market
  data lifecycle.
- `strategy`: Wheel state, resolver, scenarios, assignment pressure, and capital constraints.
- `services`: read-only reconciliation and guarded short-put candidate evaluation.
- `persistence`: SQLAlchemy schema and repositories for Chronos-only state.
- `ui`: Streamlit pages and Plotly views; no brokerage truth lives here.
- `config` and `utils`: validated settings, logging, UTC, and identifiers.

## Reconciliation boundary

Reconciliation is the only path that publishes a Wheel stage. The coordinator double-reads fresh
account values, broker positions, open orders, and executions inside one serialized, bounded
observation window, then compares that stable snapshot with one account-scoped local transaction.
The independent real-time bound covers that local read too. Broker or account-value instability
returns `PENDING` without publishing a snapshot. Incomplete local evidence returns `PENDING` with
only the sanitized stable broker view; unresolved exposure returns `MANUAL_REVIEW`. The dashboard
never receives raw account IDs or order references, and every order action remains locked even
after a successful read-only run.

The strategy engines remain pure and accept an explicit reconciled snapshot, policy, clock, and
allocation context. The service coordinator owns serialized broker reads and invokes the Wheel
state engine; Streamlit renders only its immutable presentation model. This separation makes the
calculations repeatable without a network connection while preventing UI session state from
becoming strategy truth.

The pure Wheel state engine matches broker positions and closing orders by exact option contract
ID. Active orders must also have an affirmative full-identity Chronos ownership match in the
expected account. The current coordinator treats every broker execution and persisted fill as
unresolved until cycle-scoped allocation is implemented; it does not infer ownership from an
execution. Every short option requires an explicitly verified standard share-only deliverable;
covered-call coverage additionally requires stock with the exact underlying contract ID,
currency, and pseudonymous account scope. Symbol text, trading class, or multiplier alone cannot
pool nonfungible stock or bless an adjusted contract. The current IBKR adapter preserves
underlying identity but intentionally leaves the share deliverable unverified; demo fixtures mark
their standard deliverables explicitly.

The current local repository intentionally marks every symbol with persisted cycle, strategy,
draft, fill, or basis evidence unresolved. This lets locally empty flat symbols reconcile while
preventing working orders or positions from appearing proven before cycle-scoped fill and stock
allocation semantics are complete. A richer reader may later clear exact owned orders only after
that full provenance is implemented and tested.

## Read-only candidate boundary

The short-put candidate service obtains fresh reconciliation itself; a historical UI result cannot
be supplied as evidence. The first safe capital slice proceeds only when the overall result is
`RECONCILED`, the target is uniquely `FLAT`, and the stable snapshot contains no positions, open
orders, or executions. Only that whole-account-empty proof permits zero current Wheel allocation
and `total_cash` to be labeled uncommitted cash. A flat target symbol alone never proves portfolio
allocation.

After preflight, one connection-manager coroutine revalidates account scope, capital values, and
zero exposure around a force-refreshed, configured-bounds put request. The post-reconciliation
clock sample prevents a snapshot captured during reconciliation from appearing future-dated. A
new IBKR connection may begin with `UNKNOWN` market quality, but it must finish the quote window
with rankable quality and every actual quote still passes resolver quality and freshness rules.

The service selects one exact chain identity, requires pairwise equality between each qualified
contract and quoted contract, removes unverified deliverables before option quote fanout, and uses
the resolver's public spot rule for both narrowing and final evaluation. Defaults request at most
6 expirations by 12 strikes. Settings and deterministic narrowing enforce ceilings of 8
expirations and 20 strikes per expiration, with an 80-contract product cap. Qualification and
quote ingress independently reject more than 80 contracts before task creation or a broker
request. The resolver remains the sole ranking authority. Missing or changed
service-prerequisite evidence, ambiguous routing, incomplete broker responses, pacing failures, or
cleanup failures withhold resolution and return a sanitized locked `NO_TRADE`. Unverified
contracts are removed before quoting; the resolver can publish the remaining valid contracts while
listing stale or otherwise invalid quotes as rejected evidence. It returns overall `NO_TRADE` when
none pass. Even an `ELIGIBLE` result is read-only and keeps opening actions locked.

Option-position average cost is never used for premium or basis math: IBKR and demo adapters can
report that field in different units. Only execution price, qualified multiplier, fill quantity,
commission evidence, exact contract identity, and pseudonymous account scope enter the strategy
ledger. A premium without either an estimated or actual commission remains `PENDING`; it is not
treated as a final zero-fee fill.
