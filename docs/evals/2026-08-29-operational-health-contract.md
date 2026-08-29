# Operational-health contract evaluation — 2026-08-29

```yaml
plan_phase: 2
primary_kpi: broker_truth
gate_advanced: none
files: docs/adr/ADR-0040-operational-health-projection.md, docs/evals/2026-08-29-operational-health-contract.md, src/chronos/operations/health.py, src/chronos/api/operational_health.py, src/chronos/api/task_observations.py, health and operator-surface adapters/tests
verification: focused contract, API, chaos, confidentiality, terminal, and adapter tests; full make gates and installed-artifact gate before merge
evidence_artifact: docs/evals/2026-08-29-operational-health-contract.md
owner_gate: satisfied by Kevin's 2026-08-29 instruction to resume autonomously and merge each reviewed slice
open: automatic clock health, orchestrator status-code probes, watchdog/dead-man monitoring, external alerts, operational SLOs, and real PAPER/gateway evidence
```

## Claim under test

The W2 candidate separates request liveness, operator-service readiness, and lane-specific
new-exposure capability without adding an authority path or a broker call to health polling.
Unknown or stale evidence never produces `AVAILABLE`; legacy `/health` fields remain present
with their original JSON types and are explicitly scoped as compatibility only.

## Real application observations

The measurement exercised the actual FastAPI lifespan, route models, DemoBroker, SQLite
store, terminal read model, and browser client. It made 28 health/system reads across these
conditions; 12 were repeated polls used to detect accidental broker calls. All state changes
were confined to disposable temporary paths.

| Condition | Observed liveness/readiness | Observed trading effect |
|---|---|---|
| Normal DEMO writer | `LIVE` / `READY` | No false availability; clock remains `UNKNOWN` |
| Process before backend lifespan | `LIVE` / `STARTING` | New exposure blocked |
| Externally held writer lease | `LIVE` / `READY` | All lanes blocked by `writer_lease_absent` |
| Local store read failure | `LIVE` / `NOT_READY` | Blocked by `store_unreadable` |
| Sanitized startup fault | `LIVE` / `NOT_READY` | Blocked by `startup_degraded` |
| Startup reconciliation exception | `LIVE` / `NOT_READY` | API inspectable; submission locked |
| Unexpected autonomy task return | `LIVE` / `NOT_READY` | Retained as `FAILED/exited_unexpectedly` |
| Lease-heartbeat loss | Local state demotes to read-only | Failed task retained; writer absent |
| Broker cache invalidation | Service remains `READY` | Connection unknown; old positive fact erased |
| Broker loop stopped | Service remains `READY` | Blocked by `broker_loop_down` |
| Reconciliation invalidated | Service remains `READY` | Blocked by `reconciliation_not_ready` |
| Terminal poll stale/unavailable | Stale/unreachable display | Cached capability renders `UNKNOWN` |

The disclosure probe also searched unauthenticated JSON for raw account IDs and identity,
token, fingerprint, and mandate keys. None were present. Repeated polling observed one stable
broker-observation generation and raised if the broker adapter's status method was called.

## Truth-table, boundary, and fault evidence

The pure evaluator covers positive conjunctions, read-only operation, every required task
state, exact freshness boundaries, future timestamps, broker/reconciliation staleness,
lane-local facts, multiple simultaneous faults, deterministic reason ordering, and monotonic
weakening. Chaos coverage combines store, task, broker, reconciliation, and clock faults.

An AST boundary test scans the order, supervisor, risk, broker, and runtime authority trees
and fails if any imports the health projection. Connection-manager tests prove cached status
is sanitized, read without broker I/O, and invalidated with a generation advance. Task tests
prove expected shutdown is distinct from return, raise, and cancellation and that exception
text cannot enter the report.

## Red-to-green and negative control

Before implementation, the existing startup-reconciliation failure test was extended to
require schema v2 and `NOT_READY`. It failed with `KeyError: schema_version` while the old
endpoint still returned `status: ok`. After the collector and projection were wired, the same
real-app test passed and retained compatibility `status: ok` only under
`status_scope: compatibility_only`.

The evaluator's deletion boundary is explicit: authority modules cannot import it, and no
order-path decision consumes a health verdict. Removing its wiring removes diagnostics, not an
admission or send predicate.

## Scope and residuals

No live service, external network, broker gateway, credential, capital, owner mandate,
migration, or deployment was touched. One disposable temporary mandate exercised the terminal
read model; DemoBroker cannot transmit. These observations validate the code contract, not
operational availability or trading safety. In particular, there is no trusted clock input in
W2, so the shipped collector cannot claim a fully evidenced `AVAILABLE` lane.

## Candidate verification

After the implementation, documentation, generated source fingerprints, and integration
repairs were all present, the local CI mirror reported:

```text
make gates
ruff: All checks passed; 578 files already formatted
mypy: 301 Chronos source files and 10 worker files clean
pytest: 4446 passed, 1 skipped, 25 warnings in 184.16s
installed-wheel gate: PASS; migration head 0010, 34 model tables, 5 module entry points
CycloneDX 1.6 SBOM: valid, reproducible, 64 runtime components
```

The single skip is the expected owner-opt-in, read-only IBKR smoke test; no gateway was
configured or contacted. The warnings are existing Starlette/FastAPI and multiprocessing
deprecations. The exact committed candidate and hosted-CI run are bound in the pull request,
because a commit cannot contain its own hash.
