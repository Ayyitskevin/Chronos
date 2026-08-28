---
name: chronos-diagnostics
description: >
  Read-only diagnostic scripts for Chronos — measure instead of eyeball. Load
  this skill whenever you need to "check system state", answer "is it safe to
  restart", "what state is the system in", run a "health check", "diagnose"
  something before touching it, inventory safety files, check documentation
  drift, do a session-start check, or verify state around a backup restore. Also
  load it before live-adjacent code work and after documentation edits. NOT for
  fixing findings (chronos-run-and-operate, chronos-docs-map,
  chronos-build-and-env) or deep debugging (chronos-debugging-playbook).
---

# chronos-diagnostics — measure, don't eyeball

Chronos state has non-obvious defaults: a missing live kill-switch file means
the emergency stop is disarmed, while a missing platform halt file means halted.
These scripts produce labeled, exit-coded observations rather than relying on
memory or a prose snapshot.

All scripts are read-only. They read files, open SQLite through read-only URIs,
run git with optional locks disabled, and invoke pytest collection with its cache
plugin disabled and bytecode writing off. They never contact a broker or network
service.

## 1. Scripts and execution

| Script | Question |
|---|---|
| `scripts/state_inventory.py` | What is the durable safety state of this checkout? |
| `scripts/doc_drift_check.py` | Which claims in the dated contradiction ledger still match? |
| `scripts/env_check.py` | Can this machine build and test the project? |

Run from the repository root:

```bash
.venv/bin/python .claude/skills/chronos-diagnostics/scripts/state_inventory.py
.venv/bin/python .claude/skills/chronos-diagnostics/scripts/doc_drift_check.py
.venv/bin/python .claude/skills/chronos-diagnostics/scripts/env_check.py
```

The implementations use the standard library and do not import `chronos`, so a
compatible system `python3` can run them before the project environment exists.

For `state_inventory.py` and `env_check.py`, exit codes are `0` for clean, `1`
for findings, and `2` when the script cannot run. For `doc_drift_check.py`, exit
`1` specifically means a `PRESENT` or `FILE-MISSING` ledger rule; its collection
cross-check warnings are visible context and do not change that ledger exit.
Optional environment variables:

- `CHRONOS_REPO_ROOT` overrides repository-root discovery.
- `CHRONOS_DIAG_VENV_PYTHON` selects the Python used for the optional
  `pytest --collect-only` cross-check.

The two collection-reporting scripts parse the first explicitly current summary
in `docs/TEST_RESULTS.md` through `scripts/validation_snapshot.py`. They never use
a hard-coded test-count fallback. Missing, malformed, or incoherent evidence is a
visible warning, not permission to compare against an older count.

## 2. When to run each observer

| Moment | Run | Reason |
|---|---|---|
| Session start | all three | Establish checkout and environment truth before trusting a handoff |
| Before and after restore | `state_inventory.py` | Compare durable safety artifacts; a restore can change missing-file semantics |
| Before live-adjacent work | `state_inventory.py` | Observe stop, mandate, database, environment, and git state first |
| Before answering whether restart is safe | `state_inventory.py` | Durable mandates can survive while process-local arming and sessions do not |
| After documentation edits | `doc_drift_check.py` | See which ledger entries became `CORRECTED` or `ABSENT` |
| Fresh machine or build trouble | `env_check.py` | Detect interpreter, environment, lockfile, Node, and import problems |

## 3. Interpret `state_inventory.py`

Labels are `[OK]`, `[INFO]`, `[WARN]`, `[CRIT]`, and `[SKIP]`. A warning or
critical result makes the command non-zero.

| Section | Important interpretation | Route a change to |
|---|---|---|
| Live kill switch | Missing means **DISENGAGED**: this stop contributes no protection | `chronos-run-and-operate` |
| Platform halt | Missing means **HALTED / NEVER_ARMED**, the safe default | `chronos-run-and-operate` |
| Autonomy mandate | Unset means inert; a valid configured file activates on boot and must be treated like a credential | `chronos-autonomy-and-mandates` |
| Durable stores | Fresh-checkout absence can be normal; mismatched schema or a registry ledger without its anchor is a finding | `chronos-run-and-operate`, `chronos-research-methodology` |
| Migrations | The migration-derived schema and code constant must agree; the script derives both live | `chronos-build-and-env` |
| Environment | Any live-capable value deserves deliberate review; tests reject live-capable settings by design | `chronos-config-and-flags` |
| Git | Pre-existing changes may belong to another lane | `chronos-change-control` |
| Test collection | A count below the coherent current `docs/TEST_RESULTS.md` snapshot may mean tests disappeared | `chronos-validation-and-qa` |

The halt mechanisms are deliberately asymmetric. `python -m chronos.cli halt`
does not stop the live order plane; its live kill switch is separate.

## 4. Interpret `doc_drift_check.py`

Each stable rule ID reports one verdict:

- `PRESENT`: stale text still matches without an accepted correction;
- `CORRECTED`: every match is beside a dated in-place correction;
- `ABSENT`: the pattern no longer matches;
- `FILE-MISSING`: the target disappeared and the rule needs maintenance.

This repository preserves some correction history, so retaining struck-through
wording can be correct. Rules whose resolution requires an owner decision use
`annotation_is_not_a_fix=True`; a nearby note cannot convert them to corrected.

The `RULES` tuple is a dated contradiction ledger, not a current-count promise.
Append a stable rule only after confirming a contradiction against the document
authority map. Do not delete or rewrite rules merely to improve the summary.
Route findings through `chronos-docs-map` and `chronos-change-control`; never
change executable behavior just to make stale prose true.

## 5. Interpret `env_check.py`

It derives environment facts live rather than asking this skill to cache them:

- interpreter and project-venv compatibility;
- required tool imports and the local `chronos` import origin;
- lockfile syntax, pins, and hash shape;
- Node availability for terminal-client safety tests;
- `PYTHONPATH` shadowing hazards;
- `.env` presence for cross-reference with the state inventory.

Use `chronos-build-and-env` for repairs. Diagnostics observe; they do not install,
regenerate, or mutate the environment.

## 6. Limits

1. A clean report describes observable state at one instant. It is not promotion,
   real-gateway, operating, or economic proof.
2. The scripts read durable state, not a running process's arming, session, lease,
   or reconciliation state. Use authenticated health/terminal surfaces for that.
3. Config resolution is a textual mirror so diagnostics stay import-free. If source
   syntax drifts, `state_inventory.py` warns before using its path fallbacks.
4. `references/expected-output-2026-08-02.md` is a historical report-shape fixture.
   Its values are not current baselines. Compare labels and structure only.
5. A documented validation snapshot remains a dated claim. Run `make gates` on the
   actual candidate before declaring it verified.

## 7. Maintenance contract

Do not put volatile counts in this skill or either diagnostic. Test comparison
comes from `docs/TEST_RESULTS.md`; migration, schema, rule, and lockfile facts are
derived from live files. If the current validation summary format changes, update
`validation_snapshot.py` and its unit tests in the same change.

After changing a diagnostic:

```bash
.venv/bin/pytest -q tests/unit/test_diagnostics_validation_snapshot.py
.venv/bin/ruff check .claude/skills/chronos-diagnostics/scripts
.venv/bin/ruff format --check .claude/skills/chronos-diagnostics/scripts
```

Then run all three observers and `make gates`. Never add a write path, credential
load, network call, or broker connection to these scripts.

## 8. Route elsewhere when needed

- State mutation or restore: `chronos-run-and-operate`.
- Documentation authority/corrections: `chronos-docs-map`.
- Environment repair: `chronos-build-and-env`.
- Deep refusal/failure triage: `chronos-debugging-playbook`.
- Test design or evidence judgment: `chronos-validation-and-qa`.
- Any safety-mechanism or authority change: `chronos-change-control`.
