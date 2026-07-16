# Chronos architecture

## Decisions

Chronos uses a ports-and-adapters boundary around brokerage access. Strategy and UI code depend
on the typed `Broker` protocol, never directly on `ib_async`. `DemoBroker` is a deterministic
adapter; `IBKRBroker` will be the networked adapter. This keeps a future official IB API adapter
from requiring strategy or UI rewrites.

Money, premium, fees, strikes, basis, and allocation calculations use `Decimal`. UTC-aware
timestamps are stored internally; the UI converts them to `America/New_York` for display.

SQLite stores the Chronos ledger and decision evidence. It does not override broker positions,
orders, executions, or account values. SQLAlchemy provides explicit schema initialization and
repository seams. Schema changes will use additive, versioned migrations before persisted user
data exists.

Streamlit's rerun model is isolated from broker connection ownership. A cached connection
service owns one dedicated asyncio loop in one background thread, exposes thread-safe calls,
and shuts down explicitly. A page rerun reuses that service instead of creating a broker socket.

## Four operational invariants

### Where state lives

- Brokerage truth: broker positions, open orders, fills, executions, and account values.
- Chronos truth: wheel-cycle links, candidate evidence, guardrail evidence, notes, and the
  explicitly labeled strategy-adjusted basis.
- UI state: navigation and draft presentation only. It never determines the Wheel stage.

### Where feedback lives

Connection changes, market-data lifecycle, candidate decisions, guardrails, reconciliation, and
order events are logged to the console and a rotating local file. Material decisions also become
append-only application-event rows so the dashboard can explain them.

### What breaks if a component is removed

- Removing a broker adapter removes that data source without changing strategy code.
- Removing SQLite removes Chronos metadata and audit history; it must force reconciliation and
  keep order actions locked, never infer missing ledger state.
- Removing the connection service would reintroduce rerun-created sockets and is prohibited.
- Removing quote fields makes candidates ineligible; no model substitutes fabricated values.

### When timing works

The broker service serializes adapter work on its event loop. Reconciliation runs at startup,
after reconnect, after every order/fill event, and periodically while connected. Quote age is
computed from broker timestamps at every decision and again immediately before paper submit.

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

Reconciliation is the only path that publishes a usable Wheel stage. It compares a fresh broker
snapshot with persisted Chronos metadata. Safe, deterministic differences are recorded; unsafe
differences return `MANUAL_REVIEW`. Order actions stay locked until a successful run completes.
