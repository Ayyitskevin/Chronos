---
name: chronos-diagnostics
description: >
  Read-only diagnostic scripts for Chronos — measure instead of eyeball. Load
  this skill whenever you need to "check system state", answer "is it safe to
  restart", "what state is the system in", run a "health check", "diagnose"
  something before touching it, take an "inventory" of safety files, check
  "doc drift" / stale documentation, do a "session-start check", or verify
  state "before restore" / after restoring a backup. Also load it at the start
  of any session that will touch live-adjacent code (orders, kill switch,
  mandate, .env), and after any documentation edit to confirm which known-stale
  claims are fixed. Ships three runnable scripts: state_inventory.py (safety-
  state snapshot: kill switch, halt, mandate, DBs, migrations, .env, git),
  doc_drift_check.py (20-rule stale-claim detector from the contradiction
  ledger), env_check.py (interpreter/venv/lockfile/node sanity). NOT for
  fixing what they find (chronos-run-and-operate, chronos-docs-map,
  chronos-build-and-env) or deep debugging (chronos-debugging-playbook).
---

# chronos-diagnostics — measure, don't eyeball

Chronos state lives in files with NON-OBVIOUS defaults (a missing kill-switch
file means the emergency stop is DISARMED; a missing halt file means SAFE), and
its documentation is known to lag the code. Guessing state from memory or from
docs has burned this project repeatedly. These three scripts turn "I think it's
fine" into a labeled, exit-coded report.

All three are STRICTLY read-only observers: they open files read-only, open
SQLite with `mode=ro` URIs, run git with `--no-optional-locks`, invoke pytest
(optionally) with `-p no:cacheprovider` + `PYTHONDONTWRITEBYTECODE=1` so no
cache dirs appear in the repo, and never touch the network or any broker.

## The scripts

| Script | Question it answers | Typical runtime |
|---|---|---|
| `scripts/state_inventory.py` | What is the safety state of this checkout right now? | <1 s (+~15 s with live test collection) |
| `scripts/doc_drift_check.py` | Which known-stale doc claims (2026-08-02 ledger) are still present? | <1 s |
| `scripts/env_check.py` | Can this machine actually build/test the project? | ~2 s |

## How to run

Repo convention (README Setup uses the project venv):

```bash
cd /home/user/Chronos
.venv/bin/python .claude/skills/chronos-diagnostics/scripts/state_inventory.py
.venv/bin/python .claude/skills/chronos-diagnostics/scripts/doc_drift_check.py
.venv/bin/python .claude/skills/chronos-diagnostics/scripts/env_check.py
```

The scripts are stdlib-only and deliberately do NOT import chronos, so they
also run with any `python3` (3.11+ verified 2026-08-02) even when no venv
exists yet:

```bash
python3 .claude/skills/chronos-diagnostics/scripts/state_inventory.py
```

Exit codes (all three): `0` = clean, `1` = findings (WARN/CRIT, or PRESENT
stale claims), `2` = could not run (repo root not found). Optional env vars:

- `CHRONOS_REPO_ROOT` — override repo-root autodetection (scripts locate the
  root from their own path and require `AGENTS.md` there).
- `CHRONOS_DIAG_VENV_PYTHON` — a Python 3.12 venv python to use for the live
  `pytest --collect-only` cross-check (default: `<repo>/.venv/bin/python`;
  skipped with a labeled SKIP line when absent).

## When to run them

| Moment | Run | Why |
|---|---|---|
| Session start (any Chronos work) | all three | Know the checkout before believing anything |
| Before AND after a backup restore | `state_inventory.py` | The restore docs overstate safety: a restore that omits `data/live_kill_switch.json` boots the live plane DISENGAGED. Compare before/after reports |
| Before any live-adjacent work (orders, kill switch, mandate, `.env`, arming) | `state_inventory.py` | Confirms what is armed/disarmed/present before you change it |
| Before answering "is it safe to restart?" | `state_inventory.py` | Restart clears arming + terminal sessions (process memory) but a set `AUTONOMY_MANDATE_FILE` auto-activates on boot — the report shows both |
| After ANY documentation edit | `doc_drift_check.py` | Confirms which ledger entries your edit actually fixed (ABSENT) vs left (PRESENT) |
| Fresh machine / build trouble / before running `make` anything | `env_check.py` | Catches the 3.11-default-python trap and the missing `.venv` before they waste an hour |

## Interpretation guide — state_inventory.py

Line labels: `[OK]` expected-safe, `[INFO]` neutral fact, `[NOT-PRESENT]`
reported inside INFO lines, `[WARN]`/`[CRIT]` findings (non-zero exit),
`[SKIP]` optional section unavailable.

| Report section | GOOD looks like | Warning means | Fix lives in |
|---|---|---|---|
| Live kill switch | File present + `ENGAGED` (deliberate stop), or you consciously accept DISENGAGED | `NOT PRESENT` => **the live-plane emergency stop is DISARMED** (missing file = DISENGAGED, `orders/kill_switch.py:83-85`). Loud by design — this is the single most-misunderstood default in the repo | chronos-run-and-operate (engage via `POST /live/kill`) |
| Platform halt | `NOT PRESENT => HALTED (NEVER_ARMED)` is the SAFE default | `REARMED (not halted)` = the deterministic platform will generate work when driven | chronos-run-and-operate (`python -m chronos.cli halt`) |
| Autonomy mandate | `AUTONOMY_MANDATE_FILE unset => INERT` | SET = auto-activates on every backend boot (ADR-0017); file missing/unreadable = boots inert with CRITICAL alert. Treat the file like a credential | chronos-run-and-operate (revoke), chronos-autonomy-and-mandates (semantics) |
| Durable stores | Fresh checkout: everything `NOT PRESENT` is normal. Populated box: main DB present, `schema_version = 7`, scope bound | `schema_version != 7` = backend refuses the DB at boot (CRIT); registry ledger present WITHOUT its head anchor = possible tail truncation (un-burned holdout) — run `python -m chronos.cli registry verify` | chronos-run-and-operate; chronos-research-methodology (registry/holdout meaning) |
| Migrations vs SCHEMA_VERSION | `N revisions + baseline => vN+1` consistent (6 => v7 as of 2026-08-02) | MISMATCH = a migration or schema bump landed without its counterpart; do not run `alembic upgrade` until understood | chronos-build-and-env |
| .env live-capability | `.env NOT PRESENT` or only safe values (`demo`, `paper`, `false`, empty) | Any live-capable var set (BROKER_MODE!=demo, ALLOW_ORDER_TRANSMIT, ALLOW_LIVE_TRADING, IB_ACCOUNT_ID, AUTONOMY_MANDATE_FILE, ...) — also note: a live-capable `.env` makes the whole test suite fail by design (conftest tripwires) | chronos-config-and-flags (meanings), chronos-run-and-operate (procedures) |
| Git state | Clean tree on a work branch | Dirty tree before you started = someone else's uncommitted state; know whose | chronos-change-control (branch discipline) |
| Test collection | `2490` collected (2026-08-02 baseline) | Count DROPPED below baseline = tests disappeared; a green run proves less than you think | chronos-validation-and-qa |

The kill-switch/halt asymmetry in one line: **two stop mechanisms, opposite
missing-file defaults** — platform halt missing = HALTED (safe); live
kill-switch missing = DISENGAGED (disarmed). `python -m chronos.cli halt` does
NOT stop the live order plane; `POST /live/kill` does.

## Interpretation guide — doc_drift_check.py

Verdicts per rule: `PRESENT` = the stale claim is still in the doc (finding);
`ABSENT` = fixed after 2026-08-02; `FILE-MISSING` = the doc is gone (retire or
repoint the rule). On 2026-08-02 all 20 rules were PRESENT — that is the
honest baseline, not an error.

The rules ledger (`RULES` tuple at the top of the script) is data: each rule
carries id, file, regex, why-stale, and fixed-when. Extend it by APPENDING a
rule when a new contradiction is confirmed (source of truth for contradictions:
the chronos-docs-map skill). Never delete a rule because it annoys you; delete
only when the doc is fixed AND chronos-docs-map's ledger is updated to match.

Rule families as of 2026-08-02:

- `*-AUTONOMY-M1` — "wired into nothing" claims frozen at Milestone 1.
- `TESTRESULTS-STALE-COUNT` — the "current" 1901 count (reality baseline:
  2490 collected / 2489 passed, 1 skipped; the script prints the LIVE count
  when a venv is available — always prefer the live number).
- `*-3K` — the ~USD 3,000 capital premise (verified snapshot ~USD 110; a
  LIVE, unresolved owner decision — flag, never quietly assume either number).
- `IBKRINT-ONLY-PATH`, `IBKRRUN-NO-SERVICE`, `SECURITY-*`,
  `DEPLOY-SERVICE-DENIAL` — capability claims falsified by M2-M7 code.
- `MANDATE-ARMING-*`, `ADR17-NO-RITUAL` — the mandate-replaces-arming prose
  vs code that unconditionally requires a current arm (submission.py:441).
  Fixing THIS one is not a doc edit: it is an open owner-gated Phase-1 defect.
- `ADR0012/0014/0015-PROPOSED` — ADR status lines never flipped despite
  shipped implementations.

Route PRESENT findings to chronos-docs-map (which doc to trust, house style
for corrections) under chronos-change-control rules. Do NOT bulk-edit docs
straight from this output, and NEVER "fix" a contradiction by changing code to
match prose — for authority-model rules the code is the fact and the change is
owner-gated.

## Interpretation guide — env_check.py

| Section | GOOD | Warning means | Fix lives in |
|---|---|---|---|
| Interpreters | `python3.12` on PATH | bare `python3` is 3.11 => `python3 -m venv .venv` silently builds an unusable venv | chronos-build-and-env |
| Project venv | `.venv` present, 3.12, pytest/ruff/mypy/alembic all importable, chronos importable | `.venv NOT PRESENT` => every `make` target fails (all hard-code `.venv/bin/...`); missing tool => reinstall from lock | chronos-build-and-env |
| Lockfile | present, uv-compile header, 76 pins / 1387 hashes (2026-08-02 shape), well-formed digests | missing/odd shape => no reproducible install; CI installs `--require-hashes` | chronos-build-and-env |
| Node | present (v22 here) | absent => terminal-client JS safety tests skip locally but HARD-FAIL in CI | chronos-build-and-env |
| PYTHONPATH | unset, or exactly `<repo>/src` | nonexistent entries, or another `chronos` package shadowing the repo | chronos-build-and-env |
| .env | reported for cross-reference | details live in state_inventory.py | chronos-config-and-flags |

## Limits — read these before trusting a report

1. **Observers, not gates.** A clean report means the observable file state
   was clean at that instant. It is NOT evidence for any promotion gate, NOT
   real-gateway evidence (no real IBKR gateway has ever been connected in this
   project's history — see chronos-real-gateway-campaign), and NOT proof any
   control fires. MITIGATED != CLOSED.
2. **They read files, not processes.** A running backend's in-memory state
   (arming, terminal sessions, writer lease held, reconciliation latch) is
   invisible here. For live process state use `GET /health` and the terminal
   panels (chronos-run-and-operate).
3. **Path resolution mirrors, not executes, the config machinery.** Defaults
   are parsed textually from `src/chronos/config/settings.py`, then overridden
   by process env and a textual `.env` parse (documented choice: no chronos
   import, no .env loading, so the scripts run anywhere and can never trip the
   settings validators). If settings.py's field syntax changes, the scripts
   fall back to hard-coded 2026-08-02 defaults and print a WARN — update them.
4. **Scripts drift too.** Baselines (2490 tests, 76 pins, 6 revisions, v7,
   the 20 doc rules) are 2026-08-02 facts. Re-validate after any repo change:
   run all three and compare the report SHAPE to
   `references/expected-output-2026-08-02.md` (values may differ; structure,
   verdict logic, and parse success must not).

## When NOT to use this skill

- Fixing what a script found: kill-switch/halt/mandate/restore procedures →
  **chronos-run-and-operate**; doc corrections and which-doc-to-trust →
  **chronos-docs-map**; venv/lockfile/interpreter repair →
  **chronos-build-and-env**; config meanings and safety classes →
  **chronos-config-and-flags**.
- Deep debugging of a failure or refusal (why did a gate block, UNKNOWN vs
  zero, lease demotion) → **chronos-debugging-playbook**.
- Judging test evidence or writing tests → **chronos-validation-and-qa**.
- Anything that would CHANGE state as part of "diagnosis" — not this skill,
  and for safety mechanisms not any skill without an owner gate
  (**chronos-change-control**).

## Provenance and maintenance

Compiled 2026-08-02 against branch `claude/chronos-skills-library-bfbj29`.
Volatile facts and their re-verification commands (all read-only):

| Fact (2026-08-02) | Re-verify with |
|---|---|
| Missing kill-switch file reads DISENGAGED | `sed -n '83,92p' src/chronos/orders/kill_switch.py` |
| Missing halt file reads HALTED | `sed -n '100,112p' src/chronos/control/halt.py` |
| `SCHEMA_VERSION = 7`; 6 migration revisions | `grep -n "^SCHEMA_VERSION" src/chronos/persistence/database.py; ls src/chronos/persistence/migrations/versions/` |
| Settings path defaults (kill switch, DB URL, alerts, token) | `grep -n 'Path("data/\|sqlite:///' src/chronos/config/settings.py` |
| Mandate auto-activation + `persistent-mandate:<digest16>` | `grep -n "persistent-mandate" src/chronos/api/autonomy_wiring.py` |
| Test baseline 2490 collected / 2489 passed, 1 skipped | `.venv/bin/python -m pytest --collect-only -q -p no:cacheprovider` (and CHANGELOG.md top entry) |
| Lockfile shape 76 pins / 1387 hashes | `grep -cE '^[A-Za-z0-9_.-]+==' requirements-dev.lock; grep -c -- '--hash=sha256:' requirements-dev.lock` |
| Each doc-drift rule's target text | run `doc_drift_check.py` — the rules ARE the re-verification; cross-check new/fixed entries against chronos-docs-map |
| Scripts still behave as captured | run all three; compare shape to `references/expected-output-2026-08-02.md` |

Maintenance duties for future sessions: (a) when a doc in the ledger is fixed,
confirm the rule flips to ABSENT and update chronos-docs-map; (b) when a new
contradiction is confirmed, append a dated Rule; (c) when baselines legitimately
move (new tests, new migration, new lock), update the constants in the scripts
AND recapture `references/expected-output-<date>.md`; (d) never add a
write-path or network call to these scripts — read-only is their contract.
