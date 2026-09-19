# Watchdog and dead-man: evidence-only monitoring of the backend (M3 ops plane)

Two small tools that watch and write down what they saw. **Neither one acts.** They never
halt, kill, restart, arm or disarm anything, and they import nothing from the authority
packages — `tests/unit/test_ops_watchdog.py` pins that import graph in both directions.
When a verdict trips, the operator responds by the incident runbook; the tools hand over
evidence, not a decision.

| Layer | Tool | Watches | Trips when | Runs where |
|---|---|---|---|---|
| 1 | `python -m chronos.operations.watchdog` | the backend's `/health/live` and `/health/ready`, through the one-shot external probe | no HEALTHY observation for `--deadline` seconds (monotonic) | any host that can reach the backend |
| 2 | `python -m chronos.operations.deadman` | layer 1's own `heartbeat.json` | the heartbeat is absent, unreadable, malformed, not a regular file, or older than `--max-age` by both clocks | the same host (today) |
| 3 | off-host, receive-only sidecar | records the HOST PUSHES to it (it never pulls) | its own clock sees no HEALTHY observation for an outer deadline | a separately administered host — **design only, not built** (`DESIGN-alert-sidecar.md`) |

## What each layer proves, and does not

**Layer 1 proves** that, at every tick it recorded, the backend answered its two health
probes the way the line says — one JSON line per observation, append-only, plus a typed
verdict. It **does not** prove the backend is trading correctly, that the broker session
is sane, or anything about the order plane: it reads two status lines and nothing else
(the probe never consumes a response body). A `TRIPPED` verdict means "nothing HEALTHY for
the deadline", not "something is on fire" — the incident runbook decides what it is.

**Layer 2 proves** that layer 1 was still writing recently. It **does not** prove layer 1's
verdicts were right, and it cannot tell a dead watchdog from a watchdog on a host that
went away — which is the stated limit below.

**Both** keep no state between runs beyond the files they name. Layer 2 keeps none at all.

## The two files

`<evidence-dir>/watchdog.jsonl` — one object per line, appended, never rewritten:

```json
{"assessed_at": "2026-09-14T22:00:00+00:00", "state": "UNHEALTHY", "failure_code": "readiness_not_ready",
 "elapsed_ms": 12.4, "monotonic": 51234.117, "liveness_status": 200, "readiness_status": 503,
 "target_origin": "http://127.0.0.1:8000", "verdict": "HEALTHY"}
```

`state` and `failure_code` are the external probe's own values (`HEALTHY | UNHEALTHY |
UNKNOWN`; `readiness_not_ready`, `liveness_not_live`, `redirect_refused`,
`unexpected_status`, `timeout`, `transport_error`). `monotonic` is the writer's
`CLOCK_MONOTONIC`; `verdict` is the watchdog's state after this tick.

`<evidence-dir>/heartbeat.json` — replaced atomically on every tick (unique `O_EXCL` temp,
fsync, rename, directory fsync); a symlink, FIFO or directory already at that path is
refused, never replaced:

```json
{"last_healthy_at": "2026-09-14T21:58:30+00:00", "last_observed_at": "2026-09-14T22:00:00+00:00",
 "monotonic": 51234.117, "pid": 41230, "version": "0.1.0",
 "verdict": {"state": "TRIPPED", "reason": "no HEALTHY observation for 90.000 s (deadline 90.000 s); last HEALTHY at 2026-09-14T21:58:30+00:00",
             "since": "2026-09-14T21:58:30+00:00", "evidence_path": "data/ops/watchdog.jsonl"}}
```

The evidence log is opened by descriptor with `O_APPEND|O_NOFOLLOW|O_NONBLOCK` and must be a
regular file. If either file cannot be written, the tick raises and the process exits 3 —
a watchdog that cannot record is not a watchdog, and its now-stale heartbeat is exactly
what layer 2 trips on.

## Running them (nothing in this repository starts either one)

```bash
# layer 1: every 10 s, trip after 90 s without a HEALTHY observation; Ctrl-C to stop
python -m chronos.operations.watchdog --base-url http://127.0.0.1:8000 --interval 10 --deadline 90 --evidence-dir data/ops
# one tick only (exit 0 HEALTHY observation · 1 not HEALTHY but under the deadline · 2 TRIPPED · 3 evidence not writable)
python -m chronos.operations.watchdog --base-url http://127.0.0.1:8000 --evidence-dir data/ops --once
# layer 2: from cron or a timer, on the same host
python -m chronos.operations.deadman --heartbeat data/ops/heartbeat.json --max-age 180
# exit 0 ALIVE · 2 DEAD · 3 UNKNOWN · 64 bad parameters; the verdict is JSON on stdout
```

`--max-age` must be comfortably larger than `--interval` (a missed tick is not a death) and
the watchdog's `--deadline` larger than the probe's per-endpoint timeout.

## Clocks: why two, and what UNKNOWN means

The watchdog's trip decision reads only the monotonic timer, so an NTP step backwards can
never un-trip it; the wall clock is recorded so the step is visible afterwards. The dead-man
check reads both: the heartbeat's wall timestamp against its own wall clock, and the
heartbeat's monotonic value against its own timer. `DEAD` needs both to agree that the
heartbeat is stale. `UNKNOWN` (exit 3) is the honest answer when they cannot be compared:
the two disagree (a clock moved), the heartbeat's monotonic value is ahead of this
process's timer (another boot, or another host — `CLOCK_MONOTONIC` is only comparable
within one boot of one host), or the heartbeat is more than 5 s in the future. **UNKNOWN
is not ALIVE**; treat it as "go and look".

## What the operator does with TRIPPED, DEAD or UNKNOWN

The tools do not act. The response is the incident runbook's, starting at
[`../INCIDENT_RESPONSE.md`](../INCIDENT_RESPONSE.md) § "Immediate actions (any incident)":
confirm the state by hand (the one-shot probe, `python -m chronos.operations.external_probe
--base-url …`), then, if the platform must stop, use the halt/kill procedures that runbook
names — the control-plane halt file and the backend's kill route. Do not restart the backend
because a watchdog said so; a restart destroys the evidence the runbook's playbooks need.
`heartbeat.json`, `watchdog.jsonl` and the deadman's JSON output go into the incident's
evidence capture as they are.

## The stated limit, and the third layer

**A watchdog on the same host dies with the host.** Layer 2 on that host dies with it too,
and a host that loses power produces no DEAD verdict — it produces silence. Layer 2's value
is catching a watchdog that died while the host lived (OOM-killed, a stuck loop, a
mis-typed unit). Catching the host itself needs the third layer, and that layer is
**design only** tonight: [`DESIGN-alert-sidecar.md`](DESIGN-alert-sidecar.md) defines it
and nothing here builds it.

The one property the design fixes, restated here so this runbook can never drift from it:
the sidecar is **receive-only** and the host **pushes**. The host sends typed, signed records
(`watchdog.observation`, `watchdog.verdict`, `deadman.verdict`, the audit head) over HTTPS
to the sidecar with a write-only, per-host bearer token that can append records and do
nothing else; the sidecar holds the host's Ed25519 PUBLIC key and verifies every record.
The receiver **holds no Chronos-host credential** and cannot initiate a connection to the
host: there is no login, no file copy, no timer on the sidecar that reaches into the host,
and no endpoint on the sidecar that the host would obey. The sidecar **never pulls**. Its
dead-man decision uses its own clock — the `received_at` of the last observation whose
payload says HEALTHY — never a host-claimed timestamp and never the host's monotonic
value, which does not travel between hosts. Where the sidecar lives, who administers it,
and what its outbound alert channel is are Kevin's decisions (design §"Open owner asks").

## Restart, evidence entries, and the one thing a reader must tolerate

**A restart cannot turn an outage HEALTHY.** On start the watchdog reads the existing
`heartbeat.json`: a recorded `TRIPPED`, or a `last_healthy_at` already `--deadline` behind
the prior `last_observed_at`, starts the new process `TRIPPED` until a real HEALTHY
observation. A watchdog that has never seen HEALTHY at all publishes `TRIPPED` on a
non-HEALTHY tick — it has nothing to certify.

**A restart on the same boot keeps the original deadline.** The heartbeat records the
kernel's `boot_id` and the monotonic time of the last HEALTHY observation. When the new
process runs under the same boot and those monotonic values do not lie ahead of its own
timer, the deadline is anchored on that recorded monotonic value — the outage keeps
counting exactly where it was, and a wall clock that stepped backwards in between cannot
shorten it (the longest of the monotonic, wall and prior-file accounts wins). The deadline
is never rebuilt from wall time alone.

**A different boot starts TRIPPED.** When the `boot_id` differs, is missing, or the prior
monotonic values cannot be compared with this timer, continuity is unproven: the new
process starts `TRIPPED` and stays so until a real HEALTHY observation. A reboot therefore
never hands the backend a fresh grace period.

**A foreign entry at the heartbeat name is refused and left in place.** Publication is an
atomic envelope: an absent name is filled only if it is still absent; a present name is
swapped atomically with the new file and the displaced entry is judged before it is
dropped — only the heartbeat validated before the write is ever deleted. The validated
heartbeat is held open across the swap, so its inode cannot be freed and its number cannot
be handed to an entry planted in the window (a filesystem may otherwise reuse a just-freed
inode number immediately); the displaced entry must be a regular file with that pinned
identity to be dropped. A symlink, hardlink, FIFO or loose file that appears at the name in
between is swapped back, left in place, reported with a typed error, and nothing is
published; the operator sees the planted entry as evidence of tampering. Nothing is ever
deleted by name alone: every drop — the displaced heartbeat, our own temp on a refusal, our
withdrawn record — first captures the name atomically into a fresh private one and judges
that entry against the pinned descriptor; a foreign entry found there is moved back and
reported, never deleted. Linux has no unlink-by-descriptor, so the remaining window is a
planter guessing the private 16-hex name between two syscalls.

**Evidence entries are capabilities.** The evidence directory is reached component by
component without following links (a symlinked ancestor is refused and nothing is created
past it), its descriptor is kept, and every open, create, replace and fsync is relative to
it. An existing `watchdog.jsonl` or `heartbeat.json` must be a regular file owned by the
watchdog's user with exactly one link and mode 0600; a hardlink, a symlink, a FIFO, a
looser mode or a foreign owner is refused with a typed reason and nothing is written.
Every write is complete or refused — a short write is never published — and after each
publication the name is checked against the inode that was written.

**The writer's liveness is a kernel-released lock, and the dead-man reads it first.** The
watchdog holds `watchdog.lock` in the evidence directory (`LOCK_EX`, taken before its first
tick, a capability entry like the others) for the life of its loop, and the kernel releases
it on ANY exit — a clean stop, a raised tick, a crash, a kill. The dead-man has two inputs,
in this order: the **liveness lock** (probed with a non-blocking shared lock; if nobody holds
it, or the entry is absent, the verdict is `DEAD` — "no writer holds the liveness lock" —
**regardless** of how fresh `heartbeat.json` looks), then the heartbeat's **age** exactly as
before. Three rounds settled post-exchange failure branches one at a time; the lock closes the
class: whichever branch made a tick raise, the loop exits, the lock frees, and the second
layer sees it. The best-effort withdrawal of a vanished-displaced publication stays (our own
record is withdrawn where it can be, identity-bound), but no per-branch settlement is claimed
any more. The lock proves the loop's life only when the entry satisfies the **capability contract on both sides**: the writer will not take a foreign entry, and the dead-man will not read contention on a hardlinked, foreign-owned, loose-mode, non-regular or swapped entry as liveness — it reports `DEAD` naming the failed predicate. **One writer per evidence directory:** a second watchdog on the same directory is
refused with a typed error, so two loops can never publish over each other.

**The boundary that remains.** A writer that is *alive but wedged* still holds its lock, so it
is indistinguishable from a healthy one until `--max-age` elapses: the dead-man's max age is
the outage-detection bound for that case, and the operator picks it knowing that — short
enough to notice, long enough that a slow tick is not a death. A writer that *exits* after a
clean publication is `DEAD` at the very next dead-man check.

**The one thing a reader must tolerate:** if the disk stops accepting bytes mid-append,
`watchdog.jsonl` can end in a torn last line (the tick then exits 3 and no heartbeat is
published). Treat a final line that does not parse as "the writer died here", not as data.

## Objectives: what an SLO proves, and does not

`python -m chronos.operations.slo` is an **offline evaluator** over the two files above. An
operator declares objectives once, in a typed document, and the evaluator says per objective
whether the **recorded evidence** met it over the declared window. That is the whole claim:
**an SLO here proves compliance of what the watchdog wrote down, and nothing else.** The host
that died **records nothing** — its silence is the dead-man's boundary, not the evaluator's —
and a met objective is not a healthy trader, a sane broker session, or anything about the
order plane. The evaluator starts nothing, alerts nobody, and **changes no verdict**: it is
an observation (ADR-0040), the same tier as the watchdog and the dead-man, and the authority
packages are structurally barred from importing it (`tests/safety/test_operational_health_boundary.py`).
Nothing in this repository starts it; a timer or an operator does.

```bash
python -m chronos.operations.slo --evidence-dir data/ops --slo data/ops/slo.json [--pretty]
# exit 0 all MET · 2 any BREACHED · 3 any UNKNOWN and none BREACHED · 64 bad document
```

`slo.json` — every objective optional, a document declaring none is refused, every number
positive and finite. **The numbers below are placeholders** (OWNER-ASKS 9): Kevin's values
replace them. Until then, and on any host with no evidence yet, the evaluator prints
`UNKNOWN` and exits 3 — that is the correct finished state, not a failure.

```json
{"schema_version": 1,
 "probe_latency_p95_ms": 250, "readiness_availability_pct": 99.5, "window_s": 3600,
 "watchdog_deadline_s": 90, "deadman_max_age_s": 180, "clock_max_error_s": 5}
```

| Objective | Measured from | MET when |
|---|---|---|
| `probe_latency_p95_ms` | nearest-rank p95 of `elapsed_ms` over the lines within `window_s` | p95 ≤ budget |
| `readiness_availability_pct` | share of lines within `window_s` whose probe `state` is `HEALTHY` (an `UNKNOWN` tick is not availability) | share ≥ objective |
| `watchdog_deadline_s` | the longest monotonic stretch without a `HEALTHY` observation within the window, and whether any line carries a `TRIPPED` verdict | stretch < deadline and no `TRIPPED` |
| `deadman_max_age_s` | the heartbeat's `last_observed_at` age | age ≤ max |
| `clock_max_error_s` | the largest wall-versus-monotonic disagreement between consecutive lines within the window (a stepped clock shows here) | disagreement ≤ max |

`window_s` is required with the two windowed objectives; the deadline and clock objectives use
it when present and the whole log otherwise. **The document is validated against the shipped
cadence rules**, the same shape as the reconciliation and clock-health settings: `watchdog_deadline_s`
must be at least 3 × the cadence the writer actually kept (the median spacing of consecutive
`monotonic` values in `watchdog.jsonl` — a missed tick is not an outage), and
`deadman_max_age_s` at least 2 × `watchdog_deadline_s` (a missed tick is not a death). A
document that violates either is refused typed, exit 64, and publishes nothing.

`UNKNOWN` is the honest answer, never a guess: the log is absent, malformed, or spans less than
the window; a measurement needs two lines and has one; the heartbeat is absent, malformed or
from the future; an entry at either name is a symlink, FIFO, directory or device — refused
typed, never followed, never blocked on. Reads mirror the dead-man's (`O_NOFOLLOW` walk,
`O_NONBLOCK` open, `fstat` regular-file check, bounded: the newest 4 MiB of the log; a torn
last line is where the writer died). The overall state is BREACHED if any objective is, else
UNKNOWN if any is, else MET.

**The one observation `/health` shows.** Each run publishes its evaluation as
`<evidence-dir>/slo-evaluation.json` (unique `O_EXCL` 0600 temp, fsync, rename, directory
fsync; a symlink, FIFO or directory at the name is refused and left in place). When the
setting `ops_slo_evaluation_file` names that file, `/health` carries
`observations.slo: {evaluated_at, state, age_seconds, problem}` read from it — one bounded,
no-follow read per request, never the evaluator. The default is unset: no observation. The
field is an observation and **changes no verdict**: neither liveness, readiness nor trading
capability reads it (`tests/unit/test_ops_slo.py` pins that by AST and by behaviour). If the
cache cannot be published the CLI says so on stderr and a MET run exits 3 — unpublished is
UNKNOWN to `/health`.
