# IBKR Operator Runbook

Practical procedures for running Chronos against Interactive Brokers. Audience: the repo owner,
operating a paper account from a local Linux machine. Companion documents: docs/IBKR_INTEGRATION.md
(design), docs/ibkr_setup.md (wheel-dashboard read-only smoke test), docs/OPERATIONS.md (daily
routine), docs/INCIDENT_RESPONSE.md (when something is wrong).

Reality check before you start: the platform's paper adapter has never been run against a real
gateway from the build environment, and no long-running shadow/paper service loop exists yet (see
docs/DEPLOYMENT.md). Today the concrete things you can run against a gateway are the read-only
smoke test and the wheel dashboard. This runbook still documents the full procedures because the
halt/reconciliation discipline applies to all of them.

## 1. Install TWS or IB Gateway

- Download from interactivebrokers.com (TWS for a UI, IB Gateway for headless-ish operation — it
  still requires interactive login).
- Log in with the PAPER account credentials. Paper accounts have ids matching `D[UF]\d{4,}`
  (e.g. `DU1234567`). If the login banner does not say paper/simulated, stop.

## 2. Enable the API

In TWS/Gateway: Configure → API → Settings:

- Enable ActiveX and Socket Clients.
- Socket port: 7497 (TWS paper) or 4002 (Gateway paper). The platform adapter refuses any other
  port (`PAPER_PORTS` in `src/chronos/execution/brokers/ibkr_paper.py`).
- Keep "Allow connections from localhost only" (or trusted IPs = 127.0.0.1). Never expose the
  socket beyond the machine.
- For read-only smoke testing, additionally check the API "Read-Only API" option
  (docs/ibkr_setup.md).

## 3. Port checks

```bash
ss -tlnp | grep -E '7497|4002'          # is the API socket listening?
python -c "import socket; socket.create_connection(('127.0.0.1', 7497), 3); print('open')"
```

Live ports are 7496 (TWS) and 4001 (Gateway). If those are what is listening, you are logged into
a live session — do not point Chronos at it. The adapter would refuse the port, the account
pattern, and the environment check independently, but do not rely on that: fix the session.

## 4. .env setup

Copy `.env.example` to `.env` (gitignored) and set, at minimum, for gateway work:

```dotenv
BROKER_MODE=ibkr
IB_ENVIRONMENT=paper
IB_HOST=127.0.0.1
IB_PORT=7497            # or 4002 for IB Gateway
IB_CLIENT_ID=17         # any unused id; must be unique per connected client
IB_ACCOUNT_ID=DU1234567 # your real paper account id
ALLOW_ORDER_TRANSMIT=false
ALLOW_LIVE_TRADING=false
```

`ALLOW_LIVE_TRADING=true` does not enable anything: settings validation raises and the process
refuses to start (`src/chronos/config/settings.py`). Never put IBKR usernames or passwords in any
Chronos file.

## 5. Daily maintenance window and restarts

Expect the session to break daily:

- TWS/Gateway restarts itself daily at the configured restart time; set Auto-Restart in the
  configuration so re-authentication happens on the schedule you choose. 2FA prompts still
  require you.
- IBKR server resets occur nightly (roughly 23:45–00:45 US/Eastern for the main reset window;
  paper systems have their own resets). Connections drop and order status may be briefly
  unavailable.
- Weekly full restarts of the gateway application are recommended.

Plan around it: this platform trades daily bars; nothing needs to run overnight.

## 6. Reconnect procedure (after any disconnect, restart, or crash)

The order is fixed. Do not skip steps.

1. Restore the gateway session (log in, 2FA).
2. Check platform state:
   ```bash
   python -m chronos.cli status
   ```
   Run from the repository root: the default paths `data/platform_halt.json` and
   `data/platform_audit.jsonl` are relative to the current directory (override with
   `--halt-file` / `--audit-file`).
3. The halt persists across restarts by design (`src/chronos/control/halt.py`). If the process
   halted before/at the disconnect, it is still halted now. A missing or corrupt halt file also
   reads as HALTED.
4. Reconciliation must pass before anything can submit: the execution engine refuses submission
   with `RECONCILIATION_PENDING` until reconciliation has passed since startup/reconnect
   (`src/chronos/execution/engine.py`). Compare broker open orders (by `orderRef`) and positions
   against the ledger (`data/platform_ledger.db`; see docs/OPERATIONS.md for queries).
5. Only after you understand why the halt happened and reconciliation is clean:
   ```bash
   python -m chronos.cli rearm --note "reconnect after gateway restart; recon clean; <your evidence>"
   ```
   The note is mandatory, recorded, and audited. An empty note is rejected.

## 7. Specific situations

### Gateway restart (planned or crash)

Follow section 6. Nothing else is needed: no orders can be created while the process is
unreconciled, and DAY orders at the broker expire at the end of the trading day on their own.

### Order stuck in UNKNOWN / RECONCILIATION_REQUIRED

Meaning: the broker sent contradictory or unrecognized evidence for that order; the platform moved
it to `RECONCILIATION_REQUIRED`, cleared the reconciliation flag, and (for illegal transitions)
halted (`STATE_CORRUPTION` / `UNKNOWN_ORDER` in `src/chronos/control/halt.py`).

1. Do not rearm yet.
2. Find the order at the broker: TWS Order Ticket / Trade Log, matching by `orderRef` (the intent
   id, a UUID).
3. Determine its true terminal state (filled / cancelled / rejected / still working). Cancel it at
   the broker if it should not be working.
4. Record what you found (docs/INCIDENT_RESPONSE.md evidence capture).
5. Rearm with a note describing the resolution.

### Position mismatch (UNEXPLAINED_POSITION)

The broker reports shares the ledger cannot explain. The platform blocks trading and never
auto-flattens (`src/chronos/execution/reconciliation.py`).

1. Resolve at the broker: identify where the position came from (manual trade in TWS? another
   client id? a fill the platform missed?). Close or keep it by your own manual decision in TWS —
   the platform will not do it.
2. Document: what the position was, where it came from, what you did.
3. Verify the ledger and broker now agree.
4. `python -m chronos.cli rearm --note "position mismatch resolved: <details>"`.

### Pacing violations

IBKR enforces pacing (e.g. market-data request rates, historical-data request windows, order-rate
limits). Symptoms: error messages mentioning pacing, throttled or silent responses.

- Stop the offending loop; wait several minutes for the pacing window to clear.
- Do not retry in a tight loop; do not raise request rates to "push through".
- The platform's daily-bar cadence should never approach these limits; hitting one suggests a bug
  or a runaway script — treat as an incident.

### Session expiry / authentication lost

The gateway shows logged-out or the API socket refuses connections mid-day.

- Re-authenticate in TWS/Gateway (2FA in hand).
- Then run the full reconnect procedure (section 6). Session expiry is a disconnect; the same
  reconciliation-then-rearm discipline applies.

## 8. Safe command list

All operator commands (`src/chronos/cli/main.py`). Every command prints the mode banner first;
none can enable live trading; there is no `--force` flag anywhere. The banner's MODE line reflects
the command's own context (RESEARCH for status/halt/rearm, SHADOW for shadow-scan, BACKTEST for
backtest), not a running service; no CLI lock can ever hold paper-submission capability.

```bash
# global options (both default to paths relative to the current directory):
#   --halt-file data/platform_halt.json
#   --audit-file data/platform_audit.jsonl

python -m chronos.cli status                       # mode banner, halt state, audit-chain verify
python -m chronos.cli halt --reason "why"          # raise a persistent operator halt
python -m chronos.cli rearm --note "why it is safe" # clear a halt (note mandatory, audited)
python -m chronos.cli risk-show [--policy config/risk.example.yaml]
                                                   # print the validated risk policy + hash
python -m chronos.cli verify-corpus [--registry research/strategy_registry.yaml]
                                                   # verify Pine corpus SHA-256 vs registry
python -m chronos.cli verify-audit-log             # verify the audit hash chain (exit 1 on fail)
python -m chronos.cli shadow-scan \
    [--strategies regime_trend_v1,mean_reversion_v1] [--symbols SPY,QQQ,IWM,DIA,GLD,TLT] \
    [--data-dir research/data/raw] [--policy config/risk.example.yaml] [--equity 3000.0]
                                                   # one-shot shadow evaluation of the latest
                                                   # closed bars: reports would-be intents and
                                                   # risk decisions; NO_ORDERS capability, no
                                                   # broker adapter; appends to the audit log
python -m chronos.cli backtest --strategy regime_trend_v1 --symbol SPY \
    [--data-dir research/data/raw] [--policy config/risk.example.yaml] \
    [--cash 3000.0] [--slippage-bps 2.0]           # deterministic backtest, JSON summary
```

Known strategy names for `backtest`: `regime_trend_v1`, `mean_reversion_v1`, `baseline_buy_hold`,
`baseline_sma_trend`, `baseline_random_entries` (`src/chronos/research/runner.py`). The data file
must exist at `<data-dir>/<SYMBOL>.csv`.

For the wheel dashboard's read-only IBKR smoke test:

```bash
.venv/bin/python scripts/smoke_test_ibkr.py        # forces all transmission flags off
```

For ADR-0020, this smoke additionally exercises the adapter's explicit
option-chain completion envelope, but it still requests no option quote fanout
and proves no deliverable authority or promotion. Both real adapters report
autonomous deliverables as non-authoritative, so real IBKR option selection is
expected to remain `NO_TRADE`. `ENABLE_AUTONOMY_OPTION_SELECTION` defaults false,
and this release creates no live resolver-promotion artifact.
