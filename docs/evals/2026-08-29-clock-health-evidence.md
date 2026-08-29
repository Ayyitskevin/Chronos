# Quantitative clock-health evidence — 2026-08-29

```yaml
plan_phase: 2
primary_kpi: broker_truth
gate_advanced: automatic clock-health code path; operational proof and authority enforcement remain open
files: src/chronos/operations/clock.py, settings/lifespan/operational-health adapters, clock/health/settings/boundary tests, ADR-0041 and operational documentation
verification: focused parser/process/cache/lifecycle/projection tests; full make gates and installed-artifact gate before merge
evidence_artifact: docs/evals/2026-08-29-clock-health-evidence.md
owner_gate: not applicable; observation-only, no threshold selection or authority change
open: owner-selected error threshold, chronyd installation/configuration, real-host capture, off-host alerts, watchdog/dead-man behavior, clock authority gate, and operational SLO/drill evidence
```

## Claim under test

With the provider explicitly enabled and a maximum error supplied, Chronos can take a bounded,
quantitative local chrony observation, cache a sanitized result, and degrade every unavailable,
ambiguous, stale, or future case to `UNKNOWN` without making `/health` execute a command or
granting trading authority.

## Formula and outcome matrix

The implementation parses chrony's documented tracking fields and computes:

```text
maximum_error = abs(system_time_offset) + root_dispersion + 0.5 * root_delay
```

The focused evidence exercises the exact threshold boundary and these outcomes:

| Input/result | Projected state |
|---|---|
| Normal leap state, non-local reference, bound equal to threshold | `SYNCHRONIZED` |
| Calculated bound above threshold | `UNSYNCHRONIZED/error_bound_exceeded` |
| `Not synchronised` | `UNSYNCHRONIZED/not_synchronized` |
| Local chrony reference | `UNKNOWN/local_reference` |
| Leap insertion/deletion status | `UNKNOWN/leap_status_uncertain` |
| Missing/duplicate/malformed/negative required field | `UNKNOWN/output_malformed` |
| Missing executable, timeout, nonzero exit, invalid encoding | typed `UNKNOWN` |
| Combined output exceeds 64 KiB | child stopped; `UNKNOWN/output_too_large` |
| Prior positive sample becomes stale or future-dated | scalar clock state becomes `UNKNOWN` |

The real child-process size test emits more than the ceiling and verifies the observer returns
`output_too_large`. The command-shape test verifies an absolute three-element argument vector,
`shell=False`, closed stdin, root working directory, and no operator command input.

## Cache and application observations

The cache advances its generation for every observation. The periodic monitor publishes after
its bounded interval. A real FastAPI lifespan test enables the provider with a fake local
chronyc result, observes one startup sample, makes 12 `/health` requests, and proves those
requests cause no additional command invocation. The response retains schema v2's scalar
`clock` and adds quantitative `clock_evidence`.

The authority boundary test scans order, execution, portfolio, supervisor, risk, broker,
service, control, and runtime code and refuses imports of both observation modules. The monitor
runs for read-only and writer lifecycles but is not a required service-readiness task.

## Host observation and scope

This development host has no `/usr/bin/chronyc` at evaluation time. That is not repaired or
worked around: if the provider were enabled here, the correct result would be
`UNKNOWN/binary_missing`. The default remains disabled and therefore `UNKNOWN/disabled`.
No live service, daemon, remote time server, broker, credential, capital, schema, or deployment
was touched.

Accordingly this is code-contract evidence only. It does not demonstrate synchronized
production time, choose an acceptable trading threshold, or prove that a clock failure blocks
the actual submission boundary. Those remain explicit follow-up work.

## Candidate verification

Final local verification after all implementation and documentation were present reported:

```text
make gates
ruff: All checks passed; 580 files already formatted
mypy: 302 Chronos source files and 10 worker files clean
pytest: 4472 passed, 1 skipped, 25 warnings in 184.24s
installed-wheel gate: PASS; migration head 0010, 34 model tables, 5 module entry points
CycloneDX 1.6 SBOM: valid, reproducible, 64 runtime components
```

The skip is the expected owner-opt-in read-only IBKR smoke test; no gateway was configured or
contacted. The warnings are existing Starlette/FastAPI and multiprocessing deprecations. The
exact candidate commit, hosted CI, non-author verdict, and post-merge exact-main evidence are
bound in the pull request and final handoff because a commit cannot contain its own hash.
