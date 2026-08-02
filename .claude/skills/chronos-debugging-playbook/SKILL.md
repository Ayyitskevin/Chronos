---
name: chronos-debugging-playbook
description: >
  Symptom-to-triage playbook for Chronos failure modes, each with a discriminating
  experiment (a command whose output tells you which cause you have). Load this when
  you hit: "why was this refused", "order blocked", "order rejected", "AMBIGUOUS",
  "everything is blocked", "autonomy not working", "no ticks", "no decisions",
  "backend is read-only", "writer demoted", "401", "403", "409", "test failing",
  "suite fails", "SAFETY TRIPWIRE", "debug", "diagnose", "stale chart", "stale data",
  "halted", "kill switch", "not firing", "gate blocked", "INSUFFICIENT_EVIDENCE",
  "zero trades", "fingerprint mismatch", "schema version", "audit chain FAILED",
  or any behavior that looks broken in a system whose default answer is "no".
  Core doctrine: in this fail-closed system A BLOCK IS USUALLY THE SYSTEM WORKING —
  diagnose which gate fired and why before touching anything, and never weaken a
  gate to make a symptom disappear.
---

# Chronos debugging playbook

Chronos is fail-closed and deny-by-default everywhere. That inverts normal debugging
instinct: **a refusal, a block, or an UNKNOWN is usually the system working as
designed.** Your job is to identify *which* control fired and *whether its evidence
actually arrived* — not to make the block go away. The one dangerous direction is the
opposite one: a control that silently *passes everything* (R-25 did, for months).
Never weaken a gate, default, threshold, or tripwire to clear a symptom. If a gate
seems wrong, that is a finding to surface, not a bug to fix inline (see §10).

Setup used by every command below (run from the repo root; backend on its default
loopback port 8765, token header per `src/chronos/api/auth.py:21`):

```bash
TOKEN=$(cat data/backend_api_token)          # created by the backend on first boot
BASE=http://127.0.0.1:8765
AUTH="X-Chronos-Token: $TOKEN"
```

Read-only DB peeks use Python's sqlite3 in `mode=ro` (never point the app's
`DATABASE_URL` at a `file:` URI — the app refuses those; this is a debugger-side
read):

```bash
.venv/bin/python -c 'import sqlite3,sys; db=sqlite3.connect("file:data/chronos.db?mode=ro",uri=True); [print(r) for r in db.execute(sys.argv[1])]' "SELECT ..."
```

## 1. Where refusals are recorded (the receipts)

| Receipt | Where | What it tells you |
|---|---|---|
| Submit outcome | HTTP body of `POST /orders/{id}/submit` — a `SubmissionOutcome` JSON `{submitted, refusal, submission, detail}` (routes/orders.py:326-330; codes at src/chronos/orders/submission.py:92-111). Refusals return 200 with `submitted:false`, they do not raise; lifecycle misuse (wrong stage) is a 409. | Which gate chain stopped the order, coarse-grained |
| Risk decision | `POST /orders/propose` response `{order, risk_decision_id, risk_approved}` (routes/orders.py:81-84); full per-check rows persist in DB tables `risk_decisions` (overall_result, evidence JSON) and `risk_check_results` (check_name, status, detail, evidence per check) keyed by `decision_id` (src/chronos/persistence/schema.py:458-487) | Exactly which risk check FAILed/UNKNOWNed and its stated reason |
| Order lifecycle | DB table `order_events`: one row per transition with `event_key`, `from_status`, `to_status`, `source`, `evidence` JSON (schema.py:431-456) | Whether/where an intent moved (DRAFT → VALIDATED/REJECTED → … → SUBMISSION_UNKNOWN → …) |
| Owner alerts | `GET /terminal/alerts` (terminal credential, NOT writer-gated) or the JSONL sink `data/owner_alerts.jsonl` (`AUTONOMY_ALERT_FILE`, settings.py:122). Note `GET /autonomy/alerts` IS writer-gated (routes/autonomy.py:183) — use the terminal route on a demoted backend. | CRITICAL/WARNING autonomy events (bad mandate, tick failures, refusals) |
| Autonomy cycle journal | `GET /terminal/journal` — hash-chained cycle records with `stage`, `refusal`, `detail` per cycle (src/chronos/terminal/views.py:245-283) | Why an autonomous decision stopped — **but see the COMPLETE trap, §4** |
| Live state | `GET /live/status` → `{arm, kill_switch}` (routes/live.py:77-83); `GET /terminal/system` → `read_only, autonomy_configured/stopped, queue_depth, kill_switch_engaged, live_armed, mandate_active…` (views.py:183-203) | Arming/kill/lease/autonomy at a glance |
| Backend health | `GET /health` (unauthenticated) → `{status, broker_mode, environment, read_only, writer_lease_held, reconciliation_status, reconciliation_generation}` (routes/health.py:21-44) | The 10-second triage of any "nothing works" report |

Per-check risk receipt, the single most useful query when an order is refused:

```bash
.venv/bin/python -c 'import sqlite3,sys; db=sqlite3.connect("file:data/chronos.db?mode=ro",uri=True); [print(r) for r in db.execute("SELECT sequence,check_name,status,detail FROM risk_check_results WHERE decision_id=? ORDER BY sequence",(sys.argv[1],))]' "<risk_decision_id>"
```

## 2. "My order was refused" — the gate walk

First read `refusal` from the `SubmissionOutcome`. Three distinct paths exist; know
which one you are on. Branch selection is config alone: `ib_environment` LIVE picks
the live branch, everything else paper (submission.py:227).

**PAPER branch** (submission.py:241-330), in refusal order — no arming, no kill
switch on this branch (the official adapter still refuses mutating calls last-line
when the kill switch is engaged, official_ibkr.py:1248-1253):
lease → `transmission_possible` → mode lock (PAPER_SUBMISSION) → account match →
fresh broker connection status → reconciliation latch ready → risk evidence
generation/session current → risk approved+unexpired → confirmation valid → CAS +
transmit.

**LIVE branch** (submission.py:336-496): wired dependencies →
`live_transmission_possible` → live grant → account match → reconciliation + risk
evidence generation → no stuck SUBMISSION_UNKNOWN intents → fresh clock → the
ten-gate walk (live_gate.py:23-34): `config, connection, reconciliation, data, risk,
preview, session_arming, per_order_confirmation, kill_switch, session_drawdown` —
kill-switch file read LAST, then re-read again between CAS and transmit.

**Autonomy handoff** (autonomy_wiring.py:185-205): walks the FULL
propose→preview→confirm→submit pipeline. It programmatically mints the gate-8
confirmation record, but does NOTHING for gate 7 — a LIVE-environment autonomous
order blocks at `session_arming` unless an operator armed within the TTL. That is
the standing, owner-gated arming-vs-mandate contradiction (VISION_COMPLETION_PLAN.md
Phase 1 item 4) — do not "fix" it in either direction (§10).

Per-refusal triage — each row is symptom → likeliest causes → discriminating command:

| `refusal` / gate | Usual cause | Discriminating experiment |
|---|---|---|
| `READ_ONLY_LEASE` | Backend demoted or booted read-only | `curl -s $BASE/health` — `read_only:true` ⇒ §5 |
| `TRANSMISSION_NOT_POSSIBLE` / config gate | `.env` conjunction unmet (BROKER_MODE, ALLOW_ORDER_TRANSMIT, account id…) | `.venv/bin/python -c 'from chronos.config.settings import get_settings; s=get_settings(); print(s.ib_environment, s.transmission_possible, s.live_transmission_possible)'` — the two properties are mutually exclusive by design (settings.py:267-301) |
| `MODE_FORBIDS` | Mode lock denial (live is hard-denied in that lock; paper needs allowlist+paper env+transmit flag) | The `detail` string carries `lock.denial_reasons` verbatim (submission.py:268-271) |
| `ACCOUNT_MISMATCH` | Intent minted against a different account than the connected one | Compare intent account vs `GET /account/summary`; also check `database_scope` (§9) |
| `RECONCILIATION_NOT_READY` | (a) startup reconciliation never ran/failed; (b) connection blipped (generation++); (c) **the latch was consumed by your previous opening order — single-shot by design** (reconciliation_readiness.py:129-158) | `curl -s $BASE/health` → `reconciliation_status`/`generation`. If the reason says "consumed by an opening-order submission", that is design: run `curl -s -X POST -H "$AUTH" $BASE/orders/reconcile` and re-submit. Never weaken the latch — the missing periodic re-arm loop is known Phase 2 work |
| `RISK_NOT_APPROVED` | One or more risk checks FAIL/UNKNOWN | Query `risk_check_results` (§1). Then use the check-name table below |
| `RISK_EXPIRED` / `RISK_EVIDENCE_STALE` | Decision TTL (60 s default, risk.py:35) elapsed, or reconciliation generation moved | Re-propose; slow human loops between propose and submit are the usual cause |
| `CONFIRMATION_MISSING/EXPIRED/MISMATCH`, `INTENT_NOT_CONFIRMED` | Confirmation TTL (20 s default, settings.py:85) elapsed or hash mismatch (economic terms changed) | Re-run confirm; check `order_confirmations` timestamps |
| `LIVE_DEPENDENCIES_MISSING` | Boundary constructed without arming/kill/drawdown/quote wiring — a LIVE config refuses to even construct without them | Boot logs; this is a wiring bug, not an operator problem |
| `LIVE_GRANT_DENIED` | `resolve_live_submission` deny-by-default (account pattern/allowlist/observed env) | `detail` names the unmet conjunct; check `IB_ACCOUNT_ALLOWLIST`, `U`-pattern account id |
| `LIVE_GATE_BLOCKED` | One of the ten gates | `detail` names the gate. `session_arming` ⇒ `GET /live/status` (arm is process memory, TTL default 15 min ≤ 120, cleared by restart — arming.py:4-6, settings.py:83). `kill_switch` ⇒ kill row below. `session_drawdown` ⇒ breach engages the kill switch automatically; unreadable NLV refuses WITHOUT engaging (submission.py:572-584) |
| kill switch engaged | Operator engaged it, drawdown breaker engaged it, or the file is corrupt (corrupt ⇒ ENGAGED fail-closed; **missing ⇒ DISENGAGED**, kill_switch.py:83-92) | `curl -s -H "$AUTH" $BASE/live/status` — `kill_switch.reason` and `initiated_by` say who and why. `cat data/live_kill_switch.json`. Disengage is a deliberate writer-gated operator act with a note, never something a debugger does to "unblock" |
| `BROKER_REFUSED_BEFORE_SEND` | Adapter pre-send re-verification (env/port, account membership, last-line kill) — a proof-claim that no bytes left | `detail` carries the adapter's words; cross-ref chronos-ibkr-boundary |
| `BROKER_SUBMIT_FAILED` | Ambiguous send — the intent is parked `SUBMISSION_UNKNOWN` and never auto-retried | `order_events` for the intent; operator `POST /orders/{id}/resolve` with a note after checking broker truth. Stuck SUBMISSION_UNKNOWN ids also block further live submits (submission.py:457-461) |

Risk-check names you will see in `risk_check_results` (tri-state; UNKNOWN blocks,
overall PASS only if every check PASSes — risk.py:140-178): `symbol_family_eligibility`,
`reconciled_proven_state`, `market_open`, `limit_only`, `max_contracts_per_order`,
`max_opening_orders_per_day`, `standard_deliverable_verified`, `cash_secured_put`,
`covered_call_coverage`, `concentration`, plus family caps. Three of these were once
famous for never firing — that is §3.

- `market_open` UNKNOWN = session AMBIGUOUS (evidence absent/unparseable); FAIL =
  venue says CLOSED. Different causes: see §3.
- `max_opening_orders_per_day` UNKNOWN ("today's opening count is unavailable") =
  evidence gathering failed (DB/count error ⇒ blocked, R-25 doctrine); FAIL = you
  genuinely hit the cap (default 3/day, market-local midnight).
- `standard_deliverable_verified` FAIL = the qualified option did not pass the
  five-condition standard-deliverable screen — after a corporate action this is the
  system saving your account math. Cross-ref chronos-wheel-and-options; never loosen
  the screen.

## 3. "Everything is blocked / AMBIGUOUS" — is the evidence ARRIVING?

The most important lesson this repo owns: **AMBIGUOUS-blocks-everything was a live
defect for months while looking exactly like correct fail-closed behavior.** From M5
to M9 the session-gate evidence supplier hard-returned `None`, so every in-RTH
equity/option instant judged AMBIGUOUS and blocked (R-26). The gate was complete,
correct, documented, and tested — and had never once said OPEN. The evidence was
arriving on every qualification and being dropped one attribute short:
`liquidHours`/`timeZoneId` live on IBKR's `ContractDetails`, not on the `Contract`
inside it (fix: 701ebf4; current read: src/chronos/orders/evidence.py:183-211 off the
qualified contract). Cross-ref chronos-ibkr-boundary for the full nested-object map.

So when a gate blocks *everything*, ask two separate questions:

1. **Is the gate refusing on real evidence?** (system working) — the check's
   `detail` states a concrete venue/limit fact.
2. **Is the evidence ever arriving at all?** (control possibly inert or starved) —
   the detail says "unavailable"/"AMBIGUOUS" for every instrument at every time.

Discriminating experiments for evidence-starvation, per cause:

| Evidence is None because… | How to confirm |
|---|---|
| Adapter didn't enrich (the ContractDetails class of bug — R-26/R-27 pattern, or a hand-built fixture contract without `liquid_hours`/`time_zone_id`) | Inspect the risk decision's `evidence` JSON (`risk_decisions.evidence`) — session fields empty for a *qualified* contract during RTH is the signature. Fixture-built contracts blocking AMBIGUOUS is the system working (blank evidence means unknown, never "no restrictions") |
| No broker connection / demo mode | `curl -s $BASE/health` → `broker_mode`; `GET /account/connection`. Demo enriches by fiat; ib_async/official enrich only at real qualification |
| Stale quotes (data gate) | Live data gate FAILs on no quote / age > `MAX_QUOTE_AGE_SECONDS` (5 s default) / quality not LIVE-or-DELAYED (submission.py:586-619) — the `detail` names which |

**The "is the control exercised or inert?" question.** A control can be fully wired,
documented, and green in CI yet structurally unable to fire — four kernel defects
(R-24..R-27) were exactly this. Two detectors:

```bash
# 1. Does an *_exercised test drive the full path and assert the outcome that
#    "never happens" (OPEN / a refusal / PASS)? These exist for the three
#    once-inert controls; a NEW control without one is unproven:
ls tests/safety/*exercised*   # test_opening_cap_exercised.py, test_session_gate_exercised.py

# 2. Has the check EVER produced its full outcome range in this deployment?
.venv/bin/python -c 'import sqlite3; db=sqlite3.connect("file:data/chronos.db?mode=ro",uri=True); [print(r) for r in db.execute("SELECT check_name,status,COUNT(*) FROM risk_check_results GROUP BY 1,2 ORDER BY 1")]'
# A check that is 100% PASS forever, or 100% UNKNOWN forever, deserves suspicion
# either way. 100% PASS is the dangerous direction (R-25 failed OPEN).
```

When you add or fix a gate, the proof pattern is a firing assertion: a
`tests/safety/test_<control>_exercised.py` that drives qualified-evidence →
provider → check end-to-end and asserts each outcome, then reverting each half of
the fix to confirm a distinct test fails (the M9-M11 discipline; cross-ref
chronos-validation-and-qa).

## 4. "Autonomy is inert / no ticks / no decisions" — the decision tree

Walk in order; stop at the first hit. Primary instruments: `GET /terminal/system`,
`GET /terminal/mandate`, `GET /terminal/queue`, `GET /terminal/journal`,
`GET /terminal/alerts`, and `tail data/owner_alerts.jsonl`.

1. **`autonomy_configured: false`** ⇒ no `AUTONOMY_MANDATE_FILE` set. Inert **by
   design** — "no mandate file → no runtime" is ADR-0017's one kept non-maximal
   default. Not a bug.
2. **Configured, but no runtime / `mandate_active` null** ⇒ the file failed
   validation or is scoped to a different account. Signature: CRITICAL alert
   `autonomy.mandate_invalid` ("unreadable, invalid, or scoped to a different
   account; autonomy is inert until it is fixed" — autonomy_wiring.py:370-386). The
   backend continues WITHOUT autonomy on purpose. Fix the file/account; never bypass
   validation.
3. **Revoked** ⇒ durable. WARNING alert `autonomy.revoked_mandate_present`
   (autonomy_wiring.py:145-157); `GET /terminal/mandate` shows `revoked: true`.
   Restart does NOT clear a revocation; re-granting requires a new `mandate_version`
   in the file — an owner act.
4. **Expired** ⇒ `GET /terminal/mandate` `expires_at` in the past; admission refuses
   every decision. Renewal is a fresh owner act (365-day live ceiling).
5. **Runtime up, ticking, queue empty, zero decisions** ⇒ **no model worker exists
   in this repository — that is reality, not a bug.** Nothing in-repo produces
   proposals; the autonomy plane is a judged pipe with an empty producer unless the
   owner runs an external worker that POSTs `ProposedDecision` JSON to
   `/autonomy/proposals` (docs/limitations.md:281-283). Check `queue_depth` on
   `/terminal/system`.
6. **Decisions arrive but every cycle refuses** ⇒ read `GET /terminal/journal`
   entries (`stage`, `refusal`, `detail`) — admission has 15 ordered checks
   (mandate, degraded state, activation, window, account, mode, replay budget,
   version pins, evidence bundle, scope, strategy, promotion, order forms, data
   freshness). Version-pin refusals with an external worker usually mean the mandate
   does not pin the static `INGRESS_IDENTITY` constants (autonomy_wiring.py:84-94).
7. **Ticks stopped** ⇒ `autonomy_stopped: true`. The runtime stops itself after
   `max_consecutive_failures` (default 5, supervisor/runtime.py:115) with a CRITICAL
   alert already raised; each failing tick also alerts. Restart of the backend is the
   deliberate operator recovery (runtime.py:379-391).

**TRAP — `stage: COMPLETE` does not mean an order was submitted (STILL OPEN).** The
supervisor records COMPLETE for ANY non-exception handoff return; a
`SubmissionOutcome(submitted=false)` refusal (e.g. LIVE_GATE_BLOCKED because nobody
armed) journals as COMPLETE with the outcome buried in `handoff`
(supervisor/loop.py:405-453; VISION_COMPLETION_PLAN.md Phase 1 item 5).
Discriminating experiment: never trust the journal alone — cross-check the order
plane's own records (`GET /orders`, `order_events`) for an intent matching the
cycle. Do not hot-fix by importing order-plane result types into the supervisor; the
untypedness is deliberate isolation (loop.py:161-164). Owner-gated design work.

## 5. "Backend is read-only / writer demoted"

Two ways in, one way out:

- **Booted read-only**: another backend (or a dead one's unexpired lease row) held
  the single-writer lease at startup (main.py:158-180).
- **Demoted at runtime**: ONE failed lease renewal (heartbeat = TTL/3 = 10 s)
  demotes to read-only **permanently until restart** (main.py:110-153). This is not
  a retry budget: a lease that failed to renew may already belong to another writer,
  and re-acquiring in place is deliberately unsafe (split-brain, R-24). **Restart is
  the recovery.**

Confirm and triage:

```bash
curl -s $BASE/health            # read_only:true, writer_lease_held:false
# Who holds the lease (read-only DB peek — one row, id=1):
.venv/bin/python -c 'import sqlite3; db=sqlite3.connect("file:data/chronos.db?mode=ro",uri=True); [print(r) for r in db.execute("SELECT id,holder,expires_at FROM writer_lease")]'
```

Mutating routes answer 409 with "This backend is running read-only…"
(dependencies.py:42-55). Read-only ≠ dead, deliberately: all terminal reads,
`POST /live/kill`, and `POST /live/disarm` still work (authority-removing operations
are reachable without the lease, live.py:7-24). `POST /live/kill/disengage` and
`/orders/*` POSTs do not. If two processes might be running, find and stop the
stray one before restarting; do not delete the lease row.

## 6. Terminal problems

| Symptom | Cause (usually by design) | Discriminating experiment |
|---|---|---|
| 401 on every panel | Terminal session cookie expired (12 h TTL) **or the backend restarted — sessions and arming are process memory; a bounce signs every terminal out** (terminal_session.py; the client says exactly this, terminal.js:153). Terminal 401s are deliberately cause-free (routes/terminal.py:299-311) | Re-login: `POST /terminal/session` with the token. If the TOKEN itself 401s, you are reading a different `data/backend_api_token` than the running backend's CWD |
| Panels dark / all "unavailable" | Backend down — vs demoted, which still serves reads (reads are deliberately NOT writer-gated, routes/terminal.py:24-37) | `curl -s $BASE/health` — connection refused ⇒ down; `read_only:true` ⇒ demoted but panels should still populate |
| Chart shows stale label / refuses | Pacing degrade **by design**: 6 requests/rolling minute + 15 s per-key cooldown (marketdata/pacing.py:40-42); paced-out requests serve cache marked `stale` or refuse with a stated reason; budget is recorded BEFORE the call (bars.py:194-199). During a histdata backfill expect MORE staleness — two processes self-pace against a possibly-shared real IBKR limit (R-42) | Read `refusal`/`stale`/`source` in the `GET /terminal/bars` response (views.py:931-945). A non-`ibkr` source banner in demo mode is honesty, not breakage |
| No arm/kill buttons in the terminal | **By design today**: the shipped client knows exactly two mutating routes — acknowledge and revoke (terminal.js:4-8). The terminal cookie is path-scoped to `/terminal` and structurally cannot reach `/live/*` or `/orders/*` | Arm/kill via curl with the token header, e.g. `curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' -d '{"reason":"<why>"}' $BASE/live/kill`. Do not widen the cookie path to "fix" this |
| `/terminal/counters` 503 | `AUTONOMY_MARKET_TIMEZONE` unusable (not validated at load) | Fix the env var; check `TZ` database availability |

## 7. Test-suite failures that are not code bugs

| Symptom | Cause | Fix (never the other thing) |
|---|---|---|
| `make test` → `.venv/bin/python: No such file or directory` | Fresh checkout has no `.venv`; every Makefile target hard-codes it | `python3.12 -m venv .venv` then lockfile install — cross-ref chronos-build-and-env. Container default `python3` is often 3.11 < `requires-python >= 3.12` |
| Suite-wide failure "SAFETY TRIPWIRE: ambient settings are live-capable inside pytest" | Your repo-root `.env` is live-capable and leaked into the test env — two autouse ADR-0009 tripwires in tests/conftest.py:17-52 fail the run on purpose | **Fix your `.env`. Never touch the tripwire.** Also fires per-test if a test leaves cached settings live-capable |
| Terminal-client tests skip locally / fail in CI | `node` not on PATH: skip locally, hard AssertionError when `CI` is set (tests/safety/test_terminal_client.py:57-68) | Install node locally; never strip node from a CI image |
| Collection error on a new `@pytest.mark.<x>` | `--strict-markers` is on and exactly ONE marker is registered: `ibkr` (pyproject.toml:61-67) | Register the marker in pyproject; same for config keys (`--strict-config`) |
| `ibapi` missing | Intentional — not on PyPI, not in the lock; adapters lazy-import with install guidance (official_ibkr.py:202-206). The whole suite passes without it | Nothing. Do not pip-install a package named `ibapi` |
| `1 skipped` in a green run | `tests/integration/test_ibkr_smoke.py` — opt-in only via `CHRONOS_RUN_IBKR_SMOKE=1` | Expected. Green means exactly 1 skip (~2489 passed as of 2026-08-02; authoritative baseline: chronos-validation-and-qa §2) |

## 8. "Research says INSUFFICIENT_EVIDENCE / zero selected"

**That is a SUCCESS result.** The verdict machinery's blocking default is
INSUFFICIENT_EVIDENCE, and a correct NO_TRADE is success when evidence is
insufficient (AGENTS.md:23-24). The canonical example: the best cell in the entire
completed campaign — regime_trend_v1 on QQQ — made 18 closed trades against the
frozen ≥ 20-trade C4 floor, so zero strategies were selected. 18 < 20 is the floor
working, not a near-miss to round up. **Do not tune thresholds**; they were frozen
before observation and a failed gate rejects the candidate, it does not invite
threshold edits (AGENTS.md:27-28).

One config artifact masquerades as a research result: the walk-forward CLI defaults
to the deny-all policy `config/risk.example.yaml` ⇒ 0 trades ⇒ vacuous
INSUFFICIENT_EVIDENCE. Pass `--policy config/risk.research.yaml` for a non-vacuous
run. Everything else — DSR floors, trial counting, holdout state, the burned QQQ
window — lives in chronos-research-methodology.

## 9. DB / state oddities

| Symptom | Cause | Discriminating experiment / fix |
|---|---|---|
| Boot refusal: "already bound to a different broker scope; configure a separate DATABASE_URL" (database.py:199-200) | You pointed processes at a DB fingerprint-bound to a different (broker_mode, environment, account) | `SELECT broker_mode, environment, account_fingerprint FROM database_scope` (read-only peek — see the setup block at the top). Fix `DATABASE_URL` — one DB per scope. Never edit or delete the scope row |
| Boot refusal: unsupported schema version / drift | DB version ≠ `SCHEMA_VERSION = 7` (database.py:20) or byte-level drift | `SELECT version FROM schema_version` and `PYTHONPATH=src .venv/bin/alembic heads` (→ `0006 (head)`; note the numberings differ: alembic head 0006 = schema v7). The refusal text itself says the fix: back up first, then `alembic upgrade head` (`make migrate`). Chronos never modifies such a DB itself. Fresh DBs never run alembic — `initialize()` creates v7 directly |
| `python -m chronos.cli verify-audit-log` → FAILED | (a) crash-corrupted LAST record (append interrupted — the platform halts on this, fail closed); (b) mid-chain break = edit/corruption/tamper; (c) know the honest bound: hash chains are tamper-EVIDENT, not tamper-proof — a full consistent rewrite is undetectable without an external anchor (hash_chain.py:38-45; R-33, ACCEPTED) | The verifier reports where the chain broke. The registry ledger adds an on-host head anchor (`registry.head.json`) so tail truncation is detected: `python -m chronos.cli registry verify`. Treat any break as an incident to surface, not a file to repair in place |
| `chronos.db-wal` / `-shm` files present | Normal: the DB *requires* `journal_mode=WAL` + `synchronous=FULL` and refuses to run without them (database.py:390-451). Recent commits live in `-wal` until checkpointed | Back up sidecars together with the DB (`sqlite3 .backup` online, or plain copy only while stopped); on restore, delete the dead instance's stale `-wal`/`-shm` per BACKUP_AND_RECOVERY.md:45-70. Never delete sidecars under a running process |
| `chronos.cli status` shows fresh `HALTED (NEVER_ARMED)` unexpectedly | CLI paths are CWD-relative — you ran it outside the repo root and read/created `data/platform_halt.json` somewhere else | `pwd`; re-run from the repo root or pass `--halt-file` explicitly |
| Halt vs kill confusion | TWO mechanisms, opposite missing-file defaults: platform halt `data/platform_halt.json` (missing ⇒ HALTED) governs only the deterministic platform; live kill switch `data/live_kill_switch.json` (missing ⇒ DISENGAGED, corrupt ⇒ ENGAGED) governs `chronos.orders`. `python -m chronos.cli halt` does NOT stop the live plane; `POST /live/kill` does | `ls -l data/platform_halt.json data/live_kill_switch.json` + `GET /live/status`. A restore that omitted the kill-switch file booted the live plane disengaged — a known, owner-flagged finding (VISION_COMPLETION_PLAN.md:146-150), not yours to re-default solo |

## 10. Stories with scars (why "just fix it" is banned here)

Full chronicle in chronos-failure-archaeology; these five shape this playbook:

- **A zero ceiling authorized everything** (4b6bc9e, M2). `size_order()` *skipped*
  zero mandate ceilings instead of binding on them: a mandate authorizing nothing
  sized 590 shares. The docstring said "zero authorizes nothing"; the arithmetic did
  the opposite. Moral: verify the arithmetic, not the docstring.
- **Chart budget recorded after the call** (first M8c implementation; R-42 row). A
  failed fetch consumed no pacing budget, so a bad symbol retried unthrottled on
  every poll — against the connection that submits orders. Now recorded BEFORE the
  call (bars.py:194-199), pinned by its own test.
- **A test pinned the defect for six milestones** (c72a8e5, M11/R-27).
  `tests/unit/test_ibkr_broker.py` asserted `deliverable_verified is False` — green
  CI actively asserting the control could never pass. It now asserts `is True`
  (test_ibkr_broker.py:599-604). Moral: a passing test can be the bug.
- **The SELL-only cap counter** (654f842, M10/R-25). `count_opening_since` had zero
  callers AND filtered `action == SELL` — OPEN∧SELL is only the two option intents,
  so every stock/crypto opening would have stayed invisible even once wired. And the
  evidence field's `0` default read as full headroom: the only kernel defect that
  failed OPEN.
- **ADR claim vs safety test, contradicting for four milestones** (3199a17, M8d).
  ADR-0016 §5 claimed decision narratives were "recorded, displayed, and audited"
  while a safety test forbade any module from reading them — the promise was
  unimplementable as written. Fixed by narrowing the guard (named-recorder
  exemption), not weakening it.
- **STILL OPEN — supervisor COMPLETE-on-refusal** (§4 trap). A "submitted" autonomy
  cycle may not have been. Treat every cycle-journal COMPLETE as "reached the order
  plane", nothing more, until Phase 1 item 5 lands.

## 11. Escalation — what you never "fix" solo

Diagnosis is yours; these changes are not. Surface a finding (per AGENTS.md:54:
stop and surface contradictions, never average them) instead of patching:

- **Anything in the closed-boundary table** (single transmit site, model-plane
  isolation, deny-by-default scopes, kill-switch precedence, revocation semantics,
  …) — see chronos-autonomy-and-mandates. Reopening any row needs a new ADR + owner
  decision.
- **Gate semantics or ordering** (ten-gate walk, paper chain, reconciliation
  single-shot latch, fail-closed defaults like `opening_orders_today: int | None`),
  and settings the validators forbid changing (`REQUIRE_LIVE_ARMING`,
  `REQUIRE_TYPED_CONFIRMATION`, loopback `BACKEND_HOST`).
- **Thresholds and floors** — frozen before observation (research gates, drawdown
  limits, caps). A block at a threshold is evidence, never an invitation.
- **The standing contradictions**: mandate-vs-arming (Phase 1 item 4), kill-switch
  missing-file default vs recovery doctrine (Phase 1 items 2-3), supervisor
  COMPLETE-on-refusal (item 5). All are owner-gated design decisions already on the
  canonical plan.
- Anything that would connect to IBKR, transmit, or arm as a "diagnostic step".
  Diagnosis here is read-only.

## 12. When NOT to use this skill

- Operating normally (start/stop, arm/kill procedures, backups) →
  **chronos-run-and-operate**.
- The historical *why* behind a defect or pivot → **chronos-failure-archaeology**.
- Writing the test that proves a fix → **chronos-validation-and-qa**.
- Adapter/nested-object details (Contract vs ContractDetails map, pacing internals)
  → **chronos-ibkr-boundary**.
- Statistical verdicts, holdouts, trial counts → **chronos-research-methodology**.
- Env-var/config reference → **chronos-config-and-flags**.
- Broad read-only state inventory scripts → **chronos-diagnostics**.

## Provenance and maintenance

Compiled 2026-08-02 from the live repo (branch claude/chronos-skills-library-bfbj29,
HEAD 47a8d72). Volatile facts and their re-verification commands:

| Fact (2026-08-02) | Re-verify with |
|---|---|
| Refusal codes / SubmissionOutcome shape | `grep -n "class SubmissionRefusalCode" -A 22 src/chronos/orders/submission.py` |
| Ten gate names & order | `sed -n '20,40p' src/chronos/orders/live_gate.py` |
| Risk check names / tri-state | `grep -n '_passed("\|_failed("\|_unknown("' src/chronos/orders/risk.py` |
| Receipt tables (risk_check_results, order_events) | `grep -n '__tablename__' src/chronos/persistence/schema.py` |
| Route inventory (/terminal, /live, /autonomy) | `grep -rn "@router" src/chronos/api/routes/` |
| Kill-switch defaults (missing ⇒ DISENGAGED, corrupt ⇒ ENGAGED) | `sed -n '80,95p' src/chronos/orders/kill_switch.py` |
| Lease demotion is permanent-until-restart | `sed -n '110,155p' src/chronos/api/main.py` |
| Tick stop after 5 consecutive failures | `grep -n max_consecutive_failures src/chronos/supervisor/runtime.py` |
| COMPLETE-on-refusal still open | `sed -n '405,455p' src/chronos/supervisor/loop.py` + VISION_COMPLETION_PLAN.md Phase 1 item 5 |
| Schema v7 / alembic head 0006 | `grep -n SCHEMA_VERSION src/chronos/persistence/database.py`; `PYTHONPATH=src .venv/bin/alembic heads` |
| Green baseline: 1 skip, ~2489 passed (home: chronos-validation-and-qa §2) | `.venv/bin/python -m pytest -q` |
| Pacing 6/min + 15 s cooldown; TTLs (arm 15 m, confirm 20 s, risk 60 s, quote 5 s) | `grep -n _DEFAULT src/chronos/marketdata/pacing.py`; `grep -n "ttl\|_TTL" src/chronos/config/settings.py src/chronos/orders/risk.py` |
| Scar commits exist | `git log --format='%h %s' -1 4b6bc9e 3199a17 654f842 c72a8e5 701ebf4` (one at a time) |
