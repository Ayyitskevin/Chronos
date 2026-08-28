---
name: chronos-validation-and-qa
description: >
  Load this skill whenever you are about to write a test, judge whether something in Chronos
  is "tested" or "proven", claim that a control works, verify a fix, or interpret the test
  suite. Triggers: "write a test", "is this tested", "how do I prove this", "test suite",
  "CI failing", "pytest failing", "add coverage", "did this control actually work", "what
  counts as evidence", "can I claim this is done", "verify a fix", "revert the fix",
  "structural test", "AST test", "exercised test", "mutation/property/fuzz tests",
  "SAFETY TRIPWIRE failure", "which tests protect X". Home of the claim-evidence
  ladder, the test-suite map, and the house proof patterns. NOT for statistical
  strategy-evidence gates (chronos-research-methodology), environment setup
  (chronos-build-and-env), or what documents may claim (chronos-change-control).
---

# Chronos validation and QA: what counts as evidence

Chronos is a trading system whose costliest historical failures were **controls that
passed tests while structurally unable to fire** (four kernel defects, R-24..R-27 —
history in `chronos-failure-archaeology`). The entire QA culture here exists to prevent a
fifth instance. The rule that governs everything below:

> **Never claim a milestone, control, or fix is "done/working/validated" without naming
> the exact evidence artifact — a specific test file and what it asserts, a real-gateway
> capture, or a promotion artifact. If you cannot name it, do not claim it.**

Base examples were written against 2026-08-02 HEAD `47a8d72`; the suite inventory and CI
contract were re-verified on 2026-08-28 at `d44fc4ac7d2f37475e81ebdc15ccd9ba301247a2`.

## 1. The claim-evidence ladder (the centerpiece)

Every claim about Chronos sits on one rung. Naming a rung ABOVE what your evidence
supports is the exact failure that produced R-25/R-26/R-27: three safety controls were
"implemented, documented, and tested" for milestones while inert (see
`chronos-failure-archaeology`). Use the ladder in code review, in commit messages, in
docs, and in your own head.

| Rung (weakest → strongest) | What it means | Minimum evidence you must name | Chronos examples today (2026-08-02) |
|---|---|---|---|
| 1. Code exists | The lines are present | `file:line` | Almost everything |
| 2. Tested | A test calls it and asserts something | Named test file + what it actually asserts (not just that it exists) | Most of `src/chronos`; the current dated suite count is below |
| 3. Structurally enforced | A test fails on ANY regression, not just the cases someone thought of — typically an AST/source scan or pinned inventory | The structural test + the property it pins | Single transmit site, transmit/mutation inventory, import bans, no-HTML-sinks, ENFORCED/INERT mandate pin (§4a) |
| 4. Exercised | The control has been driven end-to-end on its real blocking path and observed producing its intended outcome (a refusal, an OPEN, a PASS) with realistic payloads | A `test_*_exercised.py`-style test asserting the outcome fires | Opening cap (14 tests), session gate (9), option deliverable (30) — each asserts "the outcome that had never happened" |
| 5. Gateway-verified | Behavior confirmed against a real IBKR TWS/Gateway session | A captured, sanitized real-gateway run | **NOTHING has this. No real IBKR gateway (paper or live) has ever been connected in this project's history** (docs/limitations.md:22-23) |
| 6. Operationally proven | Promotion-ladder rungs earned with live operational evidence (replay → shadow → paper → canary → capped live) | A signed/expiring promotion evidence artifact | **Nothing has this.** Promotion levels in a mandate file are self-declared config; no evidence store exists (VISION_COMPLETION_PLAN.md §6 finding 8) |

Vocabulary that encodes the ladder — use it, never blur it:

- **README labels**: bullets marked `[enforced]` are "live controls with code and tests
  behind them today"; `[contract]` are "structural guarantees of the contract types"
  (README.md:121-122). Neither label means gateway-verified.
- **RISK_REGISTER statuses**: `OPEN / MITIGATED / ACCEPTED / CLOSED`
  (RISK_REGISTER.md:4). **MITIGATED ≠ CLOSED.** All four kernel defects are MITIGATED
  with disclosed residuals; none is CLOSED, because every adapter-path control is
  fixture-verified only ("All four kernel defects are now mitigated and none is closed",
  README.md "Current status"). Do not upgrade a MITIGATED to CLOSED without the evidence
  the residual names (usually: a real-gateway run — see `chronos-real-gateway-campaign`).
- Claim rules for documents (what prose may assert, freeze-before-observe) live in
  `chronos-change-control`. Statistical evidence for strategies (walk-forward, DSR,
  sample floors) lives in `chronos-research-methodology` — a green pytest run is rung 2-4
  evidence about *code*, never evidence that a *strategy* works.

## 2. Test-suite map

> **Current snapshot (2026-08-28, exact main `d44fc4ac7d2f`):** `pytest -q` → **4239
> passed, 1 skipped** (4240 collected); `mypy src/chronos` → 294 source files;
> `mypy --strict worker` → 10 source files; `ruff format --check .` → 548 files; the
> installed-wheel release gate passed. Counts are evidence snapshots, not contracts: rerun
> `make gates` before citing another tree.

~~Verified 2026-08-02: `pytest -q` → **2489 passed, 1 skipped, ~2 minutes** (2490
collected). Companion gate baselines, same date: `mypy src/chronos` → "Success: no
issues found in **218** source files"; `ruff format --check .` → "**379** files already
formatted"~~

> **Historical snapshot (2026-08-09, superseded).** `pytest -q` → ~~**2745 passed, 1 skipped**~~ ~~**2767
> passed, 1 skipped**~~ ~~**2805 passed, 1 skipped**~~ **2889 passed, 1 skipped, ~2
> minutes** (2890 collected).
> Companion gate baselines, same date: `mypy --strict src/chronos` → "Success: no issues
> found in ~~**232**~~ ~~**233**~~ **235** source files"; `ruff format --check .` →
> ~~"**409** files already formatted"~~ ~~"**410**"~~ ~~"**412**"~~ "**416** files already
> formatted".
> The jump has five causes, four of them landing on this date: M12 carried the suite to
> **2543 passed / 1 skipped** at `721d7f1` (CHANGELOG M12) without this row being
> updated; the Five-Tool research slice plus its merge-review tests took it to **2745**;
> the canonical ADR-0013 registry integration added **22**; the certified-reader
> capability added **38** (39 new tests, minus one parametrized case that
> disappeared when the missing-capability list went from three names to two); and the
> replay-artifact capability plus the lookahead-provenance audit added the last **84**
> (85 new tests, minus one more parametrized case as the list went from two names to
> one). The six new safety files across those four landings are
> `test_five_tool_holdout_refusal_exercised.py`, `test_five_tool_inert_fields_disclosed.py`,
> `test_five_tool_registry_exercised.py`,
> `test_five_tool_certified_reader_exercised.py`,
> `test_five_tool_replay_exercised.py`, and
> `test_five_tool_provenance_audit_exercised.py`. Intermediate numbers are struck rather
> than deleted: 2489 is what a session running against `47a8d72` would still observe,
> 2745 is what one running against `5871338` would, 2767 is what one running against
> `5b0bce7` would, and 2805 is what one running against `8721ea0` would.

The format count includes Python scripts under `.claude/skills`, which sit inside `ruff`'s
repo-wide scope. `docs/TEST_RESULTS.md` preserves the dated run artifact; this skill keeps the
execution shape and re-verification commands. Layout under `tests/`
(**collected counts corrected 2026-08-28**; re-derive with
`.venv/bin/pytest -q --collect-only tests/<dir>`):

| Directory | Collected | What lives there |
|---|---|---|
| `tests/unit/` | ~~1461~~ ~~1631~~ **2544** | Application units: brokers, reconciliation, risk, orders, settings, UI models, histdata, research, and release tooling. No `__init__.py`. |
| `tests/integration/` | ~~202~~ ~~208~~ **240** | API/order/crypto flows, Streamlit smoke, terminal API, installed migrations, live-gate walk, and opt-in IBKR smoke. |
| `tests/safety/` | ~~561~~ ~~675~~ ~~759~~ **1159** | The safety acceptance/structural suite — the crown jewels, itemized below. |
| `tests/platform_unit/` | 226 | Deterministic strategy-platform units: engine guards, ledgers, promotion, sim broker, state machine, `test_property_invariants.py` (hypothesis). |
| `tests/parity/` | ~~27~~ **53** | Incremental-vs-batch and indicator reference parity. |
| `tests/chaos/` | 13 | Fault injection through the deterministic platform's backtest/execution/service pipelines. |
| `tests/` root | **5** | OpenCode workflow-policy tests. |
| `tests/support/` | — | Shared fakes: `order_fakes.py` (`FakeBroker`, `paper_settings`, `option_contract`, `stock_contract`), `histdata_fakes.py`, `options_fakes.py`, `terminal_harness.js` (node:vm harness). |

**The single skip** is `tests/integration/test_ibkr_smoke.py` — the opt-in, strictly
read-only IBKR smoke test: marker `ibkr`, skipped unless `CHRONOS_RUN_IBKR_SMOKE=1`
(test_ibkr_smoke.py:15-23). It asserts `allow_order_transmit is False` before touching
anything; `scripts/smoke_test_ibkr.py` forces all transmission flags off. Running it
requires a configured gateway and is an owner act — never run it as part of "verification".

**Root conftest** (`tests/conftest.py`, the only conftest in the tree): `FIXED_NOW =
2026-01-15T15:30Z` (line 9), a `demo_broker` fixture (12-14), and two **autouse ADR-0009
safety tripwires**: session-scoped `_ambient_settings_never_live_capable` (17-36) fails
the entire run if a live-capable `.env` leaks into the test environment, and per-test
`_cached_settings_never_live_capable` (39-52) fails any test that leaves the
process-cached settings live-capable. A suite-wide failure saying `SAFETY TRIPWIRE:
ambient settings are live-capable` means your repo-root `.env`, not the code.

### The load-bearing safety tests (know these by name)

| Test file | What it actually proves |
|---|---|
| `tests/safety/test_single_transmit_site.py` | AST: exactly one `transmit=True` keyword in `chronos.orders`, in `submission.py` (the site is submission.py:745); no module in all of `src/chronos` calls `to_order_request` with non-literal-False transmit; `chronos.orders` imports nothing from `chronos.execution`/`chronos.risk`. |
| `tests/safety/test_broker_mutation_inventory.py` | Repo-wide (src **and** scripts/) inventory of every transmit-enabling site, matching BOTH spellings — `transmit=` keyword AND `order.transmit =` attribute — literal and computed, pinned to an explicit expected set (2 originating: submission.py keyword + the quarantined `execution/brokers/ibkr_paper.py` attribute; 5 propagating). Also pins every `placeOrder`/`cancelOrder` call site, asserts no `exerciseOptions`/`reqGlobalCancel` exists, and that nothing constructs the quarantined adapter. Includes "guard the guard" tests proving the matcher sees computed values. |
| `tests/safety/test_writer_lease_fencing.py` (7) | R-24: `holds()` is true only for the live owner; the split-brain case; structural AST check that the backend lifespan actually calls `renew`/`bind_lease_verifier`/`create_task`; and the **adjacency assertion** — the lease re-check must sit < 40 lines above the `transmit=True` line (test at :126-149). |
| `tests/safety/test_opening_cap_exercised.py` (14) | R-25: drives intents through a real repository → evidence provider → risk check and asserts the refusal that had never once happened; unknown count is UNKNOWN→blocked, not zero; the day boundary is market-local, not UTC. |
| `tests/safety/test_session_gate_exercised.py` (9) | R-26: a qualified contract carrying IBKR's own `liquidHours` string → provider → session decision; asserts all three outcomes including the never-before-seen OPEN; the holiday case (Friday 2026-07-03, venue says `CLOSED`) that no weekday/clock calendar can derive. |
| `tests/safety/test_liquid_hours.py` (29) | The R-26 parser: both IBKR format vintages, `;`/`,` separators, overnight windows, `2400` closes — weighted 13 malformed-input cases vs 5 happy-path (RISK_REGISTER.md:34). |
| `tests/safety/test_option_deliverable.py` (30) | R-27: the five conjunctive deliverable conditions, each verified by reverting it and confirming a DISTINCT failure; asserts the first-ever PASS. Companion: `tests/unit/test_ibkr_broker.py:600-602`, the line that asserted `deliverable_verified is False` for six milestones (pinning the defect) and now asserts `is True`. |
| `tests/safety/test_autonomy_contracts.py` (46) | Model-plane isolation: AST walk over `chronos.autonomy` against a forbidden import set (orders/broker/execution/risk/api/persistence/services/control, ib_async, ibapi, sqlalchemy) including `from chronos import autonomy` aliases, PLUS a subprocess `sys.modules` probe; forbidden field names on the decision contract; only the supervisor + the named wiring module consume the contracts. |
| `tests/safety/test_autonomy_wiring.py` (18) | The ADR-0017 assembly seam: valid mandate file auto-activates; revocation survives restart; wrong-account/broken mandate boots inert; the handoff walks the full propose→preview→confirm→submit pipeline, not a shortcut. |
| `tests/safety/test_supervisor_gateway.py` (49 functions; 61 collected) | The 15 admission checks fail closed (unevaluated ⇒ not passed), and the **ENFORCED/INERT pin**: every mandate limit field is classified, the classification is compared against the model fields (a new field must be classified or the suite fails) AND against kernel source (an INERT field the kernel starts reading fails; an ENFORCED field nothing reads fails) — test_supervisor_gateway.py:634-751. |
| `tests/safety/test_supervisor_compiler.py` | MARKET order form compiles to a quote-derived collared limit (`test_a_market_form_compiles_to_a_collared_limit`, :480); no unbounded market order is expressible. |
| `tests/safety/test_terminal_client.py` (17) | Runs the REAL `terminal.js` inside a `node:vm` fake-DOM harness (`tests/support/terminal_harness.js`) and asserts on rendered claims — no fabricated calm (cached kill switch, null rendered as zero), no actions the backend will refuse. Missing `node`: **skip locally, hard FAIL when `CI` env var is set** (:57-68). |
| `tests/safety/test_terminal_client_has_no_html_sinks.py` | Source scan of the shipped client with comments stripped (the honest comment names the sinks, so raw-text scanning would false-fail): no `innerHTML`/`outerHTML`/`insertAdjacentHTML`/`document.write`/`eval`/`Function`/`javascript:`/`on*` attributes, no external origins. Also asserts the stripper still surfaces a planted sink — a scanner that quietly stops finding things is itself the failure class. |
| `tests/safety/test_single_unmask_site.py` | `unlocked=True` (holdout unmask) is passed only by `holdout_guardian.py`, and the guard is non-vacuous (the guardian really is a site). |
| `tests/safety/test_registry_no_automated_unlock.py` | No automated plane (autonomy, supervisor, scheduled paths — derived from the package tree, not a hand list) imports the registry or calls the holdout unlock. |
| `tests/safety/test_alert_delivery.py` | R-32: owner alerting works AND the module structurally cannot acquire a networked sender — alerts are local sinks only. |
| `tests/integration/test_migrations.py` | Builds a v2-shaped DB, runs `alembic upgrade head`, and asserts `Database.initialize()` accepts it with zero drift. The separate release gate repeats v2-to-head verification through the installed wheel. |
| `tests/integration/test_live_submission.py` | The LIVE branch's ten-gate walk against `FakeBroker`: stale data, reconciliation latch, account match, confirmation hash, and the kill-switch-adjacent-to-transmit wiring (:910). |

Also structural, same family: `test_histdata_isolation.py`, `test_research_isolation.py`,
`test_registry_isolation.py`, `test_model_tool_surface.py`, `test_decision_queue_writer.py`,
`tests/unit/test_ui_no_broker_imports.py`.

## 3. How tests actually run

**CI** (`.github/workflows/ci.yml`, one job `quality`, ubuntu-latest, 20-minute timeout,
every push and PR) runs on Python 3.12 with a hash-locked install
(`pip install --require-hashes -r requirements-dev.lock` then `pip install -e . --no-deps`)
under env `BROKER_MODE=demo`, `ALLOW_ORDER_TRANSMIT=false`, `ALLOW_LIVE_TRADING=false`,
then the **six gates in order**:

```bash
ruff check .
ruff format --check .
mypy src/chronos        # strict = true
mypy --strict worker
pytest -q
python scripts/verify_release_artifact.py
```

Locally: `make gates` runs the same six (Makefile targets `lint`, `format-check`, `type`,
`type-worker`, `test`, `release-gate`; every target hard-codes `.venv/bin/...` — environment
setup, the 3.11 default-python trap, and lockfile discipline are
`chronos-build-and-env`'s domain).

Facts, not endorsements (CI shape re-verified 2026-08-28):

- **No coverage tooling exists** — no pytest-cov, no coverage config, no CI coverage
  step. Any coverage number you see quoted was not produced by this repo.
- **No pytest-timeout.** The 2026-08-28 suite takes roughly three minutes locally; CI's
  20-minute job timeout remains the outer bound.
- pytest config (pyproject.toml:61-67): `--strict-config --strict-markers -ra`,
  `asyncio_mode = "auto"`, `testpaths = ["tests"]`, exactly ONE registered marker: `ibkr`.
- **mypy strict covers `src/chronos` and, in a separate command, `worker`** — tests and
  `scripts/` are not type-checked. Ruff checks everything (line-length 100, rules
  `E,F,I,B,UP,SIM,DTZ,RUF` — `DTZ` means naive datetimes fail lint; use timezone-aware
  datetimes in tests).

## 4. The house proof patterns

These are the repo's signature moves. When you add or fix a safety-relevant control, you
are expected to use the applicable ones.

### 4a. Structural / AST tests — "a regression cannot hide behind mocking"

Write a test that reads source (via `ast.parse` or text) and pins a property that must
hold over the WHOLE tree, compared against an explicit expected set. Use when the claim
is "there is exactly one X" / "nothing in plane A reaches plane B" / "no code does Y".

- Inventory against expected set: `test_broker_mutation_inventory.py` — grows only by a
  deliberate edit to `_EXPECTED_TRANSMIT_SITES`, with an ADR justifying it.
- Import bans: `test_autonomy_contracts.py` (AST walk + subprocess probe — the probe
  catches lazy imports the AST walk exempts), `test_histdata_isolation.py`.
- Single-site: `test_single_transmit_site.py`, `test_single_unmask_site.py` — always
  include the non-vacuity assertion (the one permitted site really exists).
- Adjacency: `test_writer_lease_fencing.py:126-149` — asserts an ORDERING property (the
  lease re-check stays within 40 lines of the transmit line), because a check that
  drifted early would be just another startup flag. Same shape as the kill-switch
  re-read beside it.
- Source scan of shipped non-Python assets: `test_terminal_client_has_no_html_sinks.py`
  (comments stripped first, stripper itself pinned by planted-sink tests).
- **Guard the guard**: every serious structural test here also tests its own matcher
  (`test_the_matcher_sees_computed_transmit_values`,
  `test_the_classification_matches_what_the_kernel_actually_reads`). A scanner that
  quietly stops finding things is the same defect class it exists to catch.

### 4b. EXERCISED tests — prove the control FIRES, not that code exists

The R-25/R-26/R-27 lesson: all three controls were implemented, documented, and had
passing tests; none had ever produced its intended outcome end-to-end. An exercised test:

1. Drives the FULL blocking path with realistic payloads (real repository writes, a real
   IBKR-shaped `liquidHours` string, a real OSI local symbol) — not a mock of the object
   under test.
2. Asserts the outcome that matters — **including the outcome that has never been
   observed** (the refusal for a cap, OPEN for a gate, PASS for a screen). If your test
   only ever sees the control's default answer, you have not exercised it.
3. Pins both failure directions: fail-closed hid R-26/R-27 (blocked everything,
   invisibly); fail-open hid R-25 (passed everything, invisibly). Neither direction
   produced a test failure until the exercised tests existed.

Naming convention: `tests/safety/test_<control>_exercised.py`. Multiple controls now follow it;
derive the current inventory with `rg --files tests/safety | rg 'exercised.*\.py$'`. Follow the
convention when you make an inert-capable control fire.

### 4c. Revert-the-fix verification — manual mutation testing

For a fix with N independent parts (conjuncts, conditions, halves), verify each part by
reverting IT ALONE and confirming a **distinct** named test fails. This is recorded in
the risk register as part of the evidence: R-25 "each half of the fix verified by
reverting it and confirming a distinct failure" (RISK_REGISTER.md:33); R-27 "each
condition verified by reverting it and confirming a distinct failure" (:35). If
reverting a conjunct fails NO test, you shipped an unverified conjunct — add the test
before claiming the fix. Do this on a scratch working copy; record which test caught
which revert in the commit message.

### 4d. Fail-closed weighting — malformed inputs outnumber happy paths

In a fail-closed system, a spurious pass is the only output that can open a gate that
should hold — so tests weight toward the rejecting direction. R-26's parser suite: 13
malformed-input cases vs 5 happy-path (RISK_REGISTER.md:34). R-27's suite is "weighted
toward the rejecting direction" by design. When testing any new gate: enumerate the ways
evidence can be absent/garbled/contradictory FIRST, and assert each blocks; the happy
path comes last.

### 4e. Fixtures must never set verification flags by fiat

R-27's mechanism: `DemoBroker` was the ONLY thing in the codebase that set
`deliverable_verified=True` (by fiat), so demo-driven tests passed while both real
adapters left the flag False and every real option order would have been refused.
`DemoBroker` still sets it by construction (demo.py:670) — that is fine ONLY because
exercised tests now prove the production adapters set it through the real screen. The
rule: whenever a test or fixture hand-assigns a verification/evidence flag
(`deliverable_verified`, `liquid_hours`, readiness, arming), there must exist a separate
exercised test proving production code sets that flag on the real path. A fixture-set
flag with no production setter is a latent R-27. Worse: a unit test asserted the
DEFECT's output (`is False`) for six milestones — a passing assertion can be a pinned
bug. When a test asserts a control's negative outcome, ask whether that outcome is
design or defect.

### 4f. Tripwire / autouse guards

`tests/conftest.py`'s two autouse fixtures (§2) make the SUITE ITSELF refuse to run
live-capable: a leaked live `.env` or a test that leaves cached settings live-capable
fails loudly. Pattern: when an entire category of environment must never occur during
testing, enforce it as an autouse fixture at the root, not as documentation.

### 4g. Property / fuzz / mutation / chaos — current state (labeled honestly)

- **Property tests exist (narrow):** `tests/platform_unit/test_property_invariants.py`
  (hypothesis) pins 4 invariants — intent-identity aliasing, state-machine legality,
  sizer bounds, risk-engine deny-monotonicity. `tests/unit/test_terminal_commands.py`
  also uses hypothesis. `hypothesis` is a dev dependency.
- **Chaos tests exist (deterministic platform only):** `tests/chaos/` — 13 tests, fault
  injection through backtest/execution/service pipelines via `FaultPlan`. The live-Wheel
  order plane has fault-path tests (e.g. `FakeBroker.submit_error`) but no dedicated
  chaos suite.
- **No fuzz harness and no automated mutation tooling exist.** Revert-the-fix (§4c) is
  manual mutation testing; nothing automates it.
- **GAP — OPEN:** Phase 1's EXIT criterion demands "full safety suite plus property,
  fuzz, mutation, and chaos coverage; fresh install, migrations, restore, kill/rearm,
  clock/disk/gateway failure tests" (docs/VISION_COMPLETION_PLAN.md:177-179). Current
  coverage does not meet that bar. Do not represent it as met.

## 5. Adding a test that actually protects

Where it goes: pick by what the test IS, not what module it touches —

| Kind | Directory |
|---|---|
| Unit of the wheel-dashboard subsystem (`chronos.orders/broker/api/ui/services/...`) | `tests/unit/` |
| Unit of the deterministic platform (`chronos.execution/risk/control/backtest/...`) | `tests/platform_unit/` |
| Cross-component flow through the API or DB | `tests/integration/` |
| Structural guarantee, isolation ban, exercised safety control | `tests/safety/` |
| Reference/incremental parity | `tests/parity/`; fault injection | `tests/chaos/` |

Conventions (all verified against the existing suite):

1. **Names are claims.** Files `test_<subject>.py`; exercised controls
   `test_<control>_exercised.py`. Test functions read as the sentence they prove:
   `test_the_boundary_rechecks_the_lease_immediately_before_transmit`. Module docstrings
   state what defect class the file pins and its honest residual.
2. **Markers:** only `ibkr` is registered and `--strict-markers` is on — an unregistered
   `@pytest.mark.slow` fails collection. Registering a new marker is a pyproject change;
   keep it rare and deliberate. `--strict-config` likewise. `asyncio_mode = "auto"`:
   async tests need no marker.
3. **Fixtures/helpers:** reuse `tests/conftest.py` (`demo_broker`, `FIXED_NOW`) and
   `tests/support/` (`FakeBroker` records every call and reaches no venue;
   `paper_settings`, `option_contract`, `stock_contract`, histdata/options fakes,
   `terminal_harness.js`). Build `Settings` in tests with `_env_file=None` (see
   `test_opening_cap_exercised.py:59-62`) so your ambient `.env` cannot leak in. Use
   timezone-aware datetimes (ruff `DTZ`) and fixed clocks, never `now()`.
4. **CI-faithful means:** passes under `BROKER_MODE=demo`,
   `ALLOW_ORDER_TRANSMIT=false`, `ALLOW_LIVE_TRADING=false`; no network at all; passes
   WITHOUT `ibapi` installed (it is not on PyPI and CI never installs it); `node` is
   required for terminal-client tests (present on GitHub runners; absent-node is a local
   skip but a CI failure); and never writes inside the repo (use `tmp_path`).
5. **No test may transmit or mutate a broker** — ADR-0016 §8 pins "the prohibition
   against broker mutations from tests or CI". Tests are deliberately excluded from the
   transmit-site AST scans because they construct fakes; that exclusion is safe only as
   long as tests touch fakes exclusively.
6. **When a safety control changes: update its exercised test FIRST.** Freeze the
   expected refusal outcomes (which inputs must block, with which reason) before you
   touch the control, then change the control, then run revert-the-fix (§4c) on each new
   conjunct. If your change adds/moves a structural fact (a transmit site, an import, a
   mandate field), the corresponding inventory/pin test will fail — updating its expected
   set is a reviewable, ADR-worthy act, not a chore. Adding a mandate limit field forces
   you to classify it ENFORCED or INERT in `test_supervisor_gateway.py` — classify
   honestly; an inert field must also be disclosed in `admission.py`'s docstring.

## 6. The adversarial-review tradition

Adversarial review is this repo's core development ritual, not an afterthought — every
feature block in the visible history is followed by an explicit remediation commit
(22 non-merge commits matching "remediat" as of 2026-08-02, e.g. "M5 remediation: fix 10
confirmed adversarial-review findings", "C2 review remediation: fix 8 confirmed
holdout-guardian findings").

- **Artifacts:** `docs/INDEPENDENT_REVIEW.md` (seven fresh reviewer agents, none of whom
  authored the modules reviewed, required to *demonstrate* defects with file:line
  evidence or repro output, not summarize design), `docs/INDEPENDENT_REVIEW_M5.md`
  (round 2 — reviews are re-run against current code, with per-dimension HOLDS/fixed
  verdicts), `docs/REMEDIATION_REPORT.md` (disposition of every finding, with the suite
  count before/after).
- **The M0 audit** (2026-07-25, at the autonomy governance reset) found the four kernel
  defects R-24..R-27 — controls that had passed review-free milestones for weeks.
  Findings land in `RISK_REGISTER.md` with status and **disclosed residuals**, and
  overstated claims are corrected "toward the weaker, true statement rather than toward
  the code" (commit 22450b1).
- **When to request one:** before any promotion, any authority change (mandate widening,
  new order form, ceiling raise — see `chronos-autonomy-and-mandates`), and at Phase 1
  exit ("independent review finds no unresolved Critical/High issue",
  VISION_COMPLETION_PLAN.md:178-179). Instruct reviewers the house way: fresh eyes, hunt
  real defects, cite file:line, rate severity/confidence, and say plainly when a seam
  holds rather than manufacture findings.

This library must preserve that culture: a review that produces no RISK_REGISTER entry
and no remediation commit is a summary, not a review.

## 7. What a green suite does NOT prove

Restate these verbatim when anyone (including you) is tempted to over-claim:

1. **No real IBKR gateway — paper or live — has ever been connected in this project's
   history.** Every adapter behavior (real `liquidHours` strings, crypto metadata field
   names, pacing, ack ordering) is fixture-verified conjecture (docs/limitations.md:16-23).
2. **Paper ≠ live.** The paper submission branch consults neither arming nor the kill
   switch; only the LIVE branch walks the ten-gate stack (see
   `chronos-architecture-contract`).
3. **MITIGATED ≠ CLOSED.** Every adapter-path control keeps a disclosed residual until
   gateway-verified.
4. **No options simulator exists** and **zero strategies have ever been selected** by the
   research pipeline; the one QQQ holdout is consumed (see
   `chronos-research-methodology`). A green suite says nothing about edge.
5. **Green tests once coexisted with four inert kernel defects** — tests passed for
   milestones while the controls could never fire, and one test pinned a defect as
   expected behavior for six milestones. Rung 2 of the ladder is weak; treat it so.

## 8. When NOT to use this skill

- Statistical evidence gates for strategies (DSR, walk-forward, holdouts, trial
  counting) → `chronos-research-methodology`.
- Building the environment, lockfile, container traps → `chronos-build-and-env`.
- What documents may claim, doc precedence, ADR/task discipline →
  `chronos-change-control`; which doc to trust → `chronos-docs-map`.
- The inert-control history in depth → `chronos-failure-archaeology`; the
  ContractDetails prevention checklist → `chronos-ibkr-boundary`.
- Running/operating the system → `chronos-run-and-operate`; read-only state inventory →
  `chronos-diagnostics`.

## Provenance and maintenance

Base content verified 2026-08-02 against HEAD `47a8d72`; the suite/CI facts were
re-verified 2026-08-28 against exact main `d44fc4ac7d2f`. Volatile facts and how to
re-verify each (all read-only):

| Volatile fact | Re-verify with |
|---|---|
| **4239 passed / 1 skipped (2026-08-28)**; older lineage remains in §2 | `.venv/bin/python -m pytest -q` (per README Setup) |
| **mypy 294+10 source files; format 548 files (2026-08-28)** | `.venv/bin/mypy src/chronos`; `.venv/bin/mypy --strict worker`; `.venv/bin/ruff format --check .` |
| Per-directory counts **2544/240/1159/226/53/13**, plus 5 root tests | `.venv/bin/python -m pytest tests/<dir> --collect-only -q \| tail -1` |
| The single skip is the IBKR smoke test | `.venv/bin/python -m pytest -q -ra \| grep -A1 SKIPPED` |
| Six CI gates, installed-wheel gate, and CI env | `sed -n '1,70p' .github/workflows/ci.yml` |
| Only marker is `ibkr`; strict flags | `sed -n '61,68p' pyproject.toml` |
| No coverage / pytest-timeout tooling | `grep -in "coverage\|timeout" pyproject.toml Makefile` (only CI job timeout exists) |
| Transmit site at submission.py:745 | `grep -n "transmit=True" src/chronos/orders/submission.py` |
| Conftest tripwires | `sed -n '17,52p' tests/conftest.py` |
| Kernel-defect statuses + revert-the-fix notes | `sed -n '31,35p' RISK_REGISTER.md` |
| README [enforced]/[contract] labels | `sed -n '119,125p' README.md` |
| Phase-1 EXIT (property/fuzz/mutation/chaos) still unmet | `grep -n -B2 -A2 "property, fuzz, mutation" docs/VISION_COMPLETION_PLAN.md`; `ls tests/chaos`; `grep -rln hypothesis tests --include='*.py'` |
| Remediation-commit count (22) | `git log --no-merges --oneline -i --grep="remediat" \| wc -l` |
| Exercised-test collected counts (14/9/30/29) | `.venv/bin/python -m pytest tests/safety/test_opening_cap_exercised.py tests/safety/test_session_gate_exercised.py tests/safety/test_option_deliverable.py tests/safety/test_liquid_hours.py --collect-only -q` |

If any re-verification disagrees with this file, trust the live repo, fix this file, and
note the drift — stale claims in docs are this repo's most-caught defect class.
