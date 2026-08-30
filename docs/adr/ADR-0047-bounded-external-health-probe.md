# ADR-0047 — External health observation is timeout-constrained and non-authoritative

Status: **accepted and implemented — owner authorized autonomous merge for this resumed
sequence, 2026-08-29. This adds observation only; it grants no trading capability.** Index
entry: DECISIONS.md D-61. Risk entry: RISK_REGISTER.md R-63.

## Context

ADR-0040 gave Chronos explicit `/health/live` and `/health/ready` status-code contracts, but
the repository still shipped no consumer that could run outside the backend process. A local
in-process task cannot prove that its own process, host, or route is reachable. A full alerting
sidecar would additionally require deployment ownership, a scheduler, durable dead-man state,
an authenticated transport, and an external notification destination. Those are operational
choices, not defaults the library can safely invent.

The smallest useful next seam is therefore a one-shot observer: something an operator-owned
scheduler can run locally, across an authenticated tunnel, or from another host. It must be
safe against surprising network responses and must never be mistaken for trading authority or
an always-on watchdog.

## Decision

`chronos.operations.external_probe` performs exactly two sequential read-only requests:

1. `GET /health/live`
2. `GET /health/ready`

Each request has a configured positive finite HTTPX timeout for connect, read, write, and pool
inactivity. This is not an overall wall-clock deadline. The observer disables environment proxy
discovery and redirect following, creates its client without auth or cookies, and discards any
cookie set by liveness before requesting readiness. It accepts only a plain absolute HTTP(S)
origin with a non-empty host, a valid port, and no userinfo, path, query, or fragment. It streams
only response headers and closes the response without consuming its body, so a peer cannot make
the probe buffer an arbitrary payload.

The output is one closed, machine-readable JSON document:

- `HEALTHY`: both endpoints returned 200;
- `UNHEALTHY`: liveness returned a 4xx/5xx response, or readiness returned its documented 503;
- `UNKNOWN`: timeout, transport failure, redirect, or any status outside the endpoint contract.

Exit status is 0 only for `HEALTHY`, 1 for `UNHEALTHY` or `UNKNOWN`, and 2 for invalid local
configuration. Exception details and response bodies are never copied into the report. The
origin is safe to report because credential-bearing origins are rejected before any request.

The module retains no state, retries nothing, restarts nothing, sends no alert, and does not
inspect the richer trading-capability diagnostic. It is structurally included in the existing
operational-projection boundary: order, supervisor, risk, broker, and runtime authority trees
may not import it.

## Consequences

Chronos now ships a deterministic external observation primitive that cron, systemd, a remote
runner, or a future alert sidecar can invoke without duplicating health semantics. The listener
remains loopback-only by default; any tunnel, remote exposure, TLS policy, service schedule,
retained state, notification channel, or escalation policy remains operator-owned and absent.

This is not a dead-man monitor: if the observer itself stops running, it emits nothing. It is
also not availability or trading proof. A slow peer that keeps making progress can outlive the
inactivity timeout, so an operator scheduler must impose the desired outer execution deadline.
DNS and the network path remain environmental, and HTTP protects no transport confidentiality
unless the operator supplies a trusted protected path.

## Verification

- `tests/unit/test_external_health_probe.py` exercises healthy, documented-not-ready,
  redirect, timeout, transport-failure, unsafe-origin, body-nonconsumption, JSON, and exit-code
  behavior with HTTPX's in-process mock transport.
- `tests/safety/test_operational_health_boundary.py` keeps the observer out of authority trees.

## Sources

- HTTPX, “Timeouts”: <https://www.python-httpx.org/advanced/timeouts/>
- HTTPX, “QuickStart” (redirect and streaming response behavior):
  <https://www.python-httpx.org/quickstart/>
- HTTPX, “Transports” (mock transport):
  <https://www.python-httpx.org/advanced/transports/#mock-transports>
