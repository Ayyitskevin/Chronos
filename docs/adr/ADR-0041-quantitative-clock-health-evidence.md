# ADR-0041 — Clock health is quantitative, cached chrony evidence

Status: **accepted design — observation-only continuation authorized by the owner on
2026-08-29. No clock threshold is chosen by Chronos and no trading authority changes.**
Index entry: DECISIONS.md D-55.

## Context

ADR-0040 deliberately reported clock state as `UNKNOWN`. Chronos uses wall-clock ages in
market-data, mandate, evidence, reconciliation, and operational-health checks, but it had no
automatic observation of the operator host's clock. A Boolean such as “NTP enabled” or “NTP
synchronized” is insufficient: it does not state the current uncertainty or a maximum error
that can be compared with an explicitly accepted bound.

The clock observer must not turn `/health` into a command runner, perform a remote time query,
install or configure a daemon, expose a peer name or address, or silently choose how much
clock error is acceptable. It also must not become a second authority surface: this slice is
diagnostic evidence, not a new order predicate.

## Decision

### 1. chronyd is the only supported provider, and disabled is the default

`CLOCK_HEALTH_PROVIDER` is either `disabled` or `chrony`, defaulting to `disabled`. Enabling
chrony requires the operator to set a positive, finite
`CLOCK_HEALTH_MAXIMUM_ERROR_SECONDS`. An error threshold set while the provider is disabled
is refused rather than ignored. Chronos does not install, start, configure, or repair chronyd.

The provider runs exactly:

```text
/usr/bin/chronyc -n tracking
```

The absolute executable and every argument are constants. `-n` prevents name resolution.
There is no shell, request input, command setting, network time query, or fallback provider.
Execution has a configured timeout with a 30-second hard ceiling, a 64 KiB combined
stdout/stderr ceiling enforced while reading the child pipes, a fixed root working directory,
closed stdin, and a minimal C locale.

### 2. The comparison uses chrony's documented maximum-error bound

The parser requires one each of `Reference ID`, `System time`, `Root delay`,
`Root dispersion`, and `Leap status`. Numeric inputs are finite nonnegative decimals. The
maximum error is:

```text
abs(system_time_offset) + root_dispersion + 0.5 * root_delay
```

Only exact `Leap status: Normal`, a non-local reference, and a computed maximum error at or
below the operator's threshold produce `SYNCHRONIZED`. `Not synchronised` or a bound above
the threshold produces `UNSYNCHRONIZED`. A local reference (`7F7F0101`), leap insertion or
deletion state, missing or duplicate field, invalid number, missing binary, nonzero exit,
timeout, oversized output, decoding failure, or unexpected exception produces `UNKNOWN`.
Nothing falls back to an optimistic Boolean.

### 3. Lifespan owns sampling; requests read only the cache

When enabled, both writer and read-only backends take one bounded sample during startup and
refresh it periodically in an independent lifespan task. The cache is thread-safe and advances
a generation on every completed attempt. A monitor failure publishes a typed, sanitized
`UNKNOWN` observation and keeps the operator service available.

`/health` and `/terminal/system` consume only a cache snapshot. They expose provider, evidence
freshness and age, the calculated and allowed maximum error, a closed failure code, and the
generation. They never expose raw output, stderr, peer hostname/address, reference ID, or an
exception message. Schema v2 keeps the existing scalar `observations.clock` field and adds
`observations.clock_evidence`; old clients therefore retain the original field type.

Freshness is evaluated separately from the provider result. A stale or future-dated
`SYNCHRONIZED` sample is projected as scalar `UNKNOWN` and cannot strengthen a capability
verdict. Service readiness does not depend on the monitor, so clock failure cannot remove the
inspection surface.

### 4. Observation remains outside authority

Order, risk, supervisor, broker, service, and runtime authority modules are structurally barred
from importing either operational-health or clock-observation modules. No admission, sizing,
transmit, cancel, or risk-reduction predicate consumes this cache. Deleting the observer removes
diagnostic evidence and changes no broker action.

That boundary is deliberate for this slice. Wiring clock state into permission to create
exposure is an authority change requiring its own owner-gated design and review. Until then,
the broader safety requirement that ambiguous clock state prevent new exposure is not fully
implemented even when the diagnostic projection reports it accurately.

## Consequences

Chronos can now calculate and display a bounded local clock-error observation when an operator
explicitly supplies a threshold and chronyd is available. Disabled and failure postures remain
honest `UNKNOWN`; a clock jump that makes the sample future-dated or stale also degrades to
`UNKNOWN`.

This does not prove the current host or any deployment is synchronized. It supplies no default
threshold, daemon management, off-host observer, alert delivery, watchdog, dead-man behavior,
or availability/RPO/RTO evidence. R-18 is therefore mitigated in code, not closed.

## Sources

- [chrony 4.8 `chronyc` documentation](https://chrony-project.org/doc/4.8/chronyc.html#tracking)
  — tracking fields, leap states, local reference ID, and the documented maximum-error formula.
- [Python 3.12 subprocess documentation](https://docs.python.org/3.12/library/subprocess.html#popen-constructor)
  — argument-vector process creation, pipe handling, and timeout behavior.
- [systemd 255 `timedatectl` manual source](https://github.com/systemd/systemd/blob/v255/man/timedatectl.xml)
  — the rejected qualitative system-clock status alternative.
