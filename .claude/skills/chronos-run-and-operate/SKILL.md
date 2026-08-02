---
name: chronos-run-and-operate
description: >
  Load this skill whenever you need to RUN or OPERATE Chronos: "start the backend",
  "run the app", "run the UI", "demo mode", "paper trading", "how do I stop it",
  "emergency", "kill switch", "halt", "rearm", "arm live", "disarm", "revoke the
  mandate", "acknowledge an alert", "restart the backend", "restore a backup",
  "reconcile orders", "run the terminal", "histdata backfill", "run migrations",
  or any question about which process does what and how to stop it safely. It is
  the single home for the two stop mechanisms (live kill switch vs platform halt)
  and the backup/restore reality. NOT for env setup (chronos-build-and-env),
  variable meanings (chronos-config-and-flags), diagnosing WHY something refused
  (chronos-debugging-playbook), or the real-gateway campaign
  (chronos-real-gateway-campaign).
---

# Chronos: run and operate

Repo root: `/home/user/Chronos`. All commands assume the repo root as CWD (CLI default
paths like `data/platform_halt.json` are CWD-relative) and the project venv at `.venv/`
(README Setup). Facts dated 2026-08-02; re-verify volatile ones per the last section.

Deployment reality (verified `docs/DEPLOYMENT.md:118-158`): one Linux machine, one
operator, bare **foreground processes**. No systemd units, no containers for the trading
path — the systemd unit in DEPLOYMENT.md is explicitly labeled "FUTURE WORK — no such
entry point exists". "Stop the process" means Ctrl-C / kill the foreground process.

There are TWO subsystems with SEPARATE stop mechanisms. Confusing them is the #1
operator hazard — see "The two stop mechanisms" below before touching anything.

## When NOT to use this skill

| You actually need | Go to |
|---|---|
| Create venv, install deps, container traps | chronos-build-and-env |
| What an env var means, defaults, safety class | chronos-config-and-flags |
| WHY a submission/boot was refused | chronos-debugging-playbook |
| First-ever real-gateway session | chronos-real-gateway-campaign |
| Mandate/autonomy semantics and authority rules | chronos-autonomy-and-mandates |
| Which doc is stale/contradicts code | chronos-docs-map |
| Read-only state-inventory scripts | chronos-diagnostics |

## Process inventory

| Process | Start command | Needs | Can / cannot |
|---|---|---|---|
| Backend API (the ONLY broker-owning, order-writing process; also serves the terminal) | `make backend` → `.venv/bin/python scripts/run_backend.py` (uvicorn `chronos.api.main:create_app`) | `.env`, `data/` DB; binds loopback `127.0.0.1:8765` only (non-loopback `BACKEND_HOST` refuses at load, `settings.py:255-259`) | Owns the single writer lease (the single-writer DB lock — one process may write; invariant: chronos-architecture-contract inv. 3; demotion triage: chronos-debugging-playbook §5); runs reconciliation at startup; auto-activates a valid `AUTONOMY_MANDATE_FILE` (`api/main.py:250-276`) |
| Backend-driven Streamlit UI | `make ui` → `scripts/run_ui.py` → `streamlit run src/chronos/ui/backend_app.py` | Backend must be up first (talks loopback HTTP) | Thin client; no broker handle |
| Legacy in-process Streamlit app | `.venv/bin/streamlit run src/chronos/app.py`; forced-safe: `.venv/bin/python scripts/run_demo.py` | run_demo.py forces `BROKER_MODE=demo`, `ALLOW_ORDER_TRANSMIT=false`, `ALLOW_LIVE_TRADING=false` in the child env (`scripts/run_demo.py:12-19`) | In-process runtime; the pre-backend surface |
| Operator terminal | NOT a separate process — served BY the backend at `http://127.0.0.1:8765/terminal/app` | A running backend + the API token | Read panels + acknowledge/revoke only (see Terminal section) |
| Platform CLI | `.venv/bin/python -m chronos.cli <cmd>` (prog `chronos-platform`) | Run from repo root (CWD-relative `--halt-file`/`--audit-file`) | status/halt/rearm/verify/monitor/backtest/research; NO command arms, transmits, enables live, or touches kill switch/mandate (`cli/main.py:1-10`) |
| Shadow/paper platform service | `.venv/bin/python -m chronos.service [--mode shadow\|paper] [--watch] [--interval N]` | halt/audit files; data under `research/data/raw` | Default SHADOW = capability NO_ORDERS; no flag enables live (`service/__main__.py:1-11`); halts itself on exceptions |
| Histdata capture (read-only data plane) | `.venv/bin/python -m chronos.histdata bars --symbols SPY,QQQ --end-date YYYY-MM-DD --duration-days 365` / `... options --symbols SPY,QQQ` | Gateway + official `ibapi`; its OWN client id `IB_DATA_CLIENT_ID` (must differ from `IB_CLIENT_ID`, validated at load) | Read-only: opens no trading DB, holds no lease, imports no order module (structural test `tests/safety/test_histdata_isolation.py`) |
| Migrations | `make migrate` → `.venv/bin/alembic upgrade head` | `alembic.ini`; `DATABASE_URL` overrides the URL | Only the v2→head upgrade path; fresh DBs never run alembic (`Database.initialize()` creates schema v7 directly) |
| Platform monitor | `.venv/bin/python -m chronos.cli monitor [--ledger data/platform_ledger.db]` or `CHRONOS_MONITOR_MODE=shadow streamlit run src/chronos/monitoring/streamlit_app.py` | Files on disk only | Read-only; imports no broker adapter (test-enforced) |

Traps:

- **`make demo` runs `make ui` (the backend-driven UI), NOT the forced-safe demo
  launcher** (`Makefile:26` is `demo: ui`). The only thing that forces demo-safe env
  vars is `scripts/run_demo.py`. `make ui` against a `.env` configured for ibkr/paper
  is a paper-capable session, not a demo.
- The console script `chronos` is the legacy Streamlit app (`pyproject.toml:47`), not
  the CLI. The CLI is `python -m chronos.cli`.
- Restarting the backend clears live arming and signs every terminal session out
  (both are process memory). That is a feature: restart = known state. It does NOT
  clear the kill-switch file, the halt file, or a mandate file — see below.

## Modes

**Demo (default).** `BROKER_MODE=demo` builds a deterministic in-process `DemoBroker`
— no network, no account, structurally cannot submit (`broker/demo.py:408-413`).
`DEMO_PROFILE` selects the dataset: `safety_cases` (default) or `empty_account`
(`domain/enums.py:11-13`). Demo bars are synthetic, seeded by symbol, bannered
`source="demo"` in the chart.

**Paper.** `BROKER_MODE=ibkr`, `IB_ENVIRONMENT=paper`, `ALLOW_ORDER_TRANSMIT=true`,
`IB_ACCOUNT_ID` set (required for paper transmit, `settings.py:225-231`), and
`IB_ACCOUNT_ALLOWLIST` containing the broker-reported paper account — the paper mode
lock denies on an empty allowlist, an off-list account, or an account not matching
IBKR's paper pattern `D[UF]\d{4,}` (`control/modes.py:126-144`, fed from
`settings.ib_account_allowlist` at `orders/submission.py:262`). VERIFIED: the
paper-vs-live branch is selected purely by the frozen `ib_environment`
(`submission.py:227`). The paper branch does NOT consult arming or the kill switch at
the boundary (`submission.py:241-330`) — but the official adapter's last-line check
still refuses any mutating call while the kill switch is engaged
(`official_ibkr.py:1248-1253`), so engaging the kill switch stops paper submissions
too, one layer down. Enabling `ALLOW_ORDER_TRANSMIT` for the first time is an owner
decision (chronos-config-and-flags §2); the read-only real-gateway campaign precedes
any paper order. NOTE: no real gateway (paper or live) has EVER been connected in
this project's history — see chronos-real-gateway-campaign before any gateway session.

**Live.** NEVER walk a reader toward live casually — live acceptance is an owner
action. Live requires ALL of: the full ADR-0009 config conjunction (startup refuses
otherwise, naming every unmet conjunct — `settings.py:165-199`; variable list in
chronos-config-and-flags), PLUS at runtime the ten-gate walk including a current
per-session arm (typed phrase, TTL, process memory), a per-order typed confirmation,
and the kill switch disengaged. `docs/live_trading_runbook.md` is the operator
reference. The mandate does NOT remove any of this — see the contradiction note below.

## THE TWO STOP MECHANISMS — read before any emergency

Two independent stop mechanisms exist. They stop DIFFERENT subsystems and have
OPPOSITE missing-file defaults. `python -m chronos.cli halt` does NOT stop the live
order plane; `POST /live/kill` does not stop the deterministic platform.

| | (a) Live order-plane kill switch | (b) Deterministic-platform halt |
|---|---|---|
| Stops | `chronos.orders` live-Wheel pipeline via the backend (the only path that can reach a real broker) | `chronos.execution`/`chronos.service` platform loop (shadow/paper research plane; live hard-refused in code anyway) |
| Engage | `POST /live/kill` (token only — deliberately NOT writer-gated, always reachable) | `.venv/bin/python -m chronos.cli halt --reason "..."` |
| Release | `POST /live/kill/disengage` (token + writer lease, non-empty note) | `.venv/bin/python -m chronos.cli rearm --note "..."` (non-empty note required) |
| Persistence file | `data/live_kill_switch.json` (`LIVE_KILL_SWITCH_FILE`) | `data/platform_halt.json` (CLI `--halt-file`) |
| File MISSING | **DISENGAGED — trading-capable** (`orders/kill_switch.py:83-85`) | **HALTED** (`NEVER_ARMED`, `control/halt.py:102-109`) |
| File corrupt/unreadable | ENGAGED (fail closed, `kill_switch.py:86-92`) | HALTED (`STATE_CORRUPTION`, `halt.py:110-117`) |
| Restart behavior | Engaged switch survives restart (file re-read at every check; no boot reset path exists) | Halt survives restart ("re-load, never reset", `halt.py:7`) |
| Who else engages it | Session-drawdown breaker on breach (`session_drawdown.py:119-121`); any component may | Any platform component (audit corruption, strategy exception, reconciliation mismatch, ...) |
| Status | `GET /live/status` (arm + kill state) | `python -m chronos.cli status` (banner + audit chain) |
| HTTP or CLI? | HTTP-only. No CLI command exists for it (`cli/main.py:1-10`; verified: no such subcommand) | CLI-only. No HTTP route exists for it |

**WARNING — the asymmetry that bites:** deleting or losing
`data/live_kill_switch.json` silently DISARMS the live emergency stop (missing =
DISENGAGED). Never "clean up" that file, never repoint `LIVE_KILL_SWITCH_FILE`
casually (a path change orphans an engaged switch), and never assume a
restored/fresh deploy is stopped. `docs/BACKUP_AND_RECOVERY.md`'s file table does
not even list this file (see Backup/restore below).

### Procedures (exact commands)

Token setup for all curl commands (token file created by the backend on first boot,
0600):

```bash
TOKEN=$(cat data/backend_api_token)
```

Engage the live kill switch (the live-plane emergency stop; works even when the
backend has demoted itself to read-only — that is deliberate, `routes/live.py:105-121`):

```bash
curl -sS -X POST http://127.0.0.1:8765/live/kill \
  -H "X-Chronos-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"reason": "operator emergency stop: <one line why>"}'
```

Verify it took (never trust the POST alone):

```bash
curl -sS http://127.0.0.1:8765/live/status -H "X-Chronos-Token: $TOKEN"
# expect: "kill_switch": {"engaged": true, ...}
```

Disengage (explicit operator act; token + writer lease; non-empty note required —
422 otherwise, `routes/live.py:124-133`):

```bash
curl -sS -X POST http://127.0.0.1:8765/live/kill/disengage \
  -H "X-Chronos-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"note": "verified <what you checked>; safe to resume because <why>"}'
```

Platform halt / rearm:

```bash
.venv/bin/python -m chronos.cli halt --reason "SEV-n: <one line>"
.venv/bin/python -m chronos.cli status          # confirm TRADING HALTED banner
.venv/bin/python -m chronos.cli rearm --note "resolved <what>; verified <evidence>"
```

**Restore/incident rule — VERIFY-AND-ENGAGE, never assume.** After any restore,
file operation under `data/`, or doubt about kill-switch state: engage it explicitly
(`POST /live/kill` with a reason like "post-restore default-safe"), then verify via
`GET /live/status`. Engaging is idempotent-safe and monotonically restricting; a
missing file that you assumed was "still engaged" is the failure mode this rule
exists to prevent. If the backend is not running, inspect the file
(`cat data/live_kill_switch.json` — `"engaged": true` present and parseable), and
still re-verify via `/live/status` after boot.

### Emergency stop EVERYTHING (corrected sequence)

`docs/INCIDENT_RESPONSE.md`'s universal first action is `python -m chronos.cli halt`
— that engages ONLY the platform halt and does nothing to the live order plane. This
is a known doc defect (self-disclosed at `docs/VISION_COMPLETION_PLAN.md:146-148`;
ledger home: chronos-docs-map). The complete sequence:

1. **Live plane:** `POST /live/kill` (curl above). Verify via `GET /live/status`.
2. **Platform:** `.venv/bin/python -m chronos.cli halt --reason "..."`.
3. **If a mandate is active:** revoke it durably (curl in the next section) — a
   revocation survives restarts and the revoked mandate version will not
   re-activate (`api/autonomy_wiring.py:145-157`). Additionally move the mandate
   file aside (`mv <mandate-file> <mandate-file>.standdown`) so no future boot
   under any DB/scope can auto-activate it.
4. **Cancel working orders** if needed: `POST /orders/{intent_id}/cancel` —
   cancellation deliberately still works while the kill switch is engaged
   (risk-reducing; `official_ibkr.py:1475-1477`). Or cancel manually in TWS.
5. **Do NOT restart expecting quiet.** A valid, unrevoked `AUTONOMY_MANDATE_FILE`
   auto-activates on every boot (`api/main.py:250-276`, ADR-0017). Restart clears
   arming and terminal sessions but resets NOTHING else. The vision plan's
   "recovery must always boot kill-engaged, read-only, unreconciled" is the
   REQUIRED end-state, still OPEN — today's code boots a missing kill-switch file
   DISENGAGED, so steps 1-3 are the manual bridge.

## Arming, disarming, and mandate operations

**Arm a live session** (writer-gated; grants nothing by itself — it is one of ten
gates; TTL `LIVE_ARM_TTL_MINUTES`, default 15 min, max 120; process memory, restart
clears it):

```bash
curl -sS -X POST http://127.0.0.1:8765/live/arm \
  -H "X-Chronos-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"phrase": "I ACCEPT LIVE TRADING RISK", "reason": "operator arm: <why>"}'
```

The phrase is exact (`orders/arming.py:26`), compared constant-time, never logged or
echoed. Wrong phrase → generic 400.

**Disarm** (token only, no body, no writer lease — removing authority is always
reachable, `routes/live.py:98-102`):

```bash
curl -sS -X POST http://127.0.0.1:8765/live/disarm -H "X-Chronos-Token: $TOKEN"
```

**Mandate activate:** there is no activation endpoint. Activation = a valid mandate
JSON at `AUTONOMY_MANDATE_FILE` + a backend boot (auto-activation, digest-stamped
`persistent-mandate:<digest16>`). Broken/mismatched file → CRITICAL alert, backend
continues WITHOUT autonomy. Authoring a mandate is owner-gated — see
chronos-autonomy-and-mandates.

**Mandate revoke** (writer-gated; `reason` required — 422 if blank; optional
`mandate_id` must match the grant in force or 409; durable, audited into the
authority chain, survives restarts; `routes/terminal.py:702-819`):

```bash
curl -sS -X POST http://127.0.0.1:8765/terminal/mandate/revoke \
  -H "X-Chronos-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"reason": "<why you are standing the supervisor down>"}'
```

`revoked: false` with 200 means nothing was in force — that is an answer, not a
failure. A cycle already in flight finishes; the next one refuses at admission.
The "typed confirmation" for revoke (typing `REVOKE MANDATE`) is a terminal-client
UI gate (`terminal.js:1201-1216`), not a server field — curl needs only the reason.

**The arming-vs-mandate contradiction (one line):** prose in
`docs/live_trading_runbook.md:21-24` says a mandate replaces gates 7+8, but the CODE
requires a current, unexpired arm for every LIVE submit regardless of mandate
(`submission.py:441`; `live_gate.py` has no mandate input) — trust the code; details
in chronos-autonomy-and-mandates.

**Terminal client buttons are acknowledge + revoke ONLY** (`terminal.js:4-8, 78-80`).
Arm/disarm/kill/disengage have no buttons today — they are curl-only with
`X-Chronos-Token`. The terminal cookie is path-scoped to `/terminal` and structurally
cannot reach `/live/*` or `/orders/*`.

## Terminal usage

- Browse `http://127.0.0.1:8765/terminal/app` (shell is unauthenticated by design; every
  data route is credentialed).
- Login: the page posts your token to `POST /terminal/session`, which exchanges it for
  an httpOnly cookie scoped to `path=/terminal`, TTL 12h, max 32 live sessions, stored
  in process memory as SHA-256 digests (`api/terminal_session.py:71-101`). Script
  equivalent:

```bash
curl -sS -c /tmp/chronos-cookies.txt -X POST http://127.0.0.1:8765/terminal/session \
  -H "Content-Type: application/json" -d "{\"token\": \"$TOKEN\"}"
```

- Scripts can skip the cookie entirely: every `/terminal/*` data route also accepts the
  `X-Chronos-Token` header directly.
- Panels (commands typed into the terminal): SYS/system, MAND/mandate, JRNL/journal,
  CNTR/counters, QUE/queue, ALRT/alerts, THESIS/theses, GP `<SYMBOL>`/chart, HELP.
  Panels poll every 5s; the chart every 120s (pacing). `GET /terminal/system` shows
  concrete `kill_switch_engaged` and `live_armed` booleans — the fastest glanceable
  stop-state readout.
- Reads work even on a read-only (lease-lost) backend; the two writes (acknowledge,
  revoke) are writer-gated.
- **A backend restart signs every terminal session out** (sessions are process
  memory) and clears arming. Log in again; re-verify state before trusting panels.

## Reconciliation ops

- Backend startup runs one reconciliation pass; failure leaves submission LOCKED while
  inspection/cancel/recovery still work (`api/main.py:201-236`).
- Readiness starts PENDING and is **consumed by exactly ONE opening submission** —
  after each opening order the latch is PENDING again, and there is NO periodic loop
  to re-arm it (OPEN gap, `docs/VISION_COMPLETION_PLAN.md:143-145`). Before each
  opening order, run a fresh pass:

```bash
curl -sS -X POST http://127.0.0.1:8765/orders/reconcile -H "X-Chronos-Token: $TOKEN"
```

  Do not "fix" a blocked second order by weakening the latch — the missing piece is
  the bounded periodic reconciliation of Phase 2.
- `GET /health` (the only unauthenticated route) reports `reconciliation_status` /
  `reconciliation_generation` for quick checks.
- **MANUAL_REVIEW** (wheel stage): the derivation found conflicting or un-attributable
  evidence — partial assignment, corporate-action warning, account mismatch,
  reconciliation not OK, broker-vs-local disagreement (`strategy/wheel_state.py:107-152`).
  Operationally: no automated action is eligible for that symbol; the owner inspects
  and resolves at the broker; the state clears only when the evidence does.
- **Unknown broker order/position** ⇒ platform halts (`HaltReason.UNKNOWN_ORDER` /
  `UNKNOWN_POSITION`); there is NO auto-flatten anywhere. Resolution is manual at the
  broker (owner gate), then document, then `rearm --note`.
- **SUBMISSION_UNKNOWN stuck intents** block live submits. Resolve via a fresh
  reconciliation pass, or `POST /orders/{intent_id}/resolve` (audited evidence
  refresh; snapshot absence returns 409 and stays locked — that is correct). Never
  edit the database.

## Backup / restore REALITY

What `docs/BACKUP_AND_RECOVERY.md` prescribes (platform ledger + halt file + audit
JSONL + config/specs/research + `data/chronos.db` + `.env`; `sqlite3 .backup` online
or plain copy only while stopped; restore → starts HALTED → verify-audit-log →
verify-corpus → broker reconciliation → explicit `rearm --note`) is correct FOR THE
PLATFORM PLANE ONLY. Two verified defects:

1. **Its file table OMITS `data/live_kill_switch.json`** (also
   `session_baseline.json`, `owner_alerts.jsonl`, `backend_api_token`). A by-the-book
   restore drops the kill-switch file, and missing = DISENGAGED.
2. Its "restore never auto-resumes trading" claims name only platform-halt gates.
   True for the platform; for the live plane, arming (empty after restart) and
   reconciliation (PENDING) block trading, but the kill switch itself contributes
   nothing after such a restore.

The line "recovery must always boot kill-engaged, read-only, and unreconciled"
(`VISION_COMPLETION_PLAN.md:149-150`) is the vision plan's REQUIRED end-state — it is
OPEN, not current code behavior. The manual procedure below is the bridge.

**Safe restore procedure (compensates for the code+doc gaps):**

1. Stop every Chronos process. Verify nothing is running.
2. Back up the current state before overwriting anything
   (`tar czf backup-pre-restore-$(date +%F).tar.gz data/ config/ 2>/dev/null`).
3. Restore files per BACKUP_AND_RECOVERY.md (delete stale `-wal`/`-shm` sidecars for
   `.backup`-produced SQLite files).
4. **BEFORE restarting anything:**
   - Write/verify an ENGAGED kill-switch state: if a known-good
     `data/live_kill_switch.json` with `"engaged": true` was backed up, restore it;
     otherwise plan to engage via `POST /live/kill` immediately after boot and treat
     the system as trading-capable until you have.
   - Verify `data/platform_halt.json` reads HALTED (missing is fine — missing =
     HALTED for this one).
   - Move any mandate file aside (`mv <AUTONOMY_MANDATE_FILE> <file>.restore-hold`)
     so boot cannot auto-activate autonomy.
5. Restart the backend. Immediately: `POST /live/kill` (verify-and-engage), then
   `GET /live/status` and `GET /health`.
6. Verify state: `python -m chronos.cli status` (halt + audit chain),
   `python -m chronos.cli verify-audit-log`, `verify-corpus` if research state was
   restored, and the read-only inventory scripts in chronos-diagnostics.
7. Reconcile against the broker (`POST /orders/reconcile`; resolve
   UNEXPLAINED/UNKNOWN discrepancies at the broker, documented).
8. Only then consider re-enabling anything: `rearm --note` for the platform,
   `POST /live/kill/disengage` with a note for the live plane, mandate file back
   only by explicit owner decision.

## Routine duties

| Duty | Command / action |
|---|---|
| Morning status | `.venv/bin/python -m chronos.cli status` (mode banner, halt state, audit chain) — read it deliberately; do not rearm reflexively |
| Audit-chain verify | `.venv/bin/python -m chronos.cli verify-audit-log` (exit 1 on failure = incident) |
| Registry ledger verify | `.venv/bin/python -m chronos.cli registry verify` (chain + anchor; exit 1 on tamper) |
| Pine corpus verify | `.venv/bin/python -m chronos.cli verify-corpus` |
| Acknowledge an alert | Terminal ALRT panel button, or `curl -sS -X POST http://127.0.0.1:8765/terminal/alerts/<id>/acknowledge -H "X-Chronos-Token: $TOKEN" -H "Content-Type: application/json" -d '{"note": "<what you saw/did>"}'` — note required (422 if inadequate); ack never resolves the condition |
| Watch the alert sink | `tail -f data/owner_alerts.jsonl` (JSONL, local-only by structural test; no email/SMS/webhook exists) |
| Clock-sync check (R-18, manual) | `timedatectl status` — confirm "System clock synchronized: yes". R-18 (OPEN) makes this an operator duty; automated NTP verification is not implemented. NOTE: R-18 points at docs/OPERATIONS.md for this duty, but that doc does not actually spell out the check — this row is the compensating procedure |
| DB integrity spot-check | `sqlite3 -readonly data/chronos.db "PRAGMA integrity_check;"` and same for `data/platform_ledger.db` (read-only flag always; never UPDATE/DELETE ledger rows) |
| Backups | Manual, per Backup/restore section — no backup script exists in scripts/ |

Log locations: `logs/chronos.log` (+.1..5 rotation, wheel/backend structured JSON,
account-masked) · `data/platform_audit.jsonl` (hash-chained platform audit) ·
`data/owner_alerts.jsonl` (alert sink) · `data/platform_ledger.db` (platform order
ledger; read with `sqlite3 -readonly`, queries in docs/OPERATIONS.md) · TWS/Gateway
logs are broker-side (export before the gateway's daily restart rotates them).

## Provenance and maintenance

Compiled 2026-08-02 from code-first verification (evidence priority: executable code >
ops docs). Volatile facts and their one-line re-verification commands:

| Volatile fact | Re-verify with (read-only) |
|---|---|
| Kill-switch missing=DISENGAGED / corrupt=ENGAGED | `sed -n '83,92p' src/chronos/orders/kill_switch.py` |
| Halt missing/corrupt=HALTED | `sed -n '102,117p' src/chronos/control/halt.py` |
| `/live/*` routes, payloads, lease asymmetry | `sed -n '1,135p' src/chronos/api/routes/live.py` |
| No CLI kill/arm/live command; halt/rearm flags | `.venv/bin/python -m chronos.cli --help` |
| Arm phrase constant | `grep -n REQUIRED_ARM_PHRASE src/chronos/orders/arming.py` |
| Revoke route contract (reason 422, mandate_id 409) | `sed -n '702,760p' src/chronos/api/routes/terminal.py` |
| Terminal client mutates only ack+revoke | `sed -n '1,10p;78,80p' src/chronos/terminal/static/terminal.js` |
| Paper branch skips arming/kill; branch by ib_environment | `sed -n '211,330p' src/chronos/orders/submission.py` |
| Readiness consumed by one opening submission | `grep -n "consumed by an opening" src/chronos/orders/reconciliation_readiness.py` |
| Backup doc still omits live_kill_switch.json | `grep -c live_kill_switch docs/BACKUP_AND_RECOVERY.md` (0 = still omitted) |
| Incident runbook still platform-halt-only | `grep -c "live/kill" docs/INCIDENT_RESPONSE.md` (0 = defect stands) |
| Mandate auto-activation on boot | `sed -n '250,276p' src/chronos/api/main.py` |
| `make demo` still aliases `ui` | `grep -n -A1 "^demo:" Makefile` |
| Entry-point flags (service/histdata) | `.venv/bin/python -m chronos.service --help` / `... -m chronos.histdata --help` |

If any re-verification disagrees with this skill, the code wins (AGENTS.md precedence);
update this skill and log the drift in chronos-docs-map's ledger.
