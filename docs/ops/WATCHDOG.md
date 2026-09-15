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
| 3 | off-host sidecar | a copy of the heartbeat pulled to another host | as layer 2 | another host — **design only, not built** |

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
mis-typed unit). Catching the host itself needs the third layer: an off-host sidecar that
pulls `heartbeat.json` to another machine (read-only copy, e.g. `scp`/`rsync` on a timer)
and runs `chronos.operations.deadman` against the copy with a `--max-age` that allows for
the copy interval — and, because `CLOCK_MONOTONIC` does not travel between hosts, expects
`UNKNOWN` from the monotonic comparison unless the sidecar's checker is taught to trust the
wall clock alone across hosts. That teaching is a decision, not a patch; it is **design
only** tonight, and nothing here builds it.
