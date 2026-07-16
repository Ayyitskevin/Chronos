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

## Four operational invariants

### Where state lives

- Brokerage truth: broker positions, open orders, fills, executions, and account values.
- Chronos truth: wheel-cycle links, candidate evidence, guardrail evidence, notes, and the
  explicitly labeled strategy-adjusted basis.
- UI state: navigation and draft presentation only. It never determines the Wheel stage.

### Where feedback lives

Connection changes, exceptional market-data lifecycle events, and reconciliation capture/read
failures are logged to the console and a rotating local file. Candidate, guardrail, and basis
repositories retain their own decision evidence. The current read-only reconciliation coordinator
returns an in-memory presentation model and deliberately does not write reconciliation or
application-event rows yet.

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

## Package boundaries

- `domain`: immutable vocabulary and broker-neutral models.
- `broker`: protocol, deterministic demo adapter, IBKR adapter, connection ownership, and market
  data lifecycle.
- `strategy`: Wheel state, resolver, scenarios, assignment pressure, and capital constraints.
- `services`: portfolio assembly, reconciliation, subscriptions, and guarded order lifecycle.
- `persistence`: SQLAlchemy schema and repositories for Chronos-only state.
- `ui`: Streamlit pages and Plotly views; no brokerage truth lives here.
- `config` and `utils`: validated settings, logging, UTC, and identifiers.

## Reconciliation boundary

Reconciliation is the only path that publishes a Wheel stage. The coordinator double-reads fresh
broker positions, open orders, and executions inside one serialized, bounded observation window,
then compares that stable snapshot with one account-scoped local transaction. Broker instability
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

Option-position average cost is never used for premium or basis math: IBKR and demo adapters can
report that field in different units. Only execution price, qualified multiplier, fill quantity,
commission evidence, exact contract identity, and pseudonymous account scope enter the strategy
ledger. A premium without either an estimated or actual commission remains `PENDING`; it is not
treated as a final zero-fee fill.
