# Backup and Recovery

What to back up, how to do it safely with SQLite, and how to restore. ~~The design premise:
restore must never auto-resume trading, and the code guarantees it — a restored (or missing) halt
file reads as HALTED (`src/chronos/control/halt.py`), and submission is refused until
reconciliation passes again (`src/chronos/execution/engine.py`).~~

> **Corrected 2026-08-02 — that guarantee holds for the deterministic platform only.** It is
> true and unchanged for `chronos.execution`/`chronos.risk`: a restored or missing
> `data/platform_halt.json` reads as HALTED, and submission is refused until reconciliation
> passes. It is **not** true of the `chronos.orders` live plane, which is the plane that can
> place an order:
>
> - A **missing** `data/live_kill_switch.json` used to read as **DISENGAGED** — the opposite
>   default — so restoring a backup that omitted it came up with the emergency stop
>   **disarmed**. **Closed in code 2026-09-03 (R-66, ADR-0049)** for the case that matters:
>   the state directory carries a `state_generation.json` installation marker, and a
>   kill-switch file missing *after this installation wrote one* now reads **ENGAGED** until
>   an operator disengages with a note. A *corrupt* file still reads ENGAGED.
>   **Residual:** a restore that omits the whole state directory — marker included —
>   presents as a fresh install and still reads DISENGAGED, so the manual step below
>   remains the control for that case.
> - A **missing** `data/session_drawdown.json` used to re-establish the session baseline at
>   whatever the account was worth after the loss, reporting no breach for a drawdown that
>   had already happened. Same marker, same date: a baseline missing after this installation
>   established one now refuses and engages the kill switch instead of re-baselining.
> - A valid, account-matching `AUTONOMY_MANDATE_FILE` **auto-activates on boot** (ADR-0017),
>   so restoring one and starting the backend re-arms autonomy without any operator action.
>   Revocation, by contrast, survives restart.
>
> The vision plan's required end state is that recovery always boots kill-engaged,
> read-only, and unreconciled (`docs/VISION_COMPLETION_PLAN.md` §6, finding 3 —
> **partially closed 2026-09-03**). The *kill-engaged* half now holds for a lost or
> partially restored state directory (R-66, ADR-0049); **read-only and unreconciled on
> restore remain open**, as does the mandate auto-activation above, and a restore that
> brings back nothing at all still reads as a fresh install. Until those land, the manual
> step in the restore procedure below is the compensating control. Changing the code's boot
> defaults is a safety-mechanism modification requiring owner review, not a documentation
> fix.

## What to back up

| Path | What it is | Loss impact |
|---|---|---|
| `data/platform_ledger.db` | Platform order ledger (intents, transitions, fills) | Lose the platform's own order history; reconciliation can no longer explain broker state |
| `data/platform_halt.json` | Persistent halt state (deterministic platform) | Low (missing file fails closed to HALTED), but back it up to preserve the recorded reason/note |
| `data/live_kill_switch.json` | **Live order-plane kill switch** (`live_kill_switch_file`) — *added 2026-08-02, previously missing from this table* | **HIGH.** Since 2026-09-03 (R-66) a file missing while `state_generation.json` records that this installation wrote one reads as **ENGAGED**, so a partial restore fails closed. Restoring *neither* file still reads as a fresh install and comes up disarmed. Always restore both, or engage the kill switch again before starting the backend. |
| `data/state_generation.json` | **Installation marker** for the live-safety state directory (`chronos.orders.state_generation`) — records which state files this installation has ever written | **HIGH, and it must travel with the files it describes.** Restoring the marker without the state files is the fail-closed direction (the system boots kill-engaged); restoring the state files without the marker silently restores the old permissive reading. |
| your `AUTONOMY_MANDATE_FILE` (if configured) | Owner-authored autonomy grant — *added 2026-08-02* | Restoring it **re-arms autonomy on the next boot** (ADR-0017 auto-activation). Treat it as an authority document: back it up securely, and move it aside during any recovery you do not intend to resume trading from. |
| `data/platform_audit.jsonl` | Hash-chained audit trail | Lose the tamper-evident record of decisions and operator actions |
| `config/` | Risk policies (`risk.example.yaml` plus your local `risk.yaml`) | Lose the exact limits runs were made under; policy hashes in results become unverifiable |
| `specs/` | Canonical strategy specifications | Versioned in git, but back up local edits |
| `research/strategy_registry.yaml`, `research/strategy_catalog.*` | Pine corpus registry with pinned SHA-256 hashes | Lose corpus integrity verification (`verify-corpus`) |
| `research/pine/` | The audited Pine sources | Lose the audited corpus itself |
| `research/data/raw/` incl. `MANIFEST.json` | Historical data + provenance manifests | Lose reproducibility of research results |
| `data/chronos.db` | Wheel dashboard ledger (separate system, ADR-0003) | Lose wheel cycles/evidence/notes |
| `data/promotions/` (if you create it) | Promotion records | Lose the promotion paper trail |
| `.env` (store securely) | Your local configuration | Reconstructable, but back up to avoid mistakes on redeploy |
| your `deploy-freeze-*.txt` | Resolved dependency snapshot (docs/DEPLOYMENT.md) | Lose environment reproducibility (no lockfile exists) |

Code and most docs live in git; the list above is the state git does not hold.

## SQLite-safe backup

Both `data/platform_ledger.db` and `data/chronos.db` are SQLite. The platform ledger runs in WAL
mode (`PRAGMA journal_mode=WAL`, `src/chronos/execution/sqlite_ledger.py`), which means part of
the recent data lives in the `-wal` sidecar file until checkpointed.

**Preferred — online, consistent, works even while a process is running:**

```bash
sqlite3 data/platform_ledger.db ".backup 'backups/platform_ledger-$(date +%F).db'"
sqlite3 data/chronos.db          ".backup 'backups/chronos-$(date +%F).db'"
```

`.backup` produces a consistent snapshot regardless of WAL state.

**Acceptable — plain copy, but ONLY while every Chronos process is stopped:**

```bash
# All processes stopped. Copy the -wal and -shm sidecars too if present:
cp data/platform_ledger.db data/platform_ledger.db-wal data/platform_ledger.db-shm backups/ 2>/dev/null
```

Never plain-copy a live WAL database without its sidecars — you get a stale or torn snapshot.

The JSON/JSONL/YAML files (`platform_halt.json`, `platform_audit.jsonl`, configs, manifests) are
plain files; copy them normally. For the audit log, prefer copying while stopped, or accept that a
mid-append copy may end in a truncated last line (the verifier will point at exactly that line).

A complete cold backup, simplest form:

```bash
# all Chronos processes stopped
tar czf chronos-backup-$(date +%F).tar.gz data/ config/ specs/ research/ .env
```

Keep at least one copy off the machine. The audit log's tamper evidence is only as good as an
off-machine copy to compare against (docs/SECURITY.md).

## Automated isolated drill

The repository continuously exercises the file-level recovery contract against disposable state:

```bash
.venv/bin/python -m pytest -q tests/integration/test_backup_restore_drill.py
```

The drill keeps both real WAL-backed Chronos databases open, captures them through SQLite's online
backup API, restores them under `tmp_path`, and reopens them through `SqliteLedger` and `Database`.
It checks committed order/fill evidence, schema and scope, SQLite integrity, an engaged live kill
switch, a halted deterministic platform, and the hash-chained audit log. Negative cases prove that
an omitted or disengaged live kill switch, a rearmed platform, audit tamper, and database corruption
are rejected by the drill assertions.

This is repeatable integration-test evidence, not an operational backup system. The test itself does
not create an off-host or encrypted backup, emit recovery-time observations, inspect an owner
mandate, connect to a broker, reconcile positions/orders, or grant permission to start or rearm a
restored deployment. Follow the manual procedure below and treat those residuals as open.

## Recovery measurement campaign

The packaged recovery command turns the same bounded file-level contract into reusable evidence.
It has two deliberately separate steps so a snapshot can age before a later isolated restore:

```bash
# Preconditions: the parent directories exist; both output roots do not.
# Use a non-sensitive label. Do not put an account number or host path in source-id.
.venv/bin/python -m chronos.recovery capture \
  --source-data data \
  --snapshot-root /owner-controlled/chronos-snapshots/drill-2026-08-29 \
  --source-id paper-drill

.venv/bin/python -m chronos.recovery restore \
  --snapshot-root /owner-controlled/chronos-snapshots/drill-2026-08-29 \
  --restore-root /owner-controlled/chronos-restores/drill-2026-08-29
```

`capture` opens the source databases read-only and uses Python 3.12's SQLite online-backup API.
That API works while other clients access the source and creates a snapshot of each database as its
copy commences ([Python API](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup),
[SQLite semantics](https://www.sqlite.org/backup.html)). The databases are captured sequentially,
not atomically with each other or with the control files, so the manifest records a separate start
and completion timestamp for every artifact. The command refuses unless all five artifacts exist as
regular files, both databases pass integrity/current-version checks, the live kill switch is valid
and engaged, the deterministic platform is valid and halted, and the audit chain is non-empty and
intact. SQLite may update a live source database's transient `-shm` WAL-index while servicing the
read-only capture; the five named source artifacts remain byte-identical. A source directory must
therefore permit SQLite's ordinary WAL coordination. It copies no `.env` or autonomy mandate.

`restore` verifies every snapshot member against its manifest before creating the destination,
copies into `<restore-root>/data`, re-verifies every digest, opens the application schema through
Chronos's current schema checker, rechecks the control posture and audit chain, then writes
`<restore-root>/recovery-observation.json`. Both commands refuse an existing destination instead of
overwriting it. Directories are mode `0700`; artifacts and JSON evidence are mode `0600`. A failed
operation deliberately leaves any newly created partial directory in place for diagnosis; the
operator decides when it is safe to remove it. Successful snapshot and restored-data directories
contain only the five bound artifacts plus, for the snapshot, its manifest. Low-level integrity
checks open these new private copies as immutable read-only files; the application schema checker
separately opens `chronos.db` through Chronos, after which digests and exact contents are rechecked.
Successful bundles contain no unbound WAL sidecars.

The observation fields are measurements, not SLOs:

| Field | What it measures | What it does **not** prove |
|---|---|---|
| `oldest_snapshot_age_seconds` | Wall-clock time from the earliest per-artifact capture start to this restore attempt | Actual data loss, backup schedule compliance, or any RPO target; it is meaningful only after clock health is verified |
| `snapshot_capture_window_seconds` | Skew between the earliest artifact start and latest artifact completion | A transactionally atomic snapshot across the two databases and three files |
| `local_restore_copy_seconds` | Monotonic elapsed time to create the isolated restore directories and copy the five bound artifacts locally | Download/decryption/provisioning time |
| `local_verification_seconds` | Monotonic elapsed time for digest, database, control, and audit checks | Broker connectivity, order/position reconciliation, secrets, mandate review, or permission to rearm |
| `local_recovery_elapsed_seconds` | The sum of local copy and verification for this one run | Operational RTO, which also includes detection, human response, infrastructure, and broker reconciliation |

Retain the snapshot manifest and observation together, record verified clock status and the intended
RPO/RTO targets outside this command, and repeat on the actual recovery host with representative
state. A fast disposable or same-host run is capability evidence only. Phase 2's measured operational
RPO/RTO, encrypted/off-host backup, and external integrity anchor remain open.

## Restore procedure

1. **Stop everything.** No Chronos process may be running.
2. **Restore files** into place (`data/`, `config/`, `specs/`, `research/`). For SQLite files
   restored from `.backup` snapshots, just put the `.db` file in place; delete any leftover
   `-wal`/`-shm` sidecars from the dead instance so they cannot be replayed over the restored
   snapshot.
3. **The deterministic platform starts HALTED — by design.** Whatever the halt file says now
   stands; if the halt file was not restored, the missing file reads as HALTED (`NEVER_ARMED`).
   Nothing trades **in that plane**.

   **BEFORE starting the backend, do these three by hand** *(added 2026-08-02 — the live plane
   does not fail closed on its own; see the corrected premise at the top of this document)*:

   ```bash
   # a. Confirm the live kill switch exists. Missing AND this installation wrote one
   #    (data/state_generation.json says so) reads ENGAGED; missing with no marker
   #    either — a restore that brought back neither — reads DISENGAGED.
   ls -l data/live_kill_switch.json data/state_generation.json \
     || echo "MISSING → if the marker is gone too, the emergency stop is DISARMED"
   # b. Move any autonomy mandate aside so boot cannot auto-activate it.
   #    (Restore it deliberately, later, when you intend to resume.)
   grep -n '^AUTONOMY_MANDATE_FILE' .env || echo "no mandate configured — nothing to move"
   # c. Take a read-only state inventory before the process runs.
   python3 .claude/skills/chronos-diagnostics/scripts/state_inventory.py
   ```

   If the kill-switch file is missing, engage the kill switch immediately after the backend
   starts (`POST /live/kill`, see `docs/INCIDENT_RESPONSE.md`) and confirm the file appears —
   or start with live capability unconfigured until you have.
4. **Verify the audit chain:**
   ```bash
   python -m chronos.cli verify-audit-log
   ```
   `OK — chain intact (N records)` or a precise first-failure line. If it fails, treat it as an
   incident (docs/INCIDENT_RESPONSE.md) — a restore from an older backup legitimately has fewer
   records, but a broken chain within the restored file is corruption or tamper.
5. **Verify corpus integrity** (if research state was restored):
   ```bash
   python -m chronos.cli verify-corpus
   ```
6. **Reconcile against the broker** before any future submission window: broker open orders (by
   `orderRef`) and positions must be explained by the restored ledger
   (`src/chronos/execution/reconciliation.py`; queries in docs/OPERATIONS.md). A restore from an
   older ledger snapshot can legitimately produce `UNEXPLAINED_POSITION` or
   `UNKNOWN_BROKER_ORDER` discrepancies — resolve them at the broker and document them
   (docs/INCIDENT_RESPONSE.md playbooks).
7. **Only then rearm, explicitly:**
   ```bash
   python -m chronos.cli rearm --note "restore from backup <date>; audit chain OK; recon clean: <evidence>"
   ```

## Restore never auto-resumes trading — in the deterministic platform

*(Scoped 2026-08-02. This section was written for `chronos.execution`/`chronos.risk` and stated
as a repository-wide guarantee; it is not one.)*

For the **deterministic platform**, restoring from backup NEVER re-enables order generation on
its own. Fail-closed halt reads, the mandatory operator rearm with note, the mode lock's evidence
requirements, and the reconciliation gate each independently block it. If you ever observe a
restored deterministic platform generating orders without a fresh rearm, that is a critical bug —
halt, capture evidence, and stop using the build.

For the **`chronos.orders` live plane**, the equivalent claim is **not** true today and must not
be relied on:

| Restored artifact | Effect on boot |
|---|---|
| `data/live_kill_switch.json` present + engaged | Stop holds (correct). |
| `data/live_kill_switch.json` **absent**, `data/state_generation.json` restored | Reads **ENGAGED** — the marker proves this installation wrote one (R-66). |
| `data/live_kill_switch.json` **absent**, marker absent too | Reads **DISENGAGED** — indistinguishable from a fresh install; the manual step above is the control. |
| Valid `AUTONOMY_MANDATE_FILE` present | Autonomy **auto-activates** (ADR-0017). |
| Mandate previously revoked | Stays revoked across restart (correct). |

Live submission still requires the full ADR-0009 conjunction, a current session arm,
reconciliation, and the writer lease — so a restore alone does not place an order. But "the code
guarantees a restored system cannot resume" is false for this plane, and the manual steps in the
restore procedure above are what close the gap. Making recovery boot kill-engaged, read-only, and
unreconciled is open finding 3 in `docs/VISION_COMPLETION_PLAN.md` §6 and requires owner review.
