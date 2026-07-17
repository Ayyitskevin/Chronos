# ADR-0002 — Interactive Brokers access via the TWS API with ib_async

Status: Accepted (2026-07-17). Index entry: DECISIONS.md D-02.

## Context

The platform needs one broker integration for paper-account order submission (execution plane) and,
in the future, IBKR historical data for research. The account is a small IBKR account operated by a
single owner on a local Linux machine. Three realistic options exist:

### Option A — TWS API via `ib_async` (chosen)

- Already a dependency of this repository (`pyproject.toml`: `ib_async>=2.0,<3`); the wheel
  dashboard's read-only adapter (`src/chronos/broker/ibkr.py`) uses it today.
- `ib_async` is the actively maintained community successor of `ib_insync` (the original author of
  ib_insync died in 2024; the community fork continues maintenance).
- Event-driven order status: order acks, status changes, executions, and commission reports arrive
  as events on `Trade` objects, which maps directly onto this platform's event-oriented
  `ExecutionBrokerPort` (`src/chronos/execution/brokers/port.py`).
- Works with paper accounts on the standard paper sockets: TWS 7497, IB Gateway 4002.
- Supports `orderRef` on orders, which the platform uses as the intent-id idempotency key.
- Cost: requires an owner-run TWS or IB Gateway session. Authentication (including 2FA) is manual
  and owned by the operator; there is no headless credential automation, and this build deliberately
  does not add any (see docs/SECURITY.md).

### Option B — Client Portal Web API (rejected for this build)

- REST + websocket; runs against a local gateway that still requires interactive login.
- Session keepalive and periodic re-authentication are a continuous operational burden for an
  unattended process; a dropped session silently degrades to failure at request time.
- Streaming order-event semantics are weaker for this use case than the TWS API's per-order event
  stream; polling order state reintroduces exactly the "infer state from a call returning" pattern
  this platform forbids.

### Option C — Raw TWS API sockets (rejected)

- Implementing the TWS wire protocol (or driving the official `ibapi` threads directly) would mean
  reimplementing what `ib_async` already provides: connection management, request/response
  correlation, and the event loop. No safety benefit; substantial new surface for bugs.

## Decision

Stay on the TWS API through `ib_async` for both the existing read-only wheel adapter and the new
paper execution adapter (`src/chronos/execution/brokers/ibkr_paper.py`).

## Consequences

- The operator must run TWS or IB Gateway locally, log in manually (with 2FA where enabled), and
  keep the API socket enabled. Daily restarts and session expiries are an operational fact; the
  runbook (docs/IBKR_RUNBOOK.md) covers them.
- IBKR pacing limits apply (market-data request pacing, order-rate limits, historical-data pacing).
  The platform's daily-bar cadence makes them unlikely to bind, but they exist and are handled in
  the runbook rather than in code.
- The adapter code is unit-tested against a fake `IB` object only. It has NOT been exercised
  against a real TWS/Gateway from this build environment — no credentials exist here. First contact
  with a real gateway is an owner action (the read-only smoke path in docs/ibkr_setup.md, then the
  paper adapter under supervision).
- Paper ports are pinned in code: `PAPER_PORTS = {7497, 4002}` in
  `src/chronos/execution/brokers/ibkr_paper.py`; any other port fails adapter construction.
