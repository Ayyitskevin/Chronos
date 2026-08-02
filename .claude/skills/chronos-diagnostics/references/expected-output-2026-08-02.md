# Expected-shape outputs — captured 2026-08-02

These are the ACTUAL outputs of the three diagnostic scripts run against the
repo on 2026-08-02 (fresh checkout state: no `.env`, no `data/chronos.db`, no
`.venv`, kill-switch file absent). Use them to re-validate the scripts after
repo changes: run each script, diff the SHAPE (sections, labels, verdicts)
against these captures, and investigate any structural difference. Exact
values (HEAD sha, dirty counts, interpreter paths, live counts) are volatile
and WILL differ — that is not drift; missing sections, changed verdicts for
unchanged repo state, or parse warnings ARE drift.

Both interpreters produced identical reports except the `python:` banner line:
bare `python3` (3.11.15) and a Python 3.12 venv. The 3.12-venv run below had
`CHRONOS_DIAG_VENV_PYTHON` set, which enables the live pytest cross-check.

---

## 1. state_inventory.py (3.12 venv, live collection enabled) — exit 1

```
CHRONOS STATE INVENTORY (read-only) — repo: /home/user/Chronos
python: 3.12.3

== Live kill switch (live-Wheel order plane) [settings.py default: data/live_kill_switch.json] ==
[WARN] live kill-switch file NOT PRESENT: /home/user/Chronos/data/live_kill_switch.json
       >>> MISSING FILE MEANS DISENGAGED: the live-plane emergency stop is
       >>> DISARMED right now (orders/kill_switch.py:83-85 returns
       >>> engaged=False on FileNotFoundError). Other gates still apply,
       >>> but the kill switch itself contributes NOTHING in this state.
       To engage: POST /live/kill on a running backend (token-only, works
       even read-only). See sibling skill chronos-run-and-operate.

== Platform halt (deterministic strategy platform) ==
[  OK] platform halt file NOT PRESENT: /home/user/Chronos/data/platform_halt.json => HALTED (NEVER_ARMED)
       Missing file is the SAFE default for this plane (halt.py:102-109).
       Note the asymmetry with the live kill switch above: TWO stop
       mechanisms, OPPOSITE missing-file defaults.

== Autonomy mandate (ADR-0017 standing grant) [unset] ==
[  OK] AUTONOMY_MANDATE_FILE unset => autonomy runtime is INERT
       No mandate file, no model authority. This is the safe default.

== Durable stores ==
[INFO] main DB NOT PRESENT: /home/user/Chronos/data/chronos.db  [settings.py default]
       Fresh-checkout state: no wheel ledger, no order pipeline rows,
       no autonomy durable state, no writer lease. Nothing to trade
       with — and no history either (deny-by-default protects you).
[INFO] platform ledger NOT PRESENT: /home/user/Chronos/data/platform_ledger.db (no platform runs yet)
[INFO] platform audit log NOT PRESENT: /home/user/Chronos/data/platform_audit.jsonl
[INFO] registry ledger NOT PRESENT: /home/user/Chronos/research/registry/registry.jsonl
       Expected on a fresh checkout — the registry ships EMPTY; it is
       created by the first registered research run.
[INFO] owner alerts sink: NOT PRESENT (/home/user/Chronos/data/owner_alerts.jsonl)
[INFO] session drawdown baseline: NOT PRESENT (/home/user/Chronos/data/session_baseline.json)
[INFO] backend API token: NOT PRESENT (/home/user/Chronos/data/backend_api_token)

== Alembic migrations vs code SCHEMA_VERSION ==
[INFO] 6 migration revisions: 0001_v2_baseline.py..0006_proposal_queue.py
[INFO] code SCHEMA_VERSION = 7
[  OK] consistent: 6 revisions + baseline => v7

== .env / environment live-capability check (textual, never loaded) ==
[  OK] .env NOT PRESENT — settings.py demo-safe defaults apply

== Git state (read-only) ==
[INFO] branch=claude/chronos-skills-library-bfbj29 HEAD=e9a37dd
[  OK] working tree clean

== Test collection (optional; needs a Python 3.12 venv) ==
[  OK] pytest collected 2490 tests (baseline 2026-08-02: 2490)

== SUMMARY: 1 finding(s) (WARN/CRIT lines) ==
Exit 0 = clean, 1 = findings, 2 = could not run.
A clean report is an OBSERVATION, not gateway evidence or a promotion gate.
```

Without a venv the last section reads instead:

```
== Test collection (optional; needs a Python 3.12 venv) ==
[SKIP] no venv python at /home/user/Chronos/.venv/bin/python — skipping pytest collection
       Baseline 2026-08-02: 2490 collected / 2489 passed, 1 skipped.
       Set CHRONOS_DIAG_VENV_PYTHON to a 3.12 venv python to enable.
```

## 2. doc_drift_check.py — exit 1, all 20 rules PRESENT on 2026-08-02

Condensed to verdict lines (full output includes why-stale/fixed-when per
PRESENT rule; see the script's RULES ledger):

```
CHRONOS DOC-DRIFT CHECK (read-only) — repo: /home/user/Chronos
20 rules from the 2026-08-02 contradiction ledger

[     PRESENT] ARCH-AUTONOMY-M1  (docs/ARCHITECTURE.md)          line(s) 29
[     PRESENT] HANDOFF-AUTONOMY-M1  (HANDOFF.md)                 line(s) 23
[     PRESENT] TESTRESULTS-STALE-COUNT  (docs/TEST_RESULTS.md)   line(s) 12
[     PRESENT] ASSUMPTIONS-A10-3K  (ASSUMPTIONS.md)              line(s) 28
[     PRESENT] RISKREG-R10-3K  (RISK_REGISTER.md)                line(s) 17
[     PRESENT] GOLIVE-3K  (docs/GO_LIVE_CHECKLIST.md)            line(s) 179
[     PRESENT] HANDOFF-3K  (HANDOFF.md)                          line(s) 121
[     PRESENT] ADR0008-3K  (docs/adr/ADR-0008-...)               line(s) 9, 32
[     PRESENT] IBKRINT-ONLY-PATH  (docs/IBKR_INTEGRATION.md)     line(s) 17
[     PRESENT] IBKRRUN-NO-SERVICE  (docs/IBKR_RUNBOOK.md)        line(s) 9
[     PRESENT] SECURITY-LIVE-RAISE  (docs/SECURITY.md)           line(s) 40
[     PRESENT] SECURITY-NO-AUTH  (docs/SECURITY.md)              line(s) 50
[     PRESENT] DEPLOY-SERVICE-DENIAL  (docs/DEPLOYMENT.md)       line(s) 144
[     PRESENT] MANDATE-ARMING-RUNBOOK  (docs/live_trading_runbook.md) line(s) 21
[     PRESENT] MANDATE-ARMING-GAMEPLAN  (docs/AI_QUANT_GAME_PLAN.md)  line(s) 264
[     PRESENT] MANDATE-ARMING-WHEELPLAN  (docs/LIVE_WHEEL_GAME_PLAN.md) line(s) 132
[     PRESENT] ADR17-NO-RITUAL  (docs/adr/ADR-0017-...)          line(s) 84
[     PRESENT] ADR0012-PROPOSED  (docs/adr/ADR-0012-...)         line(s) 3
[     PRESENT] ADR0014-PROPOSED  (docs/adr/ADR-0014-...)         line(s) 3
[     PRESENT] ADR0015-PROPOSED  (docs/adr/ADR-0015-...)         line(s) 3

== Live test-collection cross-check ==
[INFO] live pytest --collect-only: 2490 tests (baseline: 2490 collected /
       2489 passed, 1 skipped (baseline 2026-08-02, CHANGELOG.md M11))

== SUMMARY: 20 PRESENT (still stale), 0 ABSENT (fixed), 0 FILE-MISSING ==
```

## 3. env_check.py (bare python3.11, this container, no .venv) — exit 1

```
CHRONOS ENV CHECK (read-only) — repo: /home/user/Chronos

== Python interpreters ==
[INFO] project requires-python: >=3.12
[INFO] this script is running on Python 3.11.15 (fine for diagnostics; NOT enough to run the project)
[WARN] python3 -> Python 3.11.15 at /usr/local/bin/python3 — BELOW requires-python
       Known trap: bare `python3 -m venv .venv` builds a 3.11 venv that
       cannot run the project. Use `python3.12 -m venv .venv` explicitly.
[  OK] python3.12 -> Python 3.12.3 at /usr/bin/python3.12

== Project venv (.venv/) ==
[WARN] .venv NOT PRESENT at /home/user/Chronos/.venv
       Every Makefile target hard-codes .venv/bin/... — `make test`,
       `make gates`, `make backend` all fail until it exists. Build it
       per the chronos-build-and-env skill (python3.12 + hash-locked
       install from requirements-dev.lock).

== Dependency lock (requirements-dev.lock) ==
[  OK] lockfile present: 76 pinned packages, 1387 sha256 hashes
[  OK] header shows uv pip compile --generate-hashes provenance
[  OK] all --hash lines are well-formed sha256 digests

== Node (terminal-client JS tests) ==
[  OK] node v22.22.2 at /opt/node22/bin/node

== PYTHONPATH hygiene ==
[  OK] PYTHONPATH unset (fine when chronos is pip-installed in .venv)
       Running tests WITHOUT an editable install needs PYTHONPATH=src
       from the repo root (src layout).

== .env presence (config; details in state_inventory.py) ==
[  OK] .env NOT PRESENT — demo-safe defaults apply; suite tripwires quiet

== SUMMARY: 2 finding(s) (WARN/CRIT lines) ==
```
