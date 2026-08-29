# ADR-0040 — Operational health is a conservative projection, never authority

Status: **accepted design — owner authorized autonomous merge for this resumed sequence,
2026-08-29. This adds observation only; it grants no trading capability.** Index entry:
DECISIONS.md D-54.

## Context

The unauthenticated `/health` response previously said only that the process had completed
lifespan startup. It could return `status: ok` while startup reconciliation had failed, a
lifespan-owned task had exited, the local store was unreadable, broker evidence was stale, or
new exposure was blocked. The terminal and Streamlit pages consequently lacked one shared
answer to three different questions: can this process answer, can it serve operator reads, and
what trading lanes are presently evidenced?

Those questions must remain separate. Kubernetes distinguishes liveness, readiness, and
startup because a liveness failure can restart a container while a readiness failure should
remove it from traffic without necessarily killing it. Chronos additionally needs a
lane-specific capability projection: a read-only backend should remain available to the
operator while every new-exposure lane stays blocked.

## Decision

### 1. One pure evaluator projects one immutable fact snapshot

`chronos.operations.health.evaluate_operational_health` performs no I/O and returns:

- `liveness`: whether the request-serving process is answering;
- `service_readiness`: whether local operator inspection is ready; and
- `trading_capability`: separate PAPER, LIVE, and autonomous new-exposure verdicts.

Each capability is `AVAILABLE`, `BLOCKED`, or `UNKNOWN`, with closed reason codes. Known
negative evidence blocks. Missing, future-dated, or stale evidence is unknown and can never
strengthen a verdict. Reasons are deduplicated and deterministically ordered.

The service-readiness projection depends only on process initialization, local-store
readability, retained startup faults, and lifespan tasks required of the current writer. A
broker outage, pending reconciliation, or missing writer lease blocks trading but does not
remove the operator's inspection surface.

### 2. Collection is bounded and remote-probe free

The FastAPI collector reads the local database, the reconciliation latch, sanitized task
observations, and a cached broker-connection observation. `/health` never calls the broker.
Production broker-status reads update that cache at their existing connection seam. Cache
invalidation advances its generation and erases prior positive connection evidence.

Lifespan tasks publish starting, running/progress, expected-stop, and closed failure states.
Exception text is never retained. Startup faults likewise retain only typed codes. Clock
state is explicitly `UNKNOWN` until a separately reviewed monitor supplies evidence.

### 3. Schema v2 is additive and preserves compatibility without endorsing it

`/health` keeps HTTP 200 and the seven prior fields so local clients do not break, but labels
the old `status` field `status_scope: compatibility_only`. The authoritative diagnostic
content is nested under liveness, service readiness, trading capability, and observations.
Because HTTP 200–399 is success to a Kubernetes HTTP probe, this diagnostic endpoint must
not be configured as an orchestrator readiness probe. A future orchestration integration
needs a dedicated status-code-bearing endpoint rather than overloading operator visibility.

The authenticated `/terminal/system` embeds the same projection. Streamlit pages lead with
service readiness and its reasons. A stale or unavailable terminal poll discards the cached
capability claim and renders it unknown.

### 4. The projection cannot grant authority

Order, risk, supervisor, broker, and runtime authority modules may not import
`chronos.operations.health`. They continue to derive permission from their own mandate,
lease, reconciliation, arming, kill-switch, quote, and deterministic gate inputs. No
authority decision consumes the projection's output; removing its diagnostic wiring does not
change any admission or send predicate.

## Consequences

Operators can distinguish an answering process from a ready inspection service and from an
evidenced trading lane. Startup and task failures remain visible after the initiating stack
frame or task has disappeared, and polling cannot create broker traffic. Existing clients
retain their old field types, but must migrate away from interpreting `status: ok` as trading
readiness.

This does not add an external clock monitor, process supervisor, watchdog, dead-man signal,
off-host alert, orchestrator probe, service-level objective, or operational campaign. It does
not establish that any lane is safe to use; W2 intentionally reports clock evidence as
unknown.

## Sources

- [Kubernetes, Liveness, Readiness and Startup Probes](https://kubernetes.io/docs/concepts/workloads/pods/probes/)
  — liveness and readiness have different recovery effects; probe results include Unknown.
- [Kubernetes, Configure Probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)
  — HTTP 200 through 399 counts as probe success.
- [Prometheus, Instrumentation](https://prometheus.io/docs/practices/instrumentation/)
  — online systems expose query/error/latency behavior, while batch/loop systems expose last
  progress and heartbeat time.
