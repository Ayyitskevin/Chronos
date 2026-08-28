---
name: chronos-run-and-operate
description: >
  Use this skill to start, stop, inspect, migrate, back up, restore, or operate
  Chronos; run the backend, UI, terminal, platform service, monitor, or histdata;
  handle an incident; use the live kill switch or platform halt; disarm or rearm;
  revoke a mandate; acknowledge an alert; or reconcile order state. Do not use it
  for environment setup, configuration design, refusal diagnosis, or first-gateway
  qualification; route those to the dedicated Chronos skills named below.
---

# Chronos: run and operate

Use this as a decision procedure, not as a snapshot of one revision. Run every
command from the repository root with the project virtual environment at `.venv/`.
Many state paths are relative to the current working directory.

## Authority and source order

Resolve operational facts in this order:

1. Executable code, command `--help`, and tests.
2. `Makefile` targets and startup scripts.
3. The focused runbooks:
   - deployment: `docs/DEPLOYMENT.md`
   - incidents: `docs/INCIDENT_RESPONSE.md`
   - backup and restore: `docs/BACKUP_AND_RECOVERY.md`
   - live controls: `docs/live_trading_runbook.md`
   - broker operations: `docs/IBKR_RUNBOOK.md`

If prose and code disagree, the executable source wins. Report the contradiction
and repair the stale guidance in its own change; do not blend two behaviors.

This skill does not grant permission to connect a broker, transmit an order, run a
migration against shared or live state, restore an autonomy mandate, rearm the
platform, disengage the kill switch, or arm a live session. Those actions can add
authority or change durable state and require deliberate owner direction. During
an incident, authority-reducing actions are the priority: engage the live kill
switch, disarm the live session, halt the deterministic platform, and revoke an
autonomy mandate.

## Route elsewhere when appropriate

| Need | Skill |
|---|---|
| Create or repair the venv and dependencies | `chronos-build-and-env` |
| Interpret or change environment variables | `chronos-config-and-flags` |
| Diagnose why startup or an operation refused | `chronos-debugging-playbook` |
| Inspect current local state without mutating it | `chronos-diagnostics` |
| Qualify the first real IBKR connection | `chronos-real-gateway-campaign` |
| Author or interpret autonomy authority | `chronos-autonomy-and-mandates` |

## Preflight before starting anything

1. Confirm the revision and worktree you are operating:

   ```bash
   git status --short --branch
   git rev-parse HEAD
   ```

2. Inventory state read-only. Do not treat an absent file as proof of a safe
   default:

   ```bash
   .venv/bin/python .claude/skills/chronos-diagnostics/scripts/state_inventory.py
   .venv/bin/python -m chronos.cli status
   ```

3. Derive the commands and accepted flags from the current checkout:

   ```bash
   make -n backend ui demo migrate
   .venv/bin/python -m chronos.cli --help
   .venv/bin/python -m chronos.service --help
   .venv/bin/python -m chronos.histdata --help
   ```

4. Inspect the intended broker mode, environment, account scope, database URL,
   transmission flags, and authority files without printing tokens or credentials.
   A backend start can open the configured broker adapter and can auto-activate a
   valid, unrevoked `AUTONOMY_MANDATE_FILE`.
5. Keep IBKR/TWS/Gateway disconnected unless the requested operation explicitly
   needs it and the owner has approved that boundary. Before any first gateway
   session, follow `chronos-real-gateway-campaign` and `docs/IBKR_RUNBOOK.md`.
6. Before a migration, restore, or overwrite, take the backup required by
   `docs/BACKUP_AND_RECOVERY.md` and verify the exact target path.

## Process map

| Surface | Current command source | Operational boundary |
|---|---|---|
| Backend API | `make backend`, implemented by `scripts/run_backend.py` | Sole broker owner and order writer; serves the terminal; may demote to read-only if it cannot hold the writer lease. |
| Backend-driven UI | `make ui`, implemented by `scripts/run_ui.py` | Thin Streamlit client; start the backend first. |
| Operator terminal | `/terminal/app` on the backend address | Browser client, not a separate process. Its cookie is scoped to `/terminal`. |
| Forced-safe legacy demo | `.venv/bin/python scripts/run_demo.py` | Forces demo broker and disables transmission in the child environment. |
| Legacy in-process UI | `.venv/bin/streamlit run src/chronos/app.py` | Older in-process surface; the `chronos` console script points here, not to the platform CLI. |
| Platform CLI | `.venv/bin/python -m chronos.cli <command>` | Deterministic platform status, halt/rearm, monitor, audit, corpus, and research commands. |
| Shadow/paper service | `.venv/bin/python -m chronos.service` | Derive flags with `--help`; the command surface has no live mode. Keep it foreground unless daemonization is reviewed. |
| Platform monitor | `.venv/bin/python -m chronos.cli monitor` | File-backed and read-only; it does not own a broker. |
| Historical-data capture | `.venv/bin/python -m chronos.histdata ...` | Read-only data plane, but it does connect to a configured gateway and needs its own client id. |
| Wheel database migration | `make migrate` | Runs Alembic against the configured `DATABASE_URL`; it is a durable-state mutation. |

`docs/DEPLOYMENT.md` contains a reviewed shape for a possible systemd user unit,
but the repository does not install or enable one. Do not daemonize a process as a
side effect of an ordinary run request.

### Choosing a start path

- For an offline demonstration, use `scripts/run_demo.py`; do not infer safety
  from the target name `make demo`, because that target aliases the backend-driven
  UI and does not rewrite the environment.
- For the current backend/UI path, run `make backend` in one foreground terminal,
  wait for a healthy loopback startup, then run `make ui` or open `/terminal/app`.
- For the deterministic platform loop, inspect `python -m chronos.service --help`,
  pass the intended mode and capital inputs explicitly, and keep the process in the
  foreground. Its service loop is separate from the backend live order plane.
- For research or monitoring, prefer `chronos.cli` commands that state their
  read-only boundary. The console command named `chronos` is not that CLI.

## The two independent stop mechanisms

Chronos has two stop states. Engage both during an incident because neither is a
repository-wide substitute for the other.

| Control | Stops | Engage | Release | Missing file |
|---|---|---|---|---|
| Live order-plane kill switch | `chronos.orders` submissions through the backend | Terminal **ENGAGE KILL SWITCH** or `POST /live/kill` | `POST /live/kill/disengage` with a note and writer lease | **DISENGAGED**; other gates still apply, but this stop contributes nothing |
| Deterministic-platform halt | `chronos.execution` and `chronos.service` order generation | `.venv/bin/python -m chronos.cli halt --reason "..."` | `.venv/bin/python -m chronos.cli rearm --note "..."` | **HALTED** as never armed |

The opposite missing-file defaults are load-bearing. Derive them from
`src/chronos/orders/kill_switch.py` and `src/chronos/control/halt.py`; never delete,
rename, or repoint either state file as cleanup.

### Incident order

Follow `docs/INCIDENT_RESPONSE.md`. Its evidence-capture and broker playbooks are
the canonical detailed procedure. The minimum safe ordering is:

1. Engage the live kill switch from `/terminal/app` or `POST /live/kill` with a
   reason. The stop remains available after writer-lease demotion.
2. Verify `GET /live/status` reports the switch engaged and confirm the configured
   kill-switch file exists. Do not trust a successful-looking click alone.
3. Halt the deterministic platform with `chronos.cli halt` and verify with
   `chronos.cli status`.
4. Disarm the live session. If autonomy is configured, revoke the mandate and
   follow the incident runbook's file-handling instruction so a later boot cannot
   restore authority unexpectedly.
5. Stop making changes, capture evidence, inspect the broker directly, and cancel
   working orders manually when the owner decides that is required. Chronos never
   auto-flattens an unexplained position.
6. Do not rearm, disengage the switch, or restore a mandate until the incident is
   explained, reconciliation is clean, and the owner deliberately accepts the
   authority increase.

Restarting is not an emergency stop. It clears process-memory live arming and
terminal sessions, but durable kill, halt, mandate, revocation, ledger, and audit
state have their own persistence rules. Re-inventory after every restart.

## Terminal operations

Open `/terminal/app` on the backend's configured loopback address and authenticate
with the local API token. Never print or commit the token. Scripts may use the
`X-Chronos-Token` header directly; the browser exchanges it for an httpOnly,
`/terminal`-scoped session cookie.

The current terminal mutation surface is derived from
`src/chronos/api/routes/terminal.py`:

- `POST /terminal/alerts/{alert_id}/acknowledge` records that an alert was seen;
  it does not resolve the underlying condition.
- `POST /terminal/live/kill` engages the live kill switch.
- `POST /terminal/live/disarm` removes the in-memory live arm.
- `POST /terminal/mandate/revoke` durably withdraws an active mandate.

Kill and disarm only remove authority and remain reachable on a read-only backend.
Acknowledge and mandate revocation require the writer boundary. The terminal does
not expose `POST /live/arm` or `POST /live/kill/disengage`; those grant authority
and remain explicit token-and-writer operations.

After a backend restart, log in again and re-read the system, mandate, queue,
alerts, and reconciliation state before acting.

## Reconciliation operations

The backend runs reconciliation at startup. A writer also starts the bounded
periodic refresher in `src/chronos/api/reconciliation_loop.py`; it renews expired
submission readiness on configured cadences and leaves readiness to expire when
broker calls fail. A read-only backend does not run that refresher and cannot
publish recovery.

- Read `GET /health` for reconciliation status and generation.
- Use `POST /orders/reconcile` only for a deliberate fresh pass or recovery
  workflow; do not weaken or bypass the readiness latch.
- Treat `SUBMISSION_UNKNOWN`, unexplained positions, and unknown broker orders as
  broker-truth problems. Follow `docs/IBKR_RUNBOOK.md` and
  `docs/INCIDENT_RESPONSE.md`; never repair them by editing SQLite rows.
- Resolve an ambiguous intent only through the audited route documented in the
  runbooks and only from positive broker evidence. Snapshot absence is not proof
  that an order was rejected.

Re-verify wiring in `src/chronos/api/main.py`,
`src/chronos/api/reconciliation_loop.py`, and
`src/chronos/orders/reconciliation_readiness.py` before changing this procedure.

## Database initialization and migrations

Do not cache a schema number or migration head in this skill. Derive both from the
checkout:

```bash
rg -n '^SCHEMA_VERSION' src/chronos/persistence/database.py
.venv/bin/alembic heads
```

`Database.initialize()` creates a fresh wheel database at the current
`SCHEMA_VERSION`. Alembic upgrades supported existing databases to the current
head. These are different paths by design.

Before `make migrate`:

1. Resolve the configured `DATABASE_URL` to one explicit file without exposing
   credentials.
2. Stop every process that can use that database unless the runbook explicitly
   permits the operation online.
3. Make a SQLite-consistent backup and preserve any WAL/SHM sidecars as prescribed
   by `docs/BACKUP_AND_RECOVERY.md`.
4. Confirm the revision set with `.venv/bin/alembic history` and
   `.venv/bin/alembic heads`.
5. Obtain owner approval before touching shared or live state.

Afterward, run initialization/validation against the upgraded database and run
`make gates`. Never edit `schema_version`, scope rows, ledgers, or migrations in
place to force acceptance.

## Backup and restore

Use `docs/BACKUP_AND_RECOVERY.md` as the canonical file inventory and exact SQLite
procedure. Pay particular attention to the live kill-switch file and any
`AUTONOMY_MANDATE_FILE`: the first has a permissive missing-file default, while a
valid unrevoked mandate can activate at backend boot.

Restore procedure:

1. Stop every Chronos process and verify the exact restore target.
2. Buy reversibility by backing up the current target before overwriting it.
3. Restore SQLite and sidecars exactly as the runbook specifies. Never remove WAL
   or SHM files from a running database.
4. Keep the mandate out of the startup path unless the owner explicitly intends to
   restore that authority. Verify the live kill switch rather than inferring its
   state from file absence.
5. Start only the minimum process needed, engage and verify the live kill switch,
   confirm the platform halt, and run the read-only state inventory.
6. Verify the audit chain, corpus when applicable, database integrity, and broker
   reconciliation. Record discrepancies; do not repair broker truth locally.
7. Treat every rearm, kill disengagement, session arm, and mandate restoration as
   a separate owner decision.

## Routine checks

| Duty | Read-only command or source |
|---|---|
| Platform status and halt state | `.venv/bin/python -m chronos.cli status` |
| Audit-chain integrity | `.venv/bin/python -m chronos.cli verify-audit-log` |
| Strategy registry integrity | `.venv/bin/python -m chronos.cli registry verify` |
| Pine corpus integrity | `.venv/bin/python -m chronos.cli verify-corpus` |
| Backend health | `GET /health` |
| Live arm and kill state | authenticated `GET /live/status` or the terminal system panel |
| Local state inventory | `.venv/bin/python .claude/skills/chronos-diagnostics/scripts/state_inventory.py` |
| SQLite integrity | `sqlite3 -readonly <db> 'PRAGMA integrity_check;'` |
| Host clock synchronization | `timedatectl status` |

Logs and durable evidence live in the paths configured by current settings. Common
surfaces include the rotating application log, platform audit JSONL, owner-alert
JSONL, the wheel database, the platform ledger, and TWS/Gateway logs. Derive their
configured paths before collecting them, and never paste secrets or raw account
identifiers into a handoff.

## Known pitfalls

- CWD changes state identity because many defaults are relative paths.
- `make demo` starts the UI target; only `scripts/run_demo.py` forces demo-safe
  environment values.
- The `chronos` console script is the legacy app, not `chronos.cli`.
- A UI or terminal start does not prove the backend is healthy or authoritative.
- A backend can demote itself read-only after losing the writer lease. Restart only
  after identifying the competing writer or lease failure.
- Restart clears some process memory but can re-read durable authority. Never use
  restart as shorthand for halt, kill, revoke, reconcile, or recover.
- One wheel database is bound to one broker/account scope. Repoint the database;
  do not edit the scope row.
- The platform service and backend order plane are separate runtimes with separate
  safety state. A stop in one does not imply a stop in the other.
- A real gateway connection remains an owner-controlled campaign even for a
  read-only command. Do not silently substitute it for demo or recorded evidence.
- Corrected historical passages in runbooks explain old defects; verify the active
  procedure and current code rather than copying the struck-through claim.

## Maintenance contract

Before relying on or updating this skill, re-derive the volatile surfaces:

```bash
make -n backend ui demo migrate
.venv/bin/python -m chronos.cli --help
.venv/bin/python -m chronos.service --help
.venv/bin/python -m chronos.histdata --help
rg -n '^@router\.(get|post)' src/chronos/api/routes/terminal.py src/chronos/api/routes/live.py
rg -n '^SCHEMA_VERSION' src/chronos/persistence/database.py
.venv/bin/alembic heads
make gates
```

Do not add schema versions, migration heads, test counts, branch names, dated
status snapshots, or copied open-finding claims. Point to the authoritative source
and state the decision rule instead. `tests/unit/test_operator_skill_contract.py`
keeps the terminal mutation surface and these anti-staleness rules executable.
