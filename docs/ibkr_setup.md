# Interactive Brokers setup

IBKR connectivity is not enabled in Milestone 1. This document records the safe setup contract
for the read-only integration milestone.

## TWS or IB Gateway

Authenticate in TWS or IB Gateway; Chronos never handles the username or password. Enable socket
clients, bind to loopback unless remote access is intentionally secured, and initially select the
API read-only option. Common defaults are port `7497` for TWS paper, `7496` for TWS live, `4002`
for Gateway paper, and `4001` for Gateway live. Verify the configured port in your installed
version rather than assuming a default.

Use a unique `IB_CLIENT_ID` and set `IB_ACCOUNT_ID` when more than one account is visible. Market
data, option Greeks, volume, and open interest depend on the account's subscriptions and exchange
permissions. Missing permissions produce an explicit missing-data state and `NO_TRADE`.

## Smoke test contract

The separately marked smoke test is skipped by default. When implemented, it will only connect,
read server time and account summary, qualify one allowlisted underlying, retrieve option-chain
metadata, request one bounded quote, cancel it, and disconnect. It will never place or preview an
order.

```bash
.venv/bin/pytest -m ibkr tests/integration/test_ibkr_smoke.py
```

## Data quality

Chronos labels market data `LIVE`, `FROZEN`, `DELAYED`, `DEMO`, `STALE`, or `UNKNOWN`.
Delayed, stale, unknown, crossed, or incomplete data cannot be used to transmit an order. The
adapter narrows chains before subscribing, batches requests, limits concurrency, cancels unused
subscriptions, backs off on pacing errors, and never retries indefinitely.

## Troubleshooting starting points

- Connection refused: confirm TWS/Gateway is running, socket access is enabled, and host/port
  match the selected environment.
- Client ID in use: choose a distinct `IB_CLIENT_ID` or terminate the stale client cleanly.
- No security definition: qualify the underlying first and use returned chain metadata including
  exchange, multiplier, and trading class.
- Missing quote or Greeks: confirm subscriptions, request mode, contract qualification, and
  underlying market-data availability. Chronos will not synthesize the absent values.
- Pacing warning: wait for the bounded backoff and reduce requested contracts; do not loop.
