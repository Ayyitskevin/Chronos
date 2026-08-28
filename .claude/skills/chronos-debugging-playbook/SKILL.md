---
name: chronos-debugging-playbook
description: >
  Source-driven, read-only triage for Chronos refusals and failures. Load this
  skill for order blocks, rejection or ambiguity, inert autonomy, missing ticks
  or decisions, writer demotion, terminal authentication or stale data, safety
  tripwires, test failures, schema drift, audit-chain failures, and research
  outcomes that look broken. A block is often the fail-closed system working;
  identify the control and its evidence before proposing a change.
---

# Chronos debugging playbook

Chronos is deny-by-default. A refusal is an observation, not permission to make
the refusal disappear. The dangerous failure direction is a control that cannot
fire or silently passes missing evidence.

**Diagnosis is read-only.** Do not arm, disarm, engage or disengage a kill switch,
reconcile, restart, migrate, edit durable state, start a worker, connect a broker,
or submit an order while following this skill. Route an established operational
action to `chronos-run-and-operate`; route any code, gate, threshold, authority, or
safety-mechanism change through `chronos-change-control`.

## 1. Establish truth before interpreting the symptom

Work from the repository root and record the exact tree first:

```bash
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git status --short
.venv/bin/python .claude/skills/chronos-diagnostics/scripts/state_inventory.py
.venv/bin/python .claude/skills/chronos-diagnostics/scripts/doc_drift_check.py
.venv/bin/python .claude/skills/chronos-diagnostics/scripts/env_check.py
```

The `chronos-diagnostics` observers derive durable state without contacting a
broker or network service. A handoff, dated report, skill, or remembered default
is only a claim until the checkout or running process confirms it.

Capture one bounded incident packet before branching into causes:

- exact commit and process start time;
- operation, endpoint, or command that produced the symptom;
- HTTP status plus structured `refusal` and `detail`, if present;
- timestamp, broker mode, environment, account scope, and writer/read-only state;
- relevant alert or log event names, with credentials and account identifiers
  redacted;
- identifiers already returned by the system: order, risk decision, cycle, or
  reconciliation generation.

Do not reproduce a mutating request merely to improve the packet. Diagnose from
the existing receipt, durable records, and read surfaces.

## 2. Source hierarchy and live discovery

Use this precedence when two accounts disagree:

1. observed running-process response, log, and read-only durable state;
2. current source, migrations, tests, and `.github/workflows/ci.yml` at the exact
   commit;
3. accepted `DECISIONS.md` and the ADRs it cites;
4. `AGENTS.md`, `docs/AGENT_PROTOCOL.md`,
   `docs/VISION_COMPLETION_PLAN.md`, `RISK_REGISTER.md`, and other prose;
5. historical reports and skills.

The repository wins over every snapshot. Read corrections in place: old prose may
be deliberately retained and annotated rather than deleted.

Derive the current HTTP surface from the running service when available:

```bash
curl -fsS "$CHRONOS_BASE/openapi.json" | .venv/bin/python -m json.tool
```

`CHRONOS_BASE` must be the operator-confirmed endpoint for the process under
investigation. Do not assume a port or working directory. FastAPI serves its
generated schema at the configured OpenAPI URL; verify Chronos's configuration
and app factory rather than assuming the framework default. Official reference:
<https://fastapi.tiangolo.com/tutorial/metadata/#openapi-url>.

For an offline checkout, derive decorators and inclusions directly:

```bash
rg -n '@(?:router|session_router)\.(?:get|post|put|patch|delete)\(' src/chronos/api/routes
rg -n 'include_router|FastAPI\(' src/chronos/api/main.py
```

Derive current refusal and journal vocabularies instead of copying a table:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from chronos.orders.submission import SubmissionRefusalCode
from chronos.supervisor.handoff import HandoffDisposition
from chronos.supervisor.loop import CycleStage

for enum in (SubmissionRefusalCode, HandoffDisposition, CycleStage):
    print(enum.__name__, *(member.value for member in enum), sep="\n  ")
PY
```

Then read the branch that emitted the observed value:

```bash
rg -n 'class SubmissionRefusalCode|def submit|_refuse\(|evaluate_live_gates|submission_guard' src/chronos/orders/submission.py
rg -n 'class HandoffDisposition|COUNTS_ACTIVITY_ATTEMPT|REQUIRES_OWNER_ALERT|def classify' src/chronos/supervisor/handoff.py
rg -n '_PROVABLY_NOT_SENT|classify_submission_outcome|def order_plane_handoff' src/chronos/api/autonomy_wiring.py
```

## 3. Refused or rejected order: walk receipts, not guesses

Start with the existing `SubmissionOutcome`. Its `refusal` chooses the source
branch; its `detail` should identify the failed evidence or boundary. Locate the
code in `src/chronos/orders/submission.py`, then trace only the named dependency.
Do not begin by reading every gate.

Use the identifiers returned with the original proposal or submit response:

1. Read the risk decision and per-check results. A failed or unknown check is the
   first causal receipt; a later submission refusal is often only its summary.
2. Read the order lifecycle events in sequence. Distinguish a pre-wire refusal,
   a persisted submission-unknown state, a venue rejection, and an active order.
3. Read health and reconciliation generation around the same timestamp.
4. Read owner alerts and structured logs for the exact operation or cycle ID.
5. Confirm the relevant setting from `src/chronos/config/settings.py` and the
   running process; do not infer it from `.env.example`.

For a positively identified local SQLite artifact, query it through a read-only
URI. This example reads the per-check risk receipt without opening a write path:

```bash
.venv/bin/python - '<db-path>' '<risk-decision-id>' <<'PY'
import sqlite3
import sys
from pathlib import Path

path = Path(sys.argv[1]).resolve().as_posix()
with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
    rows = db.execute(
        "SELECT sequence, check_name, status, detail "
        "FROM risk_check_results WHERE decision_id=? ORDER BY sequence",
        (sys.argv[2],),
    )
    for row in rows:
        print(row)
PY
```

Never point the application at a debugger URI, never edit a lease or scope row,
and never treat an absent row as proof that nothing reached a venue.

### The handoff result is typed now

The app-plane seam in `src/chronos/api/autonomy_wiring.py` translates the order
plane into four supervisor-owned outcomes:

- `SUBMITTED`: the venue acknowledged an active lifecycle;
- `REFUSED_NOT_SENT`: source evidence proves nothing reached the wire;
- `SENT_AMBIGUOUS`: bytes may have left and broker reconciliation owns truth;
- `REJECTED_AFTER_SEND`: the venue saw the order and answered non-active.

COMPLETE means a confirmed working, partially filled, or filled order; it no
longer means merely that a callback returned without raising. Verify the mapping
in `src/chronos/supervisor/handoff.py` and `src/chronos/supervisor/loop.py`, and
the order-plane translation in `src/chronos/api/autonomy_wiring.py`. Still
cross-check journal, order events, and broker truth for a single timeline; a
journal stage is not a substitute for the order lifecycle.

An untyped answer and an exception whose wire effect is unknown fail closed as
ambiguous. Do not reinterpret either as a clean refusal to make counters or
alerts look better.

## 4. Everything blocks or evidence is ambiguous

Separate two hypotheses:

- The control evaluated real evidence and correctly refused.
- The control never received evidence, so fail-closed behavior hides a broken
  supplier.

The discriminating experiment must exercise the complete supplier-to-control
path and make both a positive and negative outcome observable. A unit test that
calls only the final predicate does not prove production wiring.

Useful read-only checks:

```bash
rg -n 'exercised|firing|UNKNOWN|AMBIGUOUS' tests/safety tests/integration
rg -n 'risk_check_results|order_events|reconciliation_runs' src/chronos/persistence
rg -n 'periodic_reconciliation_|reconciliation_status|reconciliation_generation' src/chronos tests
```

For deployment evidence, group historical per-check outcomes in the identified
read-only database. A control that has only ever returned one state deserves
investigation in both directions: always-unknown may mean starvation, while
always-pass is the dangerous permissive shape.

**Periodic reconciliation exists.** Its current task and cadence selection live
in `src/chronos/api/reconciliation_loop.py`; readiness expiration and submission
consumption live in the order readiness source. A reconciliation refusal can
therefore mean stale/consumed evidence, a failed refresh, an in-flight skip, or a
connection-generation change. Discriminate with the health response, structured
`periodic_reconciliation_*` log events, readiness source, and focused tests. Do
not manually refresh it from the debugging lane.

## 5. Autonomy is inert, stopped, or produces no decisions

Read `terminal/system`, mandate, queue, journal, and alert views through the
already-authenticated operator surface. Then classify the first absent link:

1. mandate not configured, invalid, wrong-account, expired, or revoked;
2. runtime absent or stopped after failures;
3. proposer not running or forwarding disabled;
4. proposals refused by ingress, identity, evidence, admission, sizing, compile,
   or handoff;
5. proposal accepted but downstream order-plane result refused or ambiguous.

**The model worker exists.** It is the top-level `worker/` package, deliberately
outside the Chronos wheel and broker-holding process, with entry point
`python -m worker`. It **ships inert**: determine its current provider, forwarding,
budget, allowlist, policy, and loopback requirements from `worker/config.py`,
`worker/__main__.py`, `docs/model_worker.md`, and the worker tests. Do not start it
as a diagnostic step; doing so can contact a model service and, when explicitly
configured, forward proposals.

Preserve both sides of the isolation boundary: the worker imports no `chronos`,
and `src/chronos` imports no `worker`. A convenient shared helper across that seam
is a design change, not a debugging shortcut. The current accepted decision is
indexed in `DECISIONS.md`; the structural proof lives in the model-worker safety
tests.

## 6. Backend is read-only or writer-demoted

Start with the unauthenticated health read and the identified local lease record:

```bash
curl -fsS "$CHRONOS_BASE/health" | .venv/bin/python -m json.tool
```

Read-only is an operating state, not proof that the backend is dead. Determine
whether it booted without the writer lease or demoted after renewal failure by
reading `src/chronos/api/main.py`, `src/chronos/api/dependencies.py`, the lease
row through a read-only database URI, and logs from the same process lifetime.
Do not delete or rewrite a lease. Recovery belongs to `chronos-run-and-operate`
after excluding a second writer.

Authority-removing endpoints deliberately remain reachable after demotion. The
operator terminal currently exposes `POST /terminal/live/kill` and
`POST /terminal/live/disarm`; derive the complete current mutation set from
`src/chronos/api/routes/terminal.py` and the client in
`src/chronos/terminal/static/terminal.js`. These route names explain capability;
they are not instructions to invoke either action during diagnosis. Authority-
granting actions follow a different credential and writer policy.

## 7. Terminal failures

Discriminate in this order:

- connection refused: process or routing problem;
- `/health` responds but terminal reads fail: session, credential, or route
  problem;
- HTTP authentication failure after restart: inspect the current session/token
  implementation and process start, not a remembered TTL;
- read-only health with available panels: expected separation between reads and
  writer-gated mutations;
- stale/refused chart data: inspect response `source`, quality, stale/refusal
  fields, pacing logs, and current pacing code;
- missing control: compare running `/openapi.json`, route source, packaged static
  assets, and browser network trace at the same commit.

Do not widen cookie scope, bypass a writer dependency, or add a control while
debugging an authentication symptom.

## 8. Test or build failure

Derive the environment and gate from the current tree:

```bash
rg -n 'requires-python|dependencies|markers|addopts' pyproject.toml
rg -n '^gates:|^test:|^lint:|^format-check:|^type:|^type-worker:|^release-gate:' Makefile
rg -n '^\s+run:' .github/workflows/ci.yml
.venv/bin/python -m pytest -q <focused-test-path>
make gates
```

Never copy a passed, skipped, collected, source-file, warning, or migration count
from documentation. Record what the exact candidate measured. If local gates and
CI disagree, `.github/workflows/ci.yml` is authoritative and the mismatch is a
finding.

Common discriminators are current Python compatibility, `.venv` existence,
hash-locked dependencies, ambient live-capable settings triggering safety
fixtures, strict marker/config registration, Node availability for client tests,
and import shadowing. Read the failing fixture or configuration before changing
it; a safety tripwire failure is not permission to weaken the tripwire.

## 9. Database, migration, or durable-state oddity

Derive source and migration identities independently:

```bash
rg -n '^SCHEMA_VERSION' src/chronos/persistence/database.py
rg -n '^script_location' alembic.ini
PYTHONPATH=src .venv/bin/alembic heads
```

Alembic documents `heads` as the available heads in the script directory:
<https://alembic.sqlalchemy.org/en/latest/api/commands.html#alembic.command.heads>.
It does not prove that a particular database is current. Compare it with the
code constant, release-artifact verifier, and the identified database's version
rows through a read-only connection. Do not repair, stamp, upgrade, remove
sidecars, or rewrite scope from this skill; back up and route the operation.

Use `chronos-diagnostics` to interpret missing-file safety defaults instead of
caching them here. If an audit or registry chain fails verification, preserve the
artifact and treat it as an incident. A hash chain is evidence of consistency,
not permission to rewrite history until the verifier turns green.

## 10. Research refusal or zero selection

`INSUFFICIENT_EVIDENCE`, no-trade, and zero-selection outcomes can be successful
fail-closed results. Read the exact campaign constitution, frozen thresholds,
trial registry, evidence artifact, and rejection findings before calling one a
bug. Never tune a threshold after observing the result. Route statistical and
holdout questions to `chronos-research-methodology`; owner inputs and holdout
unlocks remain owner gates in `docs/VISION_COMPLETION_PLAN.md`.

## 11. Exit criteria and escalation

A diagnosis is complete only when it has:

- one observed symptom tied to an exact commit and process lifetime;
- the source branch or control that emitted it;
- the evidence that arrived, was absent, stale, or inconsistent;
- a read-only discriminating experiment whose alternate result would disprove
  the chosen cause;
- the correct disposition: working-as-designed, code defect, operational action,
  owner decision, or unresolved;
- named residual uncertainty and no unrecorded side effect.

Stop and route rather than fix inline when the likely change touches authority,
gate order or meaning, fail-closed defaults, frozen thresholds, broker access,
credentials, live state, durable data, migrations, worker isolation, or an owner
gate. A control that blocks everything and a control that passes everything both
need end-to-end evidence before anyone edits the predicate.

## 12. Maintenance contract

This skill owns the debugging process, not a snapshot of Chronos. Do not add
ports, line numbers, test totals, schema or migration numbers, TTL values, route
counts, current ADR maxima, or claims that a planned component does not exist.
Point to the live source and give a read-only derivation command.

After changing this skill:

```bash
.venv/bin/python -m pytest -q tests/unit/test_debugging_playbook_contract.py
.venv/bin/python .claude/skills/chronos-diagnostics/scripts/doc_drift_check.py
make gates
```

Use `chronos-run-and-operate` for operating actions, `chronos-validation-and-qa`
for proof design, `chronos-ibkr-boundary` for adapter semantics,
`chronos-research-methodology` for statistical evidence, and
`chronos-change-control` for every proposed change.
