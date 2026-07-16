# Interactive Brokers setup

Chronos uses `ib_async` behind its broker abstraction. The Milestone 2 smoke path is strictly
read-only: it can verify connectivity, portfolio summary access, contract discovery, and one
bounded underlying quote. It does not call any order preview, submission, modification,
order-cancellation, exercise, or global-cancel method.

IBKR configuration and error-code details change independently of Chronos. Verify them against
the [official TWS API documentation](https://ibkrcampus.com/campus/ibkr-api-page/twsapi-doc/).

## TWS or IB Gateway

Authenticate in TWS or IB Gateway; Chronos never handles the username or password. Enable socket
clients, bind to loopback unless remote access is intentionally secured, and select the IBKR API
read-only option before smoke testing. If TWS asks whether to accept an incoming API connection,
verify the client and keep the session read-only.

Common defaults are:

| Application | Paper | Live |
| --- | ---: | ---: |
| TWS | `7497` | `7496` |
| IB Gateway | `4002` | `4001` |

These are conventions, not guarantees. Verify the socket port shown by the installed TWS or
Gateway instance. A live-account connection is still read-only in this smoke path, but paper is
the recommended first test.

Use a unique `IB_CLIENT_ID` and set `IB_ACCOUNT_ID` when more than one account is visible. Market
data, option Greeks, volume, and open interest depend on the account's subscriptions and exchange
permissions. Missing permissions produce an explicit missing-data state rather than fabricated
values.

IBKR contract details used by this milestone prove the option and underlying contract IDs, but
the adapter does not treat multiplier metadata as proof of a complete standard share deliverable.
Every new short-put and covered-call candidate therefore remains `NO_TRADE` until a later evidence
source verifies a share-only deliverable equal to the premium multiplier and tied to the exact
underlying. Chronos does not guess that any contract delivers 100 shares after a corporate action.

Start from `.env.example` and keep these safety settings unchanged:

```dotenv
BROKER_MODE=ibkr
IB_ENVIRONMENT=paper
IB_HOST=127.0.0.1
IB_PORT=7497
IB_CLIENT_ID=17
IB_ACCOUNT_ID=DU1234567
ALLOW_ORDER_TRANSMIT=false
ALLOW_LIVE_TRADING=false
SYMBOL_ALLOWLIST=AAPL,MSFT,SPY
```

Replace the example account identifier locally; never commit `.env`. Do not put an IBKR username
or password in Chronos configuration. The smoke test qualifies only the first symbol in
`SYMBOL_ALLOWLIST`, so choose a symbol for which the connected account has market-data access.

## Run the smoke test

The integration test is marked `ibkr` and skipped unless `CHRONOS_RUN_IBKR_SMOKE` is exactly `1`.
Its adapter import and construction stay inside the opted-in test body, so default collection
cannot open a broker connection. Unit tests do import the adapter, but inject an in-memory fake
client and perform no network access.

The recommended wrapper sets the opt-in flag, selects the IBKR adapter, and forcibly sets both
order-transmission flags to false:

```bash
.venv/bin/python scripts/smoke_test_ibkr.py
```

The equivalent direct invocation is:

```bash
CHRONOS_RUN_IBKR_SMOKE=1 \
  BROKER_MODE=ibkr \
  ALLOW_ORDER_TRANSMIT=false \
  ALLOW_LIVE_TRADING=false \
  .venv/bin/pytest -m ibkr tests/integration/test_ibkr_smoke.py
```

The test performs, in order:

1. Connect and verify connection health.
2. Read timezone-aware server time.
3. Read the selected account summary.
4. Qualify the first allowlisted underlying.
5. Retrieve non-empty option-chain metadata for that underlying.
6. Request one underlying quote through the bounded market-data manager and verify that real
   price data was returned.
7. Have that same manager operation cancel market data for the one contract.
8. Disconnect and verify the adapter is disconnected.

The market-data manager owns quote cancellation, and a `finally` block always attempts disconnect
even when a read fails. No automated test calls `preview_order`, `submit_order`, `modify_order`, or
`cancel_order`. A passing smoke test proves only that the configured read path works; it does not
prove order safety, fill quality, or live execution quality.

## Data quality

Chronos labels market data `LIVE`, `FROZEN`, `DELAYED`, `DEMO`, `STALE`, or `UNKNOWN`. The
smoke test accepts live, frozen, or delayed read-only quotes when at least one real price
field is present. It fails on `UNKNOWN` or wholly empty quotes so missing subscriptions and
permissions are visible. Delayed, stale, unknown, crossed, or incomplete data cannot later be used
to transmit an order.

The smoke test does not request an option-chain quote fan-out. It reads chain metadata, requests
one underlying contract, and atomically cancels that contract's market data through the same
manager call. This bounded request avoids treating an integration check as a load or pacing test.

## Troubleshooting starting points

- Connection refused: confirm TWS/Gateway is running, socket access is enabled, and host/port
  match the selected environment.
- API connection rejected: confirm the trusted-IP settings and approve the expected local client;
  do not disable authentication or expose the socket publicly.
- Client ID in use: choose a distinct `IB_CLIENT_ID` or terminate the stale client cleanly.
- Wrong account or empty summary: set `IB_ACCOUNT_ID` to an account visible in the authenticated
  session, especially for advisor or multi-account configurations.
- No security definition: qualify the underlying first and use returned chain metadata including
  exchange, multiplier, and trading class; also verify the first allowlisted symbol is valid.
- `UNKNOWN` or empty quote: confirm the account's market-data subscription, exchange permissions,
  request mode, and whether TWS/Gateway is providing frozen or delayed data. Chronos will not
  synthesize absent values.
- Pacing warning: stop repeated runs, wait for IBKR's pacing window to clear, and inspect the local
  log. The smoke test intentionally makes only one market-data request.
- Cleanup or disconnect failure: close the stale API client from TWS/Gateway before retrying with a
  fresh client ID. Do not immediately loop on a failed connection.
