# External health probe evaluation — 2026-08-29

status: code-mitigated, operational evidence absent
scope: default-off one-shot consumer of `/health/live` and `/health/ready`
authority: none; observation cannot arm, mandate, reconcile, submit, cancel, or restart
open: scheduler/service configuration, authenticated tunnel or TLS policy, durable dead-man
state, off-host alert delivery, escalation policy, availability SLO, and real-host campaign

## Result

The implemented observer closes the repository-side consumption gap without claiming an
always-on monitor. It accepts only a plain credential-free HTTP(S) origin with a non-empty host
and valid port, ignores environment proxies, refuses redirects, applies a positive finite HTTPX
network-inactivity timeout to each of two exact endpoints, clears response cookies between them,
and never reads response bodies. Reports contain only the normalized origin, observation time,
status codes, elapsed time, closed state, and sanitized failure codes.

The focused pre-change operational-health baseline passed 45 tests. The new TDD test initially
failed at import because the observer did not exist, then 32 observer tests and four
authority-boundary tests passed after implementation. Full candidate and exact-main evidence is
recorded by the PR/CI history rather than predicted here.

## Interpretation boundary

Exit 0 means only that both endpoint status-code contracts answered 200 during this invocation.
Exit 1 includes both known unhealthy and unknown observations so ordinary schedulers alert on
either. Exit 2 is invalid local configuration. No result authorizes an order, and silence says
nothing: detecting observer silence requires independent retained state or supervision that this
slice deliberately does not implement. The invoking scheduler must also enforce an outer
wall-clock deadline because an HTTPX inactivity timeout is not a total-duration bound.
