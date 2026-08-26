# QQQ managed-position admission — risky-change evaluation

Date: 2026-08-25

## Task contract

```yaml
plan_phase: 1
primary_kpi: broker_truth
gate_advanced: none; code-only prerequisites for ADR-0035 activation blockers 1-2
files: authenticated admission seam, entry-risk evidence/check, schema-v11 binding, migration, safety/integration tests, ADR/index/risk/status docs
verification: red-green focused tests, migration/order integration tests, static gates, full repository gates, independent non-author review
evidence_artifact: this evaluation plus the atomic managed_position_bindings/hash-chain registration
owner_gate: required at merge; financial-risk and future authority semantics
open: ongoing management adapter, trusted management queue, broker-held protection, scheduler, real PAPER lifecycle, LIVE authority
```

## Measurement boundary

No broker credentials, real account, gateway, market dataset, order submission,
promotion artifact, PAPER campaign, or holdout was opened. This is code-only
evidence. IBKR states that paper fills and some order types are simulated, so a
future PAPER pass would still not establish LIVE execution equivalence.

Official interface assumptions were frozen before implementation:

- execution history is session/configuration bounded, so missing execution
  evidence refuses;
- active/open orders omit completed orders, so disappearance never proves fill;
- positions are account/contract aggregates, so exact quantity coherence and no
  other working QQQ order are required; and
- permanent order ID plus Chronos `orderRef`, local broker-order identity, account,
  contract, side, quantity, and time form the admission proof.

Sources: [executions](https://interactivebrokers.github.io/tws-api/executions_commissions.html),
[open orders](https://interactivebrokers.github.io/tws-api/open_orders.html),
[positions](https://interactivebrokers.github.io/tws-api/positions.html), and
[paper limitations](https://ibkrcampus.com/campus/glossary-terms/paper-trading-account/).

## Red-green evidence

The focused tests first demonstrated each missing control and then the intended
refusal or authorization behavior:

- absent module, future execution, admission before fill, fractional fill,
  local/broker fill contradiction, and invalid fill-rebased stop;
- entry-risk metadata that did not affect approval, and a forged overall PASS
  without the named passing management-risk check;
- another working QQQ order and unexplained aggregate QQQ exposure; and
- non-positive broker/permanent order identities; and
- a full-suite structural guard that caught the first implementation importing
  `chronos.broker` from `supervisor`; the final design uses a local read-only
  evidence protocol and the supervisor-boundary guard passes.

Positive controls prove a valid full fill, a terminal cancelled partial fill,
exact idempotent replay without a second broker observation, and clean retry after
a refused transaction leaves no partial binding.

## What the implementation proves

- Public proposal persistence carries the exact versioned QQQ risk evidence, and
  the ordinary risk engine rejects native-stop, CVaR, or gross-cap breaches.
- Admission takes only local identity plus time and re-derives all economic facts.
- Two stable broker reads, unchanged reconciliation provenance, positive execution
  identity, local lifecycle agreement, and exact aggregate-position coherence are
  conjunctive.
- Schema v11 enforces one order and one deterministic position per account scope;
  registration and hash-chain append are atomic and exact retries are idempotent.
- The capability has no runtime consumer or broker mutation path.

## What it does not prove

- Ongoing `PositionObservation` and directive-resolution facts are still caller
  attestations; this admission seam authenticates only opening registration.
- No trusted management-event queue, broker-held stop/target protection, scheduler,
  disconnect/gap behavior, restart recovery, or ambiguous-send drill exists.
- A point-in-time double read cannot eliminate a broker change immediately after
  the second snapshot, recover execution history the gateway no longer exposes, or
  allocate unexplained manual exposure to a Chronos strategy.
- No edge, funding, PAPER readiness, promotion rung, or LIVE authority follows.

## Verification result

```text
.venv/bin/ruff format --check <changed Python files>
# 11 files already formatted

.venv/bin/ruff check <changed Python files>
# All checks passed

.venv/bin/mypy src/chronos/orders/risk.py src/chronos/orders/service.py \
  src/chronos/supervisor/position_admission.py \
  src/chronos/persistence/schema.py src/chronos/persistence/database.py
# Success: no issues found in 5 source files

.venv/bin/pytest -q tests/safety/test_managed_position_admission.py \
  tests/safety/test_paper_position_management.py \
  tests/integration/test_order_pipeline.py tests/integration/test_migrations.py \
  tests/unit/test_database.py
# 142 passed

make gates
# ruff: All checks passed
# format: 539 files already formatted
# mypy: 292 Chronos source files; worker strict: 10 source files
# pytest: 4,063 passed / 1 skipped / 14 failed

.venv/bin/pytest -q \
  tests/unit/test_five_tool_trials.py::test_concurrent_processes_keep_chain_and_anchor_consistent
# first isolated retry: failed on the known temporary hard-link race
# second isolated retry: 1 passed
```

The exact-main baseline at `e1770b76069c04a59f014a5f64faed71ddae4338` was
static-clean and reported 4,045 passed / 1 skipped / the same 13 Streamlit 1.62
relative-path failures. This branch adds 19 tests. Its fourteenth full-run failure
was the pre-existing concurrent-ledger hard-link transient already disclosed in
the ADR-0035 evaluation; it failed once more and then passed on the next isolated
retry. No changed file reaches that research-ledger subsystem. Independent-review
results are appended before the PR is declared merge-ready.

## Result

**Pragmatic partial, owner-gated.** The implementation materially improves broker
truth and durable identity while remaining inert. It advances no promotion gate.
Owner merge approval and a separate non-author review are required; activation is
out of scope.
