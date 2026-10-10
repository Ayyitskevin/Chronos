# Operations — Routine Procedures

Day-to-day operation of the deterministic platform. Companions: docs/IBKR_RUNBOOK.md (broker
procedures), docs/BACKUP_AND_RECOVERY.md, docs/INCIDENT_RESPONSE.md.

All commands assume the repository root as the working directory (the CLI's default
`--halt-file`/`--audit-file` paths are relative).

## Morning checklist

```bash
cd ~/Chronos            # wherever the repo lives
python -m chronos.cli status
```

Read the output deliberately:

1. **Halt state.** `HALT STATE | armed (not halted)` or `TRADING HALTED | reason: ... | detail`.
   If halted: find out why before anything else. Do not rearm reflexively — the reason and detail
   name the trigger (`src/chronos/control/halt.py` lists all reasons).
2. **Audit chain.** `status` verifies the hash chain and prints
   `audit log: VALID — chain + anchor intact (N records)` or a failure naming the first bad line
   or the head-anchor mismatch (`platform_audit.head.json`). A failure is
   an incident (docs/INCIDENT_RESPONSE.md), not something to shrug at. **Upgrade note (2026-09-12):**
   a log written before the head anchor existed reads `BROKEN — head anchor missing for existing
   audit log; owner bootstrap required` — and `verify-audit-log`, monitoring, `campaign status`,
   the service and recovery capture all refuse it — until you run, once, after reviewing the file:
   `python -m chronos.cli --audit-file data/platform_audit.jsonl bootstrap-audit-anchor`
   (exit 0 published / 1 refused / 2 absent). The full note is the 2026-09-12 entry in
   `CHANGELOG.md`.
3. **Mode banner.** For `status` the banner shows `MODE: RESEARCH | CAPABILITY: NO_ORDERS` and
   `LIVE TRADING | hard-disabled`. It reflects the command's own context — it is not a status
   readout of any running service.
4. **Data freshness** (when doing research work): check that the input files you plan to use are
   the ones you think they are — `research/data/raw/MANIFEST.json` records source and SHA-256 per
   file, and every backtest summary prints the `data_sha256` it actually loaded. At runtime the
   risk engine independently rejects stale data (`STALE_MARKET_DATA` when quote/bar age exceeds
   policy limits — and a zero limit denies by default, `src/chronos/risk/engine.py`).
5. If a gateway session is part of the day: gateway logged in, correct paper account shown, port
   listening (docs/IBKR_RUNBOOK.md sections 3 and 6).
6. **Clock evidence:** inspect `observations.clock` and `observations.clock_evidence` in the
   backend's `/health` response. `SYNCHRONIZED` is possible only when the chrony provider is
   explicitly enabled, a maximum-error threshold is configured, the sample is current, and the
   calculated bound is within it. `UNKNOWN` or `UNSYNCHRONIZED` is a stop-and-investigate result,
   not permission. This observer does not configure chronyd and is not yet an actual order gate;
   see ADR-0041 and R-18 before relying on it operationally.

## Backend process probes

The loopback backend exposes two unauthenticated, no-store machine probes:

```bash
curl -fsS http://127.0.0.1:8765/health/live
curl -fsS http://127.0.0.1:8765/health/ready
```

`/health/live` returns HTTP 200 when the request-serving process can answer and performs no
fact collection. `/health/ready` returns 200 only when local operator inspection is `READY`;
startup, an unreadable store, a retained startup fault, or a failed required writer task returns
503 with closed reason codes. A broker disconnect, pending reconciliation, absent writer lease,
or unavailable trading lane does **not** make operator inspection unready; those conditions remain
visible under `/health` and block the corresponding trading-capability verdict instead.

Keep these routes loopback-only. Do not use `/health` itself as a readiness probe: it deliberately
returns HTTP 200 while degraded so a human can read the full diagnostic. The probe endpoints grant
no trading authority.

Chronos ships a default-off, one-shot consumer for an operator-owned scheduler or remote runner:

```bash
python -m chronos.operations.external_probe --base-url http://127.0.0.1:8765
```

It prints one JSON report and exits 0 only when both exact endpoints answer 200, 1 for unhealthy
or unknown evidence, and 2 for invalid configuration. The URL must be a plain credential-free
HTTP(S) origin. The client ignores proxy environment variables, refuses redirects, applies its
HTTPX network-inactivity timeout per endpoint, sends no credential, and never consumes response
bodies. The invoking scheduler must impose its own outer wall-clock deadline. For an off-host run,
provide a separately managed authenticated tunnel or protected network path; do not widen the
backend listener merely for this command.

The command is an observation primitive, not a watchdog: it installs no service/timer, retains
no last-success state, detects no silence, restarts nothing, and sends no alert. ADR-0047 records
that boundary.

## Database startup refusals

> **In effect on `main` since #284 (merged 2026-10-03 EDT, 2026-10-04 UTC).** This section describes refusals added by
> the DBLOCK lock-drop fix to `Database`; a matching rule for the platform ledger (`SqliteLedger`)
> landed in #285. The fix stops `Database` from re-opening its own SQLite files after connecting (which silently
> dropped the connection's locks and let another process delete the live WAL). The first
> file-backed construction retains one startup mode-repair window; after it closes, later checks
> are `lstat`-only and refuse instead of reopening files. A refusal is a `RuntimeError` and can
> occur during construction or a later verification. Identity problems are refused before SQLite
> opens the unsafe named object, but admission may create a previously missing main file before a
> bad sidecar is found, and `initialize()` may write schema before its final verification refuses.
> Do not infer that every file is byte- or metadata-unchanged. Every message names the path.
> Never "fix" one by deleting `-wal`/`-shm` files.

> Before any action below that changes a database or sidecar pathname or metadata (`replace`,
> `move`, `chown`, `chmod`, `unlink`, or copy-then-`mv`), stop every Chronos process that may
> access that database, record the deployed `DATABASE_URL`/service arguments, and preserve the
> named object plus its sidecars as incident evidence. Live handling is inspection and read-only
> capture only. If every configured pathname cannot be identified, do not mutate; escalate.

| Refusal text starts with | Meaning | What the operator does |
|---|---|---|
| `Refusing symbolic-link SQLite path` | The database or a sidecar (`-wal`, `-shm`, `-journal`) is a symlink. Chronos never follows it. | Find out who created the link. Point `DATABASE_URL` at the real file, or replace the link with the real file while every Chronos process is stopped. Do not leave a symlink in the data directory. |
| `Refusing non-regular SQLite path` | The path is a directory, FIFO, device or socket. | Check `DATABASE_URL`; move the object aside. Treat an unexpected object there as suspicious (incident process). |
| `Refusing SQLite path not owned by this user` | The file belongs to another uid. | Run Chronos as the owning user, or, after confirming nobody tampered with it, change ownership as root. Chronos will not chown. |
| `Refusing SQLite path with unsafe mode NNNN` + `restart the process to repair` | Mode drift: the file or a sidecar is missing owner read/write permission or has any group/other permission (for example, after a restore, backup or `chmod -R`). The startup repair window closes at the process's first database construction, so a later construction refuses instead of repairing. | With every Chronos process that may access this database stopped, start it again: the startup window restores 0600 for files you own. If the refusal persists at startup, `chmod 600` the named file by hand and look for what keeps changing it. |
| `Refusing SQLite path with N hard links` | The database or a sidecar has more than one name (`cp -al`, `rsync --link-dest`, `ln`). SQLite keys `-wal`/`-shm` by pathname, so two names get two WAL files and acknowledged writes can fork between them. | **Stop; do not delete either name.** Copy both names and their sidecars to an incident directory, find the other names (`find <backup-root> -samefile <path>`), decide which one the backend actually wrote, and recreate it as a single-name file (`cp` to a new name, then `mv` into place) with every Chronos process stopped. If you cannot tell which name has the newest data, escalate: data may already have forked. Fix the backup tool so it copies instead of linking the live database. |
| same, naming a private temporary `.<db>.create-<hex>` | A crash during first creation of the database may have left that temporary linked to it. | With every Chronos process stopped, preserve the configured path, the named temporary and all sidecars; record the deployed `DATABASE_URL`/service arguments; re-check that the two names have the same device and inode and that no deployed service names the temporary. Only then unlink the exact temporary named by the refusal and restart. If any check is uncertain, do not unlink; escalate. |
| `restart the process to repair` (any refusal) | The repair window is closed in this process. | Restart the process. If the same refusal comes back at startup, use the row for its other text. |

The same table applies to the platform ledger (`data/platform_ledger.db`, `SqliteLedger`, #285): its
messages start with `Refusing symbolic-link ledger path`, `Refusing non-regular ledger path`,
`Refusing ledger path not owned by this user`, `Refusing ledger path with N hard links` and
`Refusing ledger path with unsafe mode NNNN`, carry the same `restart the process to repair` text, and
name a `.<ledger>.create-<hex>` temporary; one extra form, `Refusing ledger path …: repair failed (…);
chmod 600 it and restart`, means the startup repair could not open the file (for example mode 0200).

Hard-link backups of the live data directory are the usual cause of the `hard links` refusal: use
the stop-then-`.backup` or read-only procedures in [`BACKUP_AND_RECOVERY.md`](BACKUP_AND_RECOVERY.md#sqlite-safe-backup)
instead.

**Startup preflight (OPS-2).** `python -m chronos.cli db-preflight` reports, read-only, what the
backend's startup would do with the configured `DATABASE_URL`: the identity checks of the table
above (`lstat`, the database and its `-wal`/`-shm`/`-journal`), the mode (what the first
construction would repair to 0600 is a `WARN`; a file it could not even open, `Unable to secure
SQLite path without following links`, is a `REFUSE`), and `initialize()`'s schema checks read
through a WAL-aware read-only connection (`mode=ro`, never `immutable=1`, so a schema a crashed
writer committed only into the write-ahead log is seen; SQLite may create or recreate `-shm`/`-wal`
for that open — they are the runtime's own sidecars). Exit 0 `CLEAR` means **only** that none of the
checks it ran would refuse; the verdict line lists what it did not check (connect-time pragmas,
filesystem writes at create, the campaign preflight's mandate/registry/recovery hold, FU2 process
state, another process holding the database, and path stability between the check and the
backend's own open, which samples pathnames again). Exit 78 is a refusal with the exact text the
backend would raise (one exception: when the runtime's URL handling raises a `ValueError` other
than its static `file:` refusal, which is reported verbatim, the report is a fixed sentence plus the
exception class, never the parser's text);
exit 1 `UNDECIDED` means a check could not run (for example the read-only open failed, or the URL
handling raised some other exception, which is reported by class alone) and nothing is known. It never repairs: no chmod, unlink, create, migration or row. `--ledger
PATH` is opt-in and inspects a platform ledger with `SqliteLedger`'s rules; its refusal is the
platform process's, worded so, because the backend does not open that ledger. `--evidence` prints a
lower-bound estimate of the FU2 first-pass window (ticks = at least `max(ceil(rows / rows_per_tick),
ceil(bytes / bytes_per_tick))`; never exact, because ordered whole-row packing can need more). The
backend unit template (`docs/ops/chronos-backend.service`) runs the command as `ExecCondition=`, with
`Environment=PYTHONPATH=%h/<CHRONOS_CHECKOUT>/src` so it imports the same checkout source that
`ExecStart`'s `scripts/run_backend.py` shims in (without it, an uninstalled checkout fails the import
and exits 1, a silent skip); a
refusal **skips** the start — `systemctl --user status` shows `inactive` with `Result=success` and
the journal carries `Skipped due to 'exec-condition'` plus the preflight's lines. Read that shape as
the refusal; `Result=success` is not readiness. The template's `StartLimitIntervalSec=600` /
`StartLimitBurst=3` bound what the preflight cannot foresee, and that limit also counts manual
`systemctl start` attempts. Exit 1 `UNDECIDED` skips the start the same way (systemd skips on any
exit 1–254): an `inactive` unit whose journal shows `UNDECIDED` means the preflight could not decide
(the read-only open failed, or `DATABASE_URL` is not a file-backed SQLite database), not that the
backend refused. The configured `DATABASE_URL` is never printed. URL-parser failures,
database/ledger read failures, and unexpected exceptions are reported by class alone (never their
text, which can carry a query-parameter password, a DSN, or a fragment the URL parser echoes).
The runtime's exact static `file:` refusal sentence is the only parser text passed through;
runtime identity refusals intentionally retain their fixed wording plus the checked filesystem
path. The journal therefore carries the diagnosis without carrying the connection string.

**Reading the unit's three preflight outcomes.** The three exit codes are not interchangeable
(`src/chronos/cli/db_preflight.py`: `EXIT_CLEAR = 0`, `EXIT_UNDECIDED = 1`, `EXIT_REFUSED = 78`):

| Preflight exit | Unit shape | What it does and does not tell you |
|---|---|---|
| 0 `CLEAR` | `ExecStart` runs | Only that the checks it ran would not refuse. The verdict line lists what it did not check; a backend that then fails or boots degraded is a different problem (next section). |
| 78 `REFUSE` | skipped: `inactive`, `Result=success` | The configured database would be refused, with the text the backend would raise — except a `DATABASE_URL` for which the runtime's URL handling raises a `ValueError` other than its static `file:` refusal (reported verbatim), which gets a fixed sentence plus the exception class (any non-`ValueError` exception there is `UNDECIDED`, exit 1). Use the refusal table above. |
| 1 `UNDECIDED` | skipped the same way | A check could not run, so nothing is known — not "the database is bad". Read the `UNDECIDED` lines in the journal. |

**Import path.** `ExecCondition=` runs `python -m chronos.cli db-preflight` from the checkout
root, while `ExecStart` runs `scripts/run_backend.py`, which puts `src/` on `sys.path` itself
(`scripts/run_backend.py:14`). The `-m` form has no such shim, so the unit template sets
`Environment=PYTHONPATH=%h/<CHRONOS_CHECKOUT>/src`, which gives the condition the same source; it does
not need the `pip install -e .` step of [`DEPLOYMENT.md`](DEPLOYMENT.md#install) to find `chronos`.
The unit's `.venv` still needs the dependencies, and an `EnvironmentFile=` value for `PYTHONPATH`
overrides the template's. If the import still fails, Python exits 1; systemd treats that as a skip
too, so the unit looks like `UNDECIDED` while the journal shows an import error instead of any
`DB-PREFLIGHT` line. This is read from the template and the script, not reproduced: no unit was run
for this document. `inactive` alone, or the absence of a `DB-PREFLIGHT` line, does not establish an
import problem: a unit you stopped yourself, another unit
name, a journal window that misses the start, or journal access can each look the same. Read the
unit's `ExecCondition` result and the journal for that start attempt: `Skipped due to 'exec-condition'`
together with a Python import error (for example `ModuleNotFoundError`) points at the venv's
dependencies or a `PYTHONPATH` override; the
same skip with `DB-PREFLIGHT` lines is a verdict; if neither is visible, the cause is unknown.

## A running backend that refuses proposals: `503 AUTONOMY_NOT_RUNNING` (OPS-3)

> **In effect on `main` since #294 (merged 2026-10-07 UTC, merge `b7097da`).** A merge is not
> deployment evidence: this describes the pinned source, and a host runs it only after it has
> pulled and restarted. Nothing here was run for this document.

This is **not** the preflight skip above. A skipped unit is `inactive` and no backend exists. Here the
backend **is running**: `GET /health/live` answers, and the autonomy runtime was simply not started
at boot, because `build_autonomy_runtime` returned nothing or raised and the lifespan carried on
(`src/chronos/api/main.py`, the comment "a broken grant alerts and boots inert"), or was not called
at all under a recovery hold. A missing, invalid, mismatched, revoked or unsafe mandate, and an
unsafe proposer-registry file, therefore do **not** stop the backend from booting; they leave it up
with autonomy inert.

Two neighbouring conditions do **not** produce this refusal, because the route refuses earlier:

- **Recovery hold (ADR-0054).** The backend boots read-only and unreconciled. `require_writer`
  answers HTTP 409 ("This backend booted under a recovery hold and is running read-only … Acknowledge it
  with a note at POST /live/recovery/acknowledge and restart."). Acknowledging records a note that takes
  effect at the next start (the response says `effective: "next start"`); it does not release the
  hold in the running process, so the 409 continues until the backend is restarted. The acknowledgement
  itself does not activate a mandate, disengage the kill switch, arm or grant trading authority. The
  restart it requires is an ordinary start: it can auto-activate a configured, valid persistent mandate
  (ADR-0017), which the hold skipped. Neither step clears the kill switch, arming or the live gates;
  those keep their own acts.
- **Broken proposer registry** (configured but unreadable or invalid). `require_proposer` answers a
  different HTTP 503, `{"detail": "the proposer registry is configured but unreadable or invalid; proposals
  refuse until the owner repairs the file"}`, with startup fault `proposer_registry_invalid` (plus
  `authority_file_unsafe` for an unsafe file), the CRITICAL alert `autonomy.proposers_invalid` and the log line
  `Proposer registry is configured but unreadable or invalid; every proposal will refuse until the file is
  fixed`. Autonomy itself may still be running. `AUTONOMY_EVIDENCE_BUNDLES` set without a registry is a third
  case with its own refusal code, `EVIDENCE_POSTURE_INVALID`.

**What a caller sees.** `POST /autonomy/proposals` answers HTTP 503 with the body
`{"accepted": false, "stage": "INGRESS", "refusal": "AUTONOMY_NOT_RUNNING", "detail": "…"}` and
writes nothing. The guard runs after authentication and the writer check and before the request
body is read (`src/chronos/api/routes/autonomy.py`, `submit_proposal`). The current `detail` is the fixed sentence
`autonomy is configured on this backend but did not start; proposals are refused until the owner
fixes the configuration and restarts. The owner can see why in GET /health (startup faults),
GET /autonomy/alerts (unacknowledged owner alerts; local API token) and the server log.` It
carries no fault
code, path or exception text, so the body alone cannot name the cause. A backend that is not the
writer, or that is under a recovery hold, refuses earlier through `require_writer` with its own
error rather than this one.

**When it applies.** `BackendState.autonomy_admission` is settled once per process by the lifespan: the
runtime started (`RUNNING`, accepts), no `AUTONOMY_MANDATE_FILE` configured at all
(`NO_MANDATE_CONFIGURED`, accepts: 202, queued, nothing will act on it — the documented inert
posture), or a mandate file is configured and autonomy did not start (`CONFIGURED_NOT_STARTED`,
refuses). Before settling it is closed. The state is not re-evaluated, so a fix needs a restart.

**Finding the cause.** The journal commands below assume the user unit from
`docs/ops/chronos-backend.service` was copied in under that name (the template is not installed by
this repository). The sentence points at `GET /health`, but `/health` cannot show every cause: it
lists only the typed `observations.startup_faults` below, and some refusals record none. Correlate
three sources:

| Cause | `/health` `observations.startup_faults` | Owner alert `kind` | Server log (`journalctl --user -u chronos-backend`) |
|---|---|---|---|
| mandate file missing, or present but invalid JSON/schema | none | `autonomy.mandate_invalid` (CRITICAL) | `Autonomy mandate file … does not exist` / `… does not validate; autonomy stays inert` |
| mandate scoped to a different account | none | `autonomy.mandate_invalid` | `Persistent mandate is scoped to a different account; autonomy stays inert` |
| mandate file unsafe: symlink, group/other-writable, foreign owner | `authority_file_unsafe` | `autonomy.mandate_unsafe` (CRITICAL) for the mandate | `An owner-authored grant is unsafe; autonomy stays inert: …` |
| this mandate version was revoked | none | `autonomy.revoked_mandate_present` (WARNING) | none beyond the alert |
| submitting-mode mandate on a static proposer posture | `autonomy_posture_unauthenticated` | `autonomy.posture_unauthenticated` | `A submitting mandate met a static proposer posture; autonomy stays inert …` |
| any other assembly exception | `autonomy_wiring_failed` | none found in source | `Autonomy wiring failed; the backend continues without it` (with traceback) |

The recovery hold (`recovery_unverified`; log `This boot cannot prove it does not follow a restore (…)`) and
a broken proposer registry have their own refusals and are described above, not in this table.

A `startup_faults: []` reading with refused proposals is therefore possible (missing, invalid,
mismatched or revoked mandate) and means the alert, not `/health`, holds the reason. Likewise
`GET /health/ready` can stay 200 in those cases and goes 503 (`startup_degraded`) only for the rows
that record a fault, so a green readiness probe does not show that proposals will be accepted. Read the
unacknowledged alerts from `GET /terminal/alerts` (not writer-gated; needs the terminal credential)
or `GET /autonomy/alerts` (local API token **and** the writer lease). Do not rely on
`AUTONOMY_ALERT_FILE` for these: its sink is attached when the autonomy runtime is assembled
(`autonomy_wiring.py`, `sinks=delivery.default_sinks(…)`) and delivered by that runtime's tick, so
for a runtime that never started the alert is a database row first. That is read from source, not
observed; the file may stay empty. If the cause is neither a fault nor an alert, the server log is
the only record.

**What to do.** Fix the named cause in the owner's files (the mandate, registry or environment —
nothing on this page changes them), then restart the backend so the admission state is settled
again, and confirm with the §5 checks of [`SHADOW_CAMPAIGN.md`](SHADOW_CAMPAIGN.md#5-the-daily-owner-check).
Do not restart to "clear" a refusal whose cause you have not read.

## Evidence-stream verification refusals and alerts (FU2)

> **In effect on `main` since #290 (merged 2026-10-04 UTC), only when `AUTONOMY_EVIDENCE_BUNDLES` is set.**
> The drain no longer verifies the account's whole hash-chained evidence stream on every resolve. A
> verification pass runs inside the autonomy tick, one chunk per tick bounded by both
> `AUTONOMY_EVIDENCE_PASS_ROWS_PER_TICK` (default 1000 rows) and `AUTONOMY_EVIDENCE_PASS_BYTES_PER_TICK` (default
> 1048576 payload bytes) inside one SQLite snapshot, and publishes a verified head; each resolve reads one
> bounded statement, requires the published head to retain its sequence and digest, verifies the bounded
> suffix above it, and answers. Rows below the published head are rechecked by the next pass, not by the
> resolve. Every refusal below is `EVIDENCE_BUNDLE_EXPIRED` in the journal; the detail
> text says which. None of them is cleared by editing the database.

| Refusal detail starts with | Meaning | What the operator does |
|---|---|---|
| `the account's durable evidence stream has not been verified since this process started` | Normal after every process start: the first pass has not completed. A long stream needs several ticks (each tick verifies at most 1000 records and 1048576 payload bytes at the defaults, whichever bound binds first). | Wait: at the defaults a 10 000-record stream needs at least about ten ticks, and more when the byte bound binds. Proposals drained meanwhile are refused and journaled as `EVIDENCE_BUNDLE_EXPIRED` with this detail; they are not retried. |
| `the account's verified evidence state is older than its verification bound` | The published state is older than `AUTONOMY_EVIDENCE_VERIFICATION_MAX_AGE_SECONDS` (default 900 s) and the next pass has not completed. | Wait one pass. If it persists, the pass is aborting: look for `evidence.pass_failed` alerts. |
| `the account's evidence stream has grown past the verified state by more than the per-resolve bound` | More than `AUTONOMY_EVIDENCE_RESOLVE_ROWS` (default 1000) records were appended since the last pass. | Wait for the pass to catch up. A stream that outgrows every pass needs larger per-tick row/byte bounds after capacity review (`AUTONOMY_EVIDENCE_PASS_ROWS_PER_TICK`, `AUTONOMY_EVIDENCE_PASS_BYTES_PER_TICK`), or a slower proposer. |
| `the process's monotonic clock reading is unusable` | The host's monotonic clock went backwards or non-finite. | Treat as a host fault; restart the process after checking the host clock. |
| `the account's durable evidence stream failed verification (…)`, or a detail naming a durable record that does not decode or is not the exact expiry shape | Corruption detectable from the stream itself (a gap, a broken link, a digest mismatch, an undecodable or malformed record). The verdict clears on the next clean pass, which will not come while the corruption remains. | **Stop; do not edit the stream.** Preserve the application database (`DATABASE_URL`) and its sidecars read-only, then follow INCIDENT_RESPONSE, "Audit-chain verification failure": the same tamper-or-corruption rules apply to this stream. |
| `the account's durable evidence stream no longer holds, with its digest, a record this process verified` (**latched**) | A record the running process had verified is missing, moved or rewritten. This is the one state a restart clears, and it is also exactly what an attacker who truncated the stream would want you to do. One CRITICAL alert `evidence.stream_truncated` was raised when it latched. | **Do not restart to clear it.** Preserve the database and sidecars first, compare the stream with your last backup, and explain the missing or changed record. Only then restart; the first pass after the restart will certify whatever the stream then holds. |
| `this database has one shared connection (an in-memory engine)` | Only an in-memory database: it cannot hold a pass snapshot across ticks. | Not a production state; a file-backed `DATABASE_URL` does not produce it. |

**Alerts.** `evidence.stream_truncated` (CRITICAL, raised once when a stream latches) and `evidence.pass_failed`
(WARNING, once per aborted pass). After `AUTONOMY_EVIDENCE_MAX_PASS_ATTEMPTS` (default 5) consecutive aborted
passes the stream stops retrying and refuses until a restart; the WARNING alerts tell you why each pass aborted
(the pass aborts on a record whose payload exceeds `AUTONOMY_EVIDENCE_ROW_BYTES`, default 4096 bytes, or when
more than `AUTONOMY_EVIDENCE_EXPIRED_IDS`, default 10000, expired bundle ids would have to be retained, as well
as on detectable corruption). Neither alert stops the runtime: proposals refuse per stream.

**What the proof does not cover** (RISK_REGISTER R-83): records this process has never verified, which after a
restart is every record. A stream truncated between a stop and the next start is certified as it then stands.
The read-only backup procedure in [`BACKUP_AND_RECOVERY.md`](BACKUP_AND_RECOVERY.md#sqlite-safe-backup) is
what lets you compare.

## Shadow scan (after market close)

> **Not the autonomy SHADOW campaign.** This is the deterministic platform's one-shot
> after-close scan. The autonomy stack's unattended campaign, its daily owner check and
> its stop conditions are in [`SHADOW_CAMPAIGN.md`](SHADOW_CAMPAIGN.md).

For the daily-bar strategies, the shadow workflow is a one-shot scan after the close
(`src/chronos/research/shadow.py`, `cmd_shadow_scan` in `src/chronos/cli/main.py`):

```bash
python -m chronos.cli shadow-scan            # defaults: both strategies, the six candidate ETFs
```

It runs the production decision path (strategy → sizing → risk engine) over the latest closed
bars and reports the proposal, the sized would-be intent, and the full risk decision per
(strategy, symbol). Nothing can be submitted: the SHADOW lock is `NO_ORDERS` and the module never
constructs a broker adapter. Every report is appended to the audit log as a `shadow_scan` record,
so the scan history is part of the tamper-evident trail. Symbols without a data file, or with
blocking data-quality issues, are reported as skipped rather than silently ignored.

## Platform monitor (read-only)

The monitor is a read-only view over persisted platform state — halt store, audit log, risk
policy, market-data files, and (optionally) the execution ledger. It **imports no broker adapter,
opens no market-data connection, and exposes no control that can arm, halt, or submit** (a unit
test asserts the no-broker-import guarantee). It is exactly as trustworthy as the files on disk.

Terminal render (`cmd_monitor` in `src/chronos/cli/main.py`):

```bash
python -m chronos.cli monitor --mode shadow \
  --policy config/risk.example.yaml --data-dir research/data/raw --symbols SPY,QQQ \
  --ledger data/platform_ledger.db      # --ledger is optional
```

Localhost Streamlit page (`src/chronos/monitoring/streamlit_app.py`), configured by environment
variables so the page stays a pure function of files on disk:

```bash
CHRONOS_MONITOR_MODE=shadow CHRONOS_LEDGER_FILE=data/platform_ledger.db \
  streamlit run src/chronos/monitoring/streamlit_app.py
```

It surfaces: operating mode and live-lock capability (the paper/live distinction is shown by an
explicit text banner and a boolean `live_capable`, **never by colour alone**), halt reason,
reconciliation outcome (from the last `service_startup` audit record — only while the audit
chain verifies VALID; a BROKEN or ABSENT chain reads `unverified (audit chain BROKEN|ABSENT)`
and no audit rows are listed, because nothing derived from an unverified chain may read as a
verified state; the verdict is the log+anchor pair's, reached through the same capability read
as `verify-audit-log` — one no-follow, exact-0600 read of the anchor and one of the log — and
the rows come from that same read of the log, so a file replaced mid-snapshot cannot pair a
VALID verdict with rows the verifier never saw, and a truncated log, stale anchor or exposed
file is BROKEN here exactly as at the CLI; the row count is kept as forensic telemetry and
labelled `parsed rows, unverified` whenever the chain is not VALID), audit-chain integrity,
market-data freshness, the active risk limits, code commit, and — when a ledger is supplied —
open orders, fill-derived net positions, and recent fills. Realized/unrealized P&L is **not**
reconstructed here: this build runs SHADOW with a flat account and submits nothing, so those rows
are empty by construction and the monitor says so rather than printing a fabricated zero.

## Running a backtest reproducibly

```bash
python -m chronos.cli backtest --strategy regime_trend_v1 --symbol SPY \
  --data-dir research/data/raw --policy config/risk.example.yaml \
  --cash 3000 --slippage-bps 2 > runs/regime_trend_v1_SPY_$(date +%F).json
```

The JSON summary is the reproducibility record (`src/chronos/research/runner.py`). It contains:
`strategy`, `strategy_version`, `symbol`, `bars`, `date_range`, `data_sha256`,
`data_quality_issues`/`data_quality_blocking`, `policy_version`, `policy_hash`, `code_commit`,
`config` (cash, slippage), `risk_rejections`, `skipped_conversions`, and `metrics`. Two runs with
the same code commit, data hash, and policy hash produce identical results — if they do not, stop
and treat it as a bug.

Notes:

- The backtest uses its own throwaway halt file, `data/backtest_halt_<strategy>_<symbol>.json`,
  which it arms itself. It never touches `data/platform_halt.json`.
- The example policy denies everything by design; a backtest under it reports rejections rather
  than trades. Copy `config/risk.example.yaml` to `config/risk.yaml` and grant limits deliberately
  for research runs. `.gitignore` excludes `config/risk.yaml`, so local limits are not committed
  by default (a policy holds no secrets, only limits, but the file is still local-only by design).

## Reading the platform ledger

`data/platform_ledger.db` is plain SQLite (`src/chronos/execution/sqlite_ledger.py`). Read it with
the standard shell; treat it as read-only evidence — never UPDATE/DELETE (the schema itself is
append-oriented; nothing in code updates or deletes rows).

Tables:

| Table | Contents |
|---|---|
| `schema_info` | single row, schema `version` (currently 1) |
| `intents` | one row per order intent: `intent_id` (PK), strategy id/version, symbol, side, quantity, limit/stop price (TEXT decimals), tif, decision timestamp, source bar, reason, `initial_status`, `created_at_utc` |
| `transitions` | insert-only status history: `intent_id`, `status`, `at_utc`, `evidence` |
| `fills` | insert-only fills: `intent_id`, `cumulative_quantity`, `average_price`, `commission_usd`, `at_utc` |

Useful queries:

```bash
sqlite3 -readonly data/platform_ledger.db "
  SELECT t.intent_id, t.status, t.at_utc
  FROM transitions t
  JOIN (SELECT intent_id, MAX(id) m FROM transitions GROUP BY intent_id) x ON x.m = t.id
  ORDER BY t.at_utc DESC LIMIT 20;"                      # latest status per intent

sqlite3 -readonly data/platform_ledger.db "
  SELECT intent_id, cumulative_quantity, average_price, commission_usd, at_utc
  FROM fills ORDER BY id DESC LIMIT 20;"                 # recent fills

sqlite3 -readonly data/platform_ledger.db "
  SELECT * FROM transitions WHERE intent_id = '<uuid>' ORDER BY id;"   # one order's history
```

An intent whose latest transition is `SUBMITTED`/`PRE_SUBMITTED`/`ACKNOWLEDGED`/
`PARTIALLY_FILLED`/`PENDING_CANCEL` is "working" — reconciliation compares exactly that set
against broker open orders (`SqliteLedger.working_intent_ids`).

## Log locations

| What | Where |
|---|---|
| Platform audit trail (hash-chained) | `data/platform_audit.jsonl` |
| Platform order ledger | `data/platform_ledger.db` |
| Platform halt state | `data/platform_halt.json` |
| Backtest throwaway halt files | `data/backtest_halt_*.json` (safe to delete when idle) |
| Wheel dashboard rotating log | `logs/chronos.log` (`LOG_FILE` setting) |
| Wheel dashboard ledger | `data/chronos.db` (`DATABASE_URL` setting) |
| Autonomous option receipts | account-scoped `autonomy.option-selections` hash-chain stream in the Wheel database |
| Autonomous owner-alert file (when configured) | `AUTONOMY_ALERT_FILE` (default `data/owner_alerts.jsonl`) |
| Platform notifications | logger `chronos.notifications` (console/log only; no external channel is implemented) |

## Autonomous option receipt inspection (ADR-0030)

This section belongs to the live-wheel/autonomy backend rather than the
deterministic strategy platform described above. Inspect option-selection
history through the authenticated terminal or bounded
`GET /terminal/option-selections`; v1 intentionally ships no option-replay CLI.
The view reports the full account-scoped hash-chain result separately from
whole-stream semantic validity (every envelope replays and each decision ID is
unique), plus verification for each returned canonical receipt. A truncated
page never turns an earlier chain break or duplicate decision into a
valid-looking tail. Invalid receipt text above the inspection byte bound is not
echoed back; neither are oversized, malformed, deeply nested, noncanonical, or
invalid-storage sequence/time/kind/payload/hash fields. The entry retains typed
invalidity detail instead. Full-history semantic inspection streams one
SQL-bounded row at a time while retaining at most the newest 25 receipts and
decision IDs. Exact duplicate detection retains one bounded decision ID per
historical receipt, so its memory grows with stream length even though receipt
bodies and driver batches do not.

Treat any invalid chain/receipt, duplicate decision receipt, status/digest/time
mismatch, or `option_selection.system_failure` alert as a stop condition. Do not
edit the SQLite rows or regenerate a digest. Preserve the database, engage the
kill switch if live authority exists, and investigate the first invalid record
and its source evidence. Ordinary candidate-economics refusals remain visible as
typed `NO_TRADE` receipts but do not raise a system alert. Missing, conflicting,
unknown, identity-invalid, stale/future, and source-quality evidence does raise
that deduplicated alert; numeric misses for DTE, moneyness, delta range, spread,
volume, or open-interest floors do not.

`ENABLE_AUTONOMY_OPTION_SELECTION` defaults false. It enables evaluation only;
it does not create live authority. A live resolver-promotion artifact is a
separate owner action for exactly one CANARY/LIVE autonomy mode, and Chronos has
no command that creates it. No artifact is shipped by ADR-0030. Real IBKR will
continue to record `NO_TRADE` until an authoritative deliverable source exists.

## Halt / rearm discipline

- Anyone (any component) halts; only you rearm. `python -m chronos.cli halt --reason "..."` is
  always safe to run and is the first move in any incident.
- Rearm requires a non-empty note: `python -m chronos.cli rearm --note "..."`. The note is your
  audit trail — write what happened, what you verified, and why it is safe now. An empty or
  whitespace note is rejected (`HaltStore.rearm`).
- Rearming clears the halt only. Order generation additionally requires mode capability and a
  passed reconciliation (the CLI prints this reminder after rearm).
- A missing/corrupt halt file is HALTED, not armed. Restoring from backup therefore never
  silently resumes trading (docs/BACKUP_AND_RECOVERY.md).

## Promotion-record workflow

Promotion between modes is evidence, not a switch (`src/chronos/control/promotion.py`). There is
no CLI subcommand for it in this build; the operator drives it from Python:

```python
from pathlib import Path
from chronos.control.modes import TradingMode
from chronos.control.promotion import GateCheck, write_promotion_record

record = write_promotion_record(
    Path("data/promotions/2026-07-17-backtest-to-replay.json"),
    current_mode=TradingMode.BACKTEST,
    proposed_mode=TradingMode.REPLAY,
    code_commit="<git rev-parse HEAD>",
    strategy_versions={"regime_trend_v1": "1"},
    risk_policy_version="...", risk_policy_hash="...", config_hash="...",
    checks=[GateCheck(name="backtests_reproducible", passed=True, detail="...")],
    known_limitations=[...], outstanding_incidents=[...],
    owner_approval="<your name, date>", rollback_plan="<how you go back>",
)
print(record.all_gates_passed)
```

Rules enforced by `evaluate_promotion`:

- **Single-step only.** RESEARCH → BACKTEST → REPLAY → SHADOW → PAPER, one step at a time; a
  skip appends a failing `single_step_promotion` check.
- **Live is refused.** A proposed `CANARY_LIVE` or `LIVE` appends a failing
  `live_capability_hard_disabled` check unconditionally: promotion into those modes requires a
  future reviewed release plus explicit owner approval, not a record.
- Writing a record never changes the running mode. After a fully-passed record, you reconfigure
  the requested mode yourself, and the mode lock re-derives capability from live evidence at the
  next resolution (ADR-0007).

Keep promotion records with your backups; they are the paper trail of why the system was allowed
to do more.
