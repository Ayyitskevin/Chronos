---
name: chronos-architecture-contract
description: "Map or change Chronos cross-package architecture. Use for locating ownership, tracing state or execution paths, evaluating imports and refactors, separating similarly named planes, and checking architectural invariants. Differentiator: derive state, feedback, coupling, timing, and enforcement from the checked-out source and tests instead of a cached package or ADR inventory; route authority-changing work through chronos-change-control."
---

# Chronos architecture contract

Treat architecture as a set of claims to prove against the checked-out revision,
not a diagram to remember. A package name, document, generated matrix, test name,
or historical handoff can suggest where to look; none replaces the current exports,
callers, state owners, feedback paths, timing behavior, and enforcing tests.

## Route the task

Use this skill when work crosses package boundaries or asks:

- where a behavior, decision, or durable record is owned;
- how a request moves between entry point, policy, state, and external effect;
- whether a new import, shared utility, refactor, or deletion preserves separation;
- which similarly named order, execution, control, stop, or promotion concept applies;
- which test enforces an architectural claim and what failure it prevents; or
- whether prose describes executable capability, configured authority, or observed
  operational evidence.

Route permission, ADR, safety-boundary, and merge classification to
`chronos-change-control`. Route document contradictions to `chronos-docs-map`.
Route runtime procedures to `chronos-run-and-operate`, model authority to
`chronos-autonomy-and-mandates`, broker semantics to `chronos-ibkr-boundary`,
research isolation to `chronos-research-methodology`, and proof design to
`chronos-validation-and-qa`.

## Source hierarchy

Read the sources needed for the question, in this order:

1. `AGENTS.md` and `docs/AGENT_PROTOCOL.md` for safety, scope, task contracts,
   review, and merge authority.
2. `DECISIONS.md` and the relevant records under `docs/adr/` for accepted intent
   and supersession history.
3. `docs/ARCHITECTURE.md` for the maintained system narrative.
4. `docs/generated/CURRENT_STATE.md` and
   `docs/generated/capability-matrix.json` for source-derived default posture and
   mapped code paths. They do not confer runtime authority.
5. `RISK_REGISTER.md`, `docs/safety.md`, and `docs/limitations.md` for controls,
   residuals, and evidence boundaries.
6. `pyproject.toml`, the live tree under `src/chronos/`, its exports and immediate
   callers, and the enforcing tests under `tests/safety/`, `tests/integration/`,
   and `tests/unit/`.

Executable source and exercising tests settle current behavior. Accepted decisions
settle intended architecture. When those disagree, stop and route the discrepancy
through `chronos-change-control`; do not silently make code preserve stale prose or
rewrite governance to match accidental code.

## 1. Establish revision and repository state

Start read-only and record the identity being analyzed:

```bash
git fetch origin --prune
git status --short --branch
git rev-parse HEAD
git ls-remote --symref origin HEAD
git log --oneline --branches --tags --not --remotes
```

An unpushed branch or active PR may already contain the apparent change. A summary
from another revision is a hypothesis until its critical path is reproduced here.

## 2. Derive topology and accepted decisions

Discover the current package surface and import edges instead of copying a package
table into this skill:

```bash
find src/chronos -mindepth 1 -maxdepth 1 -type d ! -name __pycache__
rg -n '^from chronos|^import chronos' src/chronos
rg -n '^Status:' docs/adr
rg -n '^\| D-[0-9]+' DECISIONS.md
```

Read `__init__.py`, public models or protocols, construction roots, and the relevant
ADR before assigning ownership. Then read immediate callers and tests. Directory
proximity is not ownership: wiring may live in an application root while state and
policy remain owned by separate modules.

Start with the maintained distinction in `docs/ARCHITECTURE.md`, then prove it:

- the Wheel order path is anchored by `chronos.orders` and application wiring;
- the deterministic platform has a distinct `chronos.execution` path;
- model-facing contracts live under `chronos.autonomy`; and
- `chronos.supervisor` mediates decisions before the Wheel order boundary.

These names are navigation aids, not permission to merge concepts. Trace actual
types, imports, constructors, and calls before claiming that a boundary exists or
that two similarly named mechanisms are equivalent.

## 3. Answer the four invariables

Every architecture analysis must answer all four questions for the behavior in
scope.

### Where does state live?

Identify the canonical in-memory owner, durable store, serialization model, scope
binding, and recovery path. Separate a declared default from configured external
state and from a value observed at runtime. Find every writer before trusting a
reader.

Useful searches:

```bash
rg -n '<symbol>|<state-field>' src/chronos tests
rg -n 'commit|flush|fsync|persist|append|update|write' <candidate-path>
```

### Where does feedback live?

Find the result type, refusal or error code, logs, alerts, journals, health/readiness
surface, and test assertion that make success and failure visible. An exception that
never reaches an operator surface is not adequate feedback; an empty log is not
positive evidence.

```bash
rg -n 'Outcome|Result|Status|reason|alert|journal|health|readiness' <candidate-path> tests
```

### What breaks if I delete this?

Trace imports, exports, constructors, configuration references, entry points,
fixtures, migrations, docs, and structural inventories. Search by symbol and file
name. A zero-result search only supports the exact syntax and tree it scanned.

```bash
rg -n '<symbol>|<module-name>|<file-name>' src scripts tests docs pyproject.toml
rg -n '^from chronos|^import chronos' src/chronos
```

### When does timing work?

Trace startup order, application lifespan, task creation and cancellation, locks,
leases, expiry and freshness clocks, callback ordering, retries, reconciliation,
and crash/restart recovery. State which clock and transaction boundary make each
ordering claim true.

```bash
rg -n 'lifespan|startup|shutdown|create_task|await|lock|lease|expires|stale|reconcile' \
  <candidate-path> tests
```

An unanswered invariable is an explicit open item, not an invitation to guess.

## 4. Prove high-risk boundaries

For broker, autonomy, research, operator, or persistence seams, use both broad
inventory and the repository's semantic tests. Begin with searches such as:

```bash
git grep -n 'transmit=True\|order.transmit = True' -- '*.py'
rg -n 'placeOrder|cancelOrder|exerciseOptions|reqGlobalCancel' src/chronos scripts tests/safety
rg -n 'chronos\.(orders|broker|execution|risk|api|persistence)' \
  src/chronos/autonomy tests/safety
rg -n 'unlocked=True|holdout|trial_started' src/chronos tests/safety
```

Then read and run the tests that define the current boundary. Representative
starting points include:

- `tests/safety/test_broker_mutation_inventory.py` for originating,
  propagating, and venue-mutating broker sites;
- `tests/safety/test_autonomy_contracts.py` for model-plane imports and decision
  capabilities;
- `tests/safety/test_registry_isolation.py` and
  `tests/safety/test_registry_no_automated_unlock.py` for research isolation;
- `tests/safety/test_writer_lease_fencing.py` for write ownership and loss of
  authority; and
- `tests/unit/test_ui_no_broker_imports.py` for read-surface isolation.

Do not reduce an AST-backed inventory to a text-search claim. Read its scanned
trees, classifications, exclusions, and expected set. The safety property is often
the relationship between an originating authority, its propagation, and the final
external call—not the count of one spelling.

## 5. Record one proof packet per claim

Use this format for each material architecture conclusion:

```yaml
claim: <one falsifiable sentence>
state_owner: <in-memory and durable authority, or none>
entrypoints: <construction roots or external inputs>
callers: <immediate consumers and downstream effects>
feedback: <result, refusal, log, alert, health, or journal surface>
timing: <ordering, clock, lock, transaction, and restart behavior>
enforcing_tests: <specific current test paths and focused command>
failure_if_changed: <what becomes inconsistent, unsafe, or unreachable>
evidence_status: <code path, fixture, replay, paper observation, or live observation>
```

Use file and symbol names rather than copied line numbers. Include the exact revision
and commands beside the packet when handing it off. If a claim has no enforcing test,
record that as a finding; prose is not enforcement.

## 6. Re-derive weak points and evidence status

The plan and risk register carry candidate gaps, but both instruct the reader to
reverify them. Read the current sections, then trace each relevant item through code
and tests:

```bash
sed -n '/^## 6/,/^## 7/p' docs/VISION_COMPLETION_PLAN.md
rg -n 'OPEN|MITIGATED|ACCEPTED|CLOSED' RISK_REGISTER.md
rg -n 'fixture|gateway|operational|unproven|unsupported' docs/limitations.md docs/safety.md
```

Classify the live result precisely:

- still reproducible in current code;
- corrected in code but stale in prose;
- structurally enforced but lacking operational evidence;
- fixture- or replay-verified only;
- owner-gated and intentionally unexercised; or
- disproved by the checked-out revision.

`MITIGATED` is not `CLOSED`. A row in
`docs/generated/capability-matrix.json` reports a mapped code path, not configured
authority, broker truth, or operational evidence. A test fixture can prove handling
of an input shape without proving a provider emits that shape.

## 7. Classify a proposed change before editing

Load `chronos-change-control` and state the repository task contract. A factual
skill correction or contract test that changes no runtime behavior is normally
owner-independent and may truthfully use `gate_advanced: none`. A change to an
accepted architecture, safety boundary, broker mutation path, authority owner,
durable safety state, schema semantics, or production timing is owner-gated or
proposal-only governance even when the diff is small.

For any cross-package change:

1. name the old owner and proposed owner;
2. list every import and construction edge that changes;
3. state how state, feedback, deletion impact, and timing remain coherent;
4. identify the ADR that permits the relationship or propose a new decision;
5. add or strengthen a structural test before relaxing an existing inventory; and
6. keep the change to one reviewable, reversible unit.

Never add an exemption merely to make a structural test green. A newly legitimate
edge needs an evidence-backed architectural reason and the review tier its effects
require.

## Known pitfalls

- **Same word, different authority:** order, execution, halt, kill, promotion,
  status, and paper can name distinct planes. Follow the owning type and caller.
- **Construction is capability:** an unused class is not a runtime path. Prove who
  constructs it before describing it as active or safe to delete.
- **Default is not deployment:** committed settings do not reveal environment,
  credentials, owner artifacts, broker state, or current process state.
- **Generated is not authorized:** generated current-state artifacts improve
  legibility but cannot grant a mandate, promotion, gateway, account, or market-data
  truth.
- **Absence needs scope:** a grep that finds nothing may omit scripts, generated
  calls, alternate syntax, imports, or runtime construction. State the searched tree
  and use semantic tests for safety claims.
- **Persistence has identity:** changing a path or database URL can detach safety
  state even when models and migrations remain unchanged.
- **Timing is architecture:** startup order, cancellation, callbacks, lease loss,
  and restart recovery can invalidate a correct-looking static dependency graph.
- **Historical correction is not current truth:** preserve historical narrative in
  its owning document while deriving current behavior from source and tests.

## Close the loop

Before reporting an architecture analysis or change:

1. capture the exact revision and clean status;
2. complete every proof packet and surface unanswered fields;
3. run the focused structural tests named by the packets;
4. run `.venv/bin/python -m pytest -q tests/unit/test_architecture_skill_contract.py`
   when this skill changes;
5. run `make gates` after the last edit for a shipped change;
6. obtain the required non-author review at the exact candidate SHA; and
7. after merge, prove changed-path equality or ancestry and exact-default CI.

Report warnings, skips, fixture limits, owner gates, and missing operational evidence.
A clean static map with an unanswered state, feedback, coupling, or timing question is
an incomplete architecture claim.
