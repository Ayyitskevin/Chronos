# Operations — Routine Procedures

Day-to-day operation of the deterministic platform. Companions: docs/IBKR_RUNBOOK.md (broker
procedures), docs/BACKUP_AND_RECOVERY.md, docs/INCIDENT_RESPONSE.md.

All commands assume the repository root as the working directory (the CLI's default
`--halt-file`/`--audit-file` paths are relative).

## Morning checklist

```bash
cd ~/Chronos            # wherever the repo lives
python -m chronos.cli status
```

Read the output deliberately:

1. **Halt state.** `HALT STATE | armed (not halted)` or `TRADING HALTED | reason: ... | detail`.
   If halted: find out why before anything else. Do not rearm reflexively — the reason and detail
   name the trigger (`src/chronos/control/halt.py` lists all reasons).
2. **Audit chain.** `status` verifies the hash chain and prints
   `audit log: OK — chain intact (N records)` or a failure with the first bad line. A failure is
   an incident (docs/INCIDENT_RESPONSE.md), not something to shrug at.
3. **Mode banner.** For `status` the banner shows `MODE: RESEARCH | CAPABILITY: NO_ORDERS` and
   `LIVE TRADING | hard-disabled`. It reflects the command's own context — it is not a status
   readout of any running service.
4. **Data freshness** (when doing research work): check that the input files you plan to use are
   the ones you think they are — `research/data/raw/MANIFEST.json` records source and SHA-256 per
   file, and every backtest summary prints the `data_sha256` it actually loaded. At runtime the
   risk engine independently rejects stale data (`STALE_MARKET_DATA` when quote/bar age exceeds
   policy limits — and a zero limit denies by default, `src/chronos/risk/engine.py`).
5. If a gateway session is part of the day: gateway logged in, correct paper account shown, port
   listening (docs/IBKR_RUNBOOK.md sections 3 and 6).

## Shadow scan (after market close)

For the daily-bar strategies, the shadow workflow is a one-shot scan after the close
(`src/chronos/research/shadow.py`, `cmd_shadow_scan` in `src/chronos/cli/main.py`):

```bash
python -m chronos.cli shadow-scan            # defaults: both strategies, the six candidate ETFs
```

It runs the production decision path (strategy → sizing → risk engine) over the latest closed
bars and reports the proposal, the sized would-be intent, and the full risk decision per
(strategy, symbol). Nothing can be submitted: the SHADOW lock is `NO_ORDERS` and the module never
constructs a broker adapter. Every report is appended to the audit log as a `shadow_scan` record,
so the scan history is part of the tamper-evident trail. Symbols without a data file, or with
blocking data-quality issues, are reported as skipped rather than silently ignored.

## Platform monitor (read-only)

The monitor is a read-only view over persisted platform state — halt store, audit log, risk
policy, market-data files, and (optionally) the execution ledger. It **imports no broker adapter,
opens no market-data connection, and exposes no control that can arm, halt, or submit** (a unit
test asserts the no-broker-import guarantee). It is exactly as trustworthy as the files on disk.

Terminal render (`cmd_monitor` in `src/chronos/cli/main.py`):

```bash
python -m chronos.cli monitor --mode shadow \
  --policy config/risk.example.yaml --data-dir research/data/raw --symbols SPY,QQQ \
  --ledger data/platform_ledger.db      # --ledger is optional
```

Localhost Streamlit page (`src/chronos/monitoring/streamlit_app.py`), configured by environment
variables so the page stays a pure function of files on disk:

```bash
CHRONOS_MONITOR_MODE=shadow CHRONOS_LEDGER_FILE=data/platform_ledger.db \
  streamlit run src/chronos/monitoring/streamlit_app.py
```

It surfaces: operating mode and live-lock capability (the paper/live distinction is shown by an
explicit text banner and a boolean `live_capable`, **never by colour alone**), halt reason,
reconciliation outcome (from the last `service_startup` audit record), audit-chain integrity,
market-data freshness, the active risk limits, code commit, and — when a ledger is supplied —
open orders, fill-derived net positions, and recent fills. Realized/unrealized P&L is **not**
reconstructed here: this build runs SHADOW with a flat account and submits nothing, so those rows
are empty by construction and the monitor says so rather than printing a fabricated zero.

## Running a backtest reproducibly

```bash
python -m chronos.cli backtest --strategy regime_trend_v1 --symbol SPY \
  --data-dir research/data/raw --policy config/risk.example.yaml \
  --cash 3000 --slippage-bps 2 > runs/regime_trend_v1_SPY_$(date +%F).json
```

The JSON summary is the reproducibility record (`src/chronos/research/runner.py`). It contains:
`strategy`, `strategy_version`, `symbol`, `bars`, `date_range`, `data_sha256`,
`data_quality_issues`/`data_quality_blocking`, `policy_version`, `policy_hash`, `code_commit`,
`config` (cash, slippage), `risk_rejections`, `skipped_conversions`, and `metrics`. Two runs with
the same code commit, data hash, and policy hash produce identical results — if they do not, stop
and treat it as a bug.

Notes:

- The backtest uses its own throwaway halt file, `data/backtest_halt_<strategy>_<symbol>.json`,
  which it arms itself. It never touches `data/platform_halt.json`.
- The example policy denies everything by design; a backtest under it reports rejections rather
  than trades. Copy `config/risk.example.yaml` to `config/risk.yaml` and grant limits deliberately
  for research runs. `.gitignore` excludes `config/risk.yaml`, so local limits are not committed
  by default (a policy holds no secrets, only limits, but the file is still local-only by design).

## Reading the platform ledger

`data/platform_ledger.db` is plain SQLite (`src/chronos/execution/sqlite_ledger.py`). Read it with
the standard shell; treat it as read-only evidence — never UPDATE/DELETE (the schema itself is
append-oriented; nothing in code updates or deletes rows).

Tables:

| Table | Contents |
|---|---|
| `schema_info` | single row, schema `version` (currently 1) |
| `intents` | one row per order intent: `intent_id` (PK), strategy id/version, symbol, side, quantity, limit/stop price (TEXT decimals), tif, decision timestamp, source bar, reason, `initial_status`, `created_at_utc` |
| `transitions` | insert-only status history: `intent_id`, `status`, `at_utc`, `evidence` |
| `fills` | insert-only fills: `intent_id`, `cumulative_quantity`, `average_price`, `commission_usd`, `at_utc` |

Useful queries:

```bash
sqlite3 -readonly data/platform_ledger.db "
  SELECT t.intent_id, t.status, t.at_utc
  FROM transitions t
  JOIN (SELECT intent_id, MAX(id) m FROM transitions GROUP BY intent_id) x ON x.m = t.id
  ORDER BY t.at_utc DESC LIMIT 20;"                      # latest status per intent

sqlite3 -readonly data/platform_ledger.db "
  SELECT intent_id, cumulative_quantity, average_price, commission_usd, at_utc
  FROM fills ORDER BY id DESC LIMIT 20;"                 # recent fills

sqlite3 -readonly data/platform_ledger.db "
  SELECT * FROM transitions WHERE intent_id = '<uuid>' ORDER BY id;"   # one order's history
```

An intent whose latest transition is `SUBMITTED`/`PRE_SUBMITTED`/`ACKNOWLEDGED`/
`PARTIALLY_FILLED`/`PENDING_CANCEL` is "working" — reconciliation compares exactly that set
against broker open orders (`SqliteLedger.working_intent_ids`).

## Log locations

| What | Where |
|---|---|
| Platform audit trail (hash-chained) | `data/platform_audit.jsonl` |
| Platform order ledger | `data/platform_ledger.db` |
| Platform halt state | `data/platform_halt.json` |
| Backtest throwaway halt files | `data/backtest_halt_*.json` (safe to delete when idle) |
| Wheel dashboard rotating log | `logs/chronos.log` (`LOG_FILE` setting) |
| Wheel dashboard ledger | `data/chronos.db` (`DATABASE_URL` setting) |
| Autonomous option receipts | account-scoped `autonomy.option-selections` hash-chain stream in the Wheel database |
| Autonomous owner-alert file (when configured) | `AUTONOMY_ALERT_FILE` (default `data/owner_alerts.jsonl`) |
| Platform notifications | logger `chronos.notifications` (console/log only; no external channel is implemented) |

## Autonomous option receipt inspection (ADR-0020)

This section belongs to the live-wheel/autonomy backend rather than the
deterministic strategy platform described above. Inspect option-selection
history through the authenticated terminal or bounded
`GET /terminal/option-selections`; v1 intentionally ships no option-replay CLI.
The view reports the full account-scoped hash-chain result separately from
whole-stream semantic validity (every envelope replays and each decision ID is
unique), plus verification for each returned canonical receipt. A truncated
page never turns an earlier chain break or duplicate decision into a
valid-looking tail. Invalid receipt text above the inspection byte bound is not
echoed back; neither are oversized, malformed, deeply nested, noncanonical, or
invalid-storage sequence/time/kind/payload/hash fields. The entry retains typed
invalidity detail instead. Full-history semantic inspection streams one
SQL-bounded row at a time while retaining at most the newest 25 receipts and
decision IDs. Exact duplicate detection retains one bounded decision ID per
historical receipt, so its memory grows with stream length even though receipt
bodies and driver batches do not.

Treat any invalid chain/receipt, duplicate decision receipt, status/digest/time
mismatch, or `option_selection.system_failure` alert as a stop condition. Do not
edit the SQLite rows or regenerate a digest. Preserve the database, engage the
kill switch if live authority exists, and investigate the first invalid record
and its source evidence. Ordinary candidate-economics refusals remain visible as
typed `NO_TRADE` receipts but do not raise a system alert. Missing, conflicting,
unknown, identity-invalid, stale/future, and source-quality evidence does raise
that deduplicated alert; numeric misses for DTE, moneyness, delta range, spread,
volume, or open-interest floors do not.

`ENABLE_AUTONOMY_OPTION_SELECTION` defaults false. It enables evaluation only;
it does not create live authority. A live resolver-promotion artifact is a
separate owner action for exactly one CANARY/LIVE autonomy mode, and Chronos has
no command that creates it. No artifact is shipped by ADR-0020. Real IBKR will
continue to record `NO_TRADE` until an authoritative deliverable source exists.

## Halt / rearm discipline

- Anyone (any component) halts; only you rearm. `python -m chronos.cli halt --reason "..."` is
  always safe to run and is the first move in any incident.
- Rearm requires a non-empty note: `python -m chronos.cli rearm --note "..."`. The note is your
  audit trail — write what happened, what you verified, and why it is safe now. An empty or
  whitespace note is rejected (`HaltStore.rearm`).
- Rearming clears the halt only. Order generation additionally requires mode capability and a
  passed reconciliation (the CLI prints this reminder after rearm).
- A missing/corrupt halt file is HALTED, not armed. Restoring from backup therefore never
  silently resumes trading (docs/BACKUP_AND_RECOVERY.md).

## Promotion-record workflow

Promotion between modes is evidence, not a switch (`src/chronos/control/promotion.py`). There is
no CLI subcommand for it in this build; the operator drives it from Python:

```python
from pathlib import Path
from chronos.control.modes import TradingMode
from chronos.control.promotion import GateCheck, write_promotion_record

record = write_promotion_record(
    Path("data/promotions/2026-07-17-backtest-to-replay.json"),
    current_mode=TradingMode.BACKTEST,
    proposed_mode=TradingMode.REPLAY,
    code_commit="<git rev-parse HEAD>",
    strategy_versions={"regime_trend_v1": "1"},
    risk_policy_version="...", risk_policy_hash="...", config_hash="...",
    checks=[GateCheck(name="backtests_reproducible", passed=True, detail="...")],
    known_limitations=[...], outstanding_incidents=[...],
    owner_approval="<your name, date>", rollback_plan="<how you go back>",
)
print(record.all_gates_passed)
```

Rules enforced by `evaluate_promotion`:

- **Single-step only.** RESEARCH → BACKTEST → REPLAY → SHADOW → PAPER, one step at a time; a
  skip appends a failing `single_step_promotion` check.
- **Live is refused.** A proposed `CANARY_LIVE` or `LIVE` appends a failing
  `live_capability_hard_disabled` check unconditionally: promotion into those modes requires a
  future reviewed release plus explicit owner approval, not a record.
- Writing a record never changes the running mode. After a fully-passed record, you reconfigure
  the requested mode yourself, and the mode lock re-derives capability from live evidence at the
  next resolution (ADR-0007).

Keep promotion records with your backups; they are the paper trail of why the system was allowed
to do more.
