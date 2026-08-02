# IBKR Integration — Execution-Plane Paper Adapter

This document describes how the deterministic platform talks to Interactive Brokers. It covers the
paper execution adapter (`src/chronos/execution/brokers/ibkr_paper.py`), the gates around it, and
how it differs from the wheel dashboard's separate read-only adapter.

**Status: implemented and unit-tested against a fake IB object; NOT yet exercised against a real
TWS/IB Gateway.** No credentials exist in this build environment. The first real-gateway contact is
an owner action: run the read-only smoke path (docs/ibkr_setup.md) with owner credentials and a
running TWS/Gateway, then exercise the paper adapter under supervision.

## Two adapters, two jobs

| Adapter | File | Capability |
|---|---|---|
| Wheel dashboard adapter | `src/chronos/broker/ibkr.py` | Read-only. Every order method (`preview_order`, `submit_order`, `modify_order`, `cancel_order`) raises `BrokerSafetyError` unconditionally. |
| Platform paper adapter | `src/chronos/execution/brokers/ibkr_paper.py` | ~~The ONLY code path in the repository that can hand an equity order to IBKR~~ **Corrected 2026-08-02: false since Milestone 5-7.** This adapter is **quarantined** (R-28): it refuses construction unless passed `quarantine_ack=True`, which no module in `src/` passes, and an AST test asserts no production module constructs it. The one reachable path that can hand an order to IBKR is the `chronos.orders` submission boundary — the repository's single `transmit=True` site (`src/chronos/orders/submission.py:745`), pinned by `tests/safety/test_single_transmit_site.py` and the repo-wide inventory in `tests/safety/test_broker_mutation_inventory.py`. |

They share the `ib_async` dependency (ADR-0002) but no state.

## Construction gates (mode lock)

`IBKRPaperExecutionAdapter.__post_init__` refuses to construct unless:

1. The `ModeLock` grants `PAPER_SUBMISSION` (`mode_lock.may_submit_paper`). Per
   `src/chronos/control/modes.py`, that capability only exists when, simultaneously:
   - order transmission is enabled,
   - the operator-maintained paper account allowlist is non-empty,
   - the broker reported an account id, and it is on the allowlist,
   - the account id matches the IBKR paper pattern `D[UF]\d{4,}` (e.g. `DU1234567`) — a
     live-looking `U…` id fails even if allowlisted,
   - the broker-reported environment is verified as paper.
2. The configured port is on the approved paper-port list: `PAPER_PORTS = {7497, 4002}`
   (TWS paper, IB Gateway paper). Any other port — including the live ports 7496/4001 — raises
   `BrokerSafetyError`.

There is no way to construct this adapter in RESEARCH, BACKTEST, REPLAY, SHADOW, CANARY_LIVE, or
LIVE mode: those locks never carry `PAPER_SUBMISSION`.

## Per-submission verification

`verify_account()` runs at the start of EVERY `submit()` call (every submission window), not just
at connect time. It requires:

- the gateway is connected, and
- `ib.managedAccounts()` returns exactly `[<the lock's paper account id>]` — an exact one-element
  match. Extra accounts, a different account, or an empty list refuse the trade.

## Order shape

Every order the adapter places is:

- a `LimitOrder` on a `Stock(symbol, "SMART", "USD")` contract — there is no market-order path
  (the `OrderIntent` type has no market variant at all, `src/chronos/execution/intents.py`);
- `tif = "DAY"` (the only `TimeInForce` value that exists);
- `outsideRth = False`;
- `order.account` set to the verified paper account;
- `orderRef = intent_id` — the deterministic UUIDv5 of the intent's economic content. This is the
  idempotency key: reconciliation matches broker open orders to ledger intents by `orderRef`, and
  the adapter refuses to submit the same intent id twice (`BrokerSafetyError`);
- `transmit = True` (within the paper account only; everything above gates whether this line is
  reachable at all).

## Event translation

`drain_events()` briefly pumps the ib_async loop (`waitOnUpdate(0.05)`, exceptions suppressed — a
pump failure surfaces as data staleness, never a crash) and translates each trade's
`orderStatus.status` through `_STATUS_MAP`:

| IB status | Platform `BrokerEventKind` |
|---|---|
| `PendingSubmit` | no event while unfilled |
| `PreSubmitted` | `ACKNOWLEDGED` |
| `Submitted` | `ACKNOWLEDGED` |
| `Filled` | `FILLED` |
| `Cancelled` | `CANCELLED` |
| `ApiCancelled` | `CANCELLED` |
| `Inactive` | `REJECTED` |
| anything else | `ERROR` |

Additional rules:

- An `ACKNOWLEDGED` translation with `0 < filled < totalQuantity` becomes `PARTIAL_FILL`.
- Duplicate (status, filled) pairs are suppressed; only changes emit events.
- Average fill price comes from `orderStatus.avgFillPrice` (only when filled > 0); commission is
  summed from each fill's `commissionReport.commission` and is `None` until a commission report
  arrives.
- An `ERROR` event drives the order state machine to `UNKNOWN` then `RECONCILIATION_REQUIRED`
  (`src/chronos/execution/engine.py`), and clears `reconciliation_passed`, blocking all further
  submission until reconciliation passes again.

The execution engine treats broker events as the only truth: a submission call returning proves
nothing beyond "the request left the process" (`src/chronos/execution/brokers/port.py`).

## Reconciliation gate

`src/chronos/execution/reconciliation.py` compares, before any submission window after
startup/reconnect:

- broker open orders (matched by `orderRef` = intent id) vs the ledger's working intents, and
- broker positions vs positions the ledger can explain.

Any of these leaves `passed=False`, keeps `ExecutionEngine.reconciliation_passed` False, and must
raise a halt for operator review:

| Discrepancy | Meaning |
|---|---|
| `UNKNOWN_BROKER_ORDER` | broker reports an open order this ledger never submitted |
| `MISSING_BROKER_ORDER` | ledger believes an intent is working but the broker does not report it |
| `UNEXPLAINED_POSITION` | broker reports shares the ledger cannot map to a Chronos fill |

**There is no auto-flatten.** An unknown position blocks trading; it never triggers an emergency
order. Resolution is manual (docs/INCIDENT_RESPONSE.md).

Note: `reconcile()` is a pure comparison function. The evidence-gathering caller that queries a
real gateway for open orders/positions and feeds it is part of the not-yet-implemented shadow/paper
service loop (see docs/DEPLOYMENT.md, "Future work — shadow/paper service (NOT IMPLEMENTED)").

## Cancels

`request_cancel(intent_id)` cancels via the stored trade object; cancelling an unknown intent id is
a no-op. There is no modify/cancel-replace path in this build (RISK_REGISTER.md R-03 residual).

## What this integration does NOT do

- No credentials: nothing in this module (or repository) reads, stores, or automates IBKR
  authentication. TWS/Gateway login, including 2FA, is the operator's (docs/SECURITY.md).
- No live path: live-capable modes resolve to `DENIED_LIVE_DISABLED` (ADR-0007), so a
  `PAPER_SUBMISSION` lock — the adapter's only accepted input — cannot exist for a live account.
- No market orders, no shorts, no options, no outside-RTH orders.
- Not proven against a real gateway. Unit tests use a fake `IBLike` object. The smoke path
  requires owner credentials and a running TWS/Gateway (docs/IBKR_RUNBOOK.md).
