# Restore drill — measured RPO / RTO for the Chronos sqlite database

One command backs up `data/chronos.db` consistently, restores it into an **isolated**
directory, verifies the copy, and prints two measured numbers. This page is the operated
procedure: the harness runs exactly these steps, so an operator can do them by hand and
get the same verdict.

The harness: `src/chronos/operations/restore_drill.py` · tests:
`tests/unit/test_ops_restore_drill.py`. The whole-data-directory snapshot
(`python -m chronos.recovery`, [`../BACKUP_AND_RECOVERY.md`](../BACKUP_AND_RECOVERY.md))
is the sibling procedure for a full recovery; this drill is the database-only check with
per-table verification and a backup-time RPO. It builds on the isolated drill in
`tests/integration/test_backup_restore_drill.py` (the online backup API over the real
WAL-backed stores, which proves integrity and the fail-closed posture, not RPO/RTO).

## The one command

```bash
BROKER_MODE=demo ALLOW_ORDER_TRANSMIT=false ALLOW_LIVE_TRADING=false \
python -m chronos.operations.restore_drill \
  --db data/chronos.db \
  --out /var/backups/chronos/$(date -u +%Y%m%d) \
  --restore-into /tmp/chronos-restore-$(date -u +%Y%m%dT%H%M%SZ) \
  --pretty
```

- exit `0` and `"verdict": "VERIFIED"` — the restored copy is byte-identical to the backup,
  its schema head is the one the backup recorded and the repository accepts it, every
  table's row count matches, and the audit chain (when one travelled with the backup)
  verifies.
- exit `2` — `"verdict": "FAILED"` with `failures[]` naming the verifier that spoke, or a
  `refused:` line (the source was not a regular file, the target was not empty, …). Nothing
  was written to the source in either case.

The manifest is written beside the backup as `<backup>.manifest.json`.

## What the two numbers are — and are not

- **`rpo_s`** — `snapshot_completed_at − max(audit log's last entry at_utc, newest row
  timestamp among tables that carry one)`, where `snapshot_completed_at` is the instant the
  backup API returned (recorded once) and BOTH evidence sources are read **from the retained
  snapshot** — the backup file and the retained audit copy — never from the live database,
  which keeps moving after the backup. `rpo_basis.source` names which one won
  (`audit_log:last_entry.at_utc` or `table:<table>.<column>`). It measures how far behind
  the newest retained fact this backup is; it is **not** a recovery-point objective and says
  nothing about backups you did not take. A snapshot with no timestamped evidence reports
  `rpo_s: null` with a reason — never `0`; evidence dated **after** `snapshot_completed_at`
  is a typed refusal (`rpo: refused — …`, verdict `FAILED`) — never a negative number: it
  means a writer's clock or this host's clock is wrong, and that is the finding.
- **`rto_s`** — monotonic wall-clock from the start of the restore to the moment the copy
  was verified (copy → sha256 → schema head → row counts → audit chain). It is the time
  this host takes to restore and verify **this** store; it is **not** a recovery-time
  objective and excludes locating the backup, the operator, and the backend boot — which
  comes up under a recovery hold ([`../BACKUP_AND_RECOVERY.md`](../BACKUP_AND_RECOVERY.md)).

Both are observations of one run. Keep the JSON: a series of them is the evidence a
recovery objective would later be set against.

## The isolation rule

**Never restore over the live path.** `--restore-into` must be a directory that does not
exist or is empty; the harness refuses anything else and refuses a symlink. The restored
copy is for verification and for a deliberate, separate cut-over decision — not for the
running backend. The source database is opened read-only (`mode=ro`, SQLite's normal
locking — never `immutable=1`, which would ignore a WAL that appears after any check) and
is never written; on a WAL-mode store a read-only reader may create empty `-wal`/`-shm`
sidecars beside it (sqlite's own bookkeeping, a directory write, not a database write).

**Publication is name-bound and durable.** The harness writes the backup into the
exclusively created temp file's own descriptor (`/proc/self/fd/<n>`, identity re-checked),
stores the copy in rollback-journal mode (one self-contained file), fsyncs it, acquires the
final name with `link` — an existing file of that name is a refusal, nothing is ever
renamed over it — and fsyncs the directory. The audit log and its head anchor are read
**once, as a pair, after the backup completed** and both copies are written from that read;
an anchor without its log (or the reverse) is a refusal and nothing is published.

## By hand — the same steps the harness runs

1. **Refuse a source that is not a regular file.** `test -f data/chronos.db && ! test -L
   data/chronos.db` — a symlink, FIFO or directory at that path is a stop, not a backup.
2. **Take the backup with sqlite's online backup API, never `cp`.** A file copy of a
   WAL-mode database misses committed rows still in `-wal`; the backup API reads through
   the WAL and produces a consistent single file:
   `sqlite3 data/chronos.db ".backup '/var/backups/chronos/chronos-<stamp>.db'"`
   (the harness does the same through Python's `Connection.backup`, into a temp name that is
   renamed into place).
3. **Record the manifest.** Write down the completion time of step 2 as
   `snapshot_completed_at`; `sha256sum <backup>`; the schema head — `SELECT version_num FROM
   alembic_version` when that table exists, else `SELECT version FROM schema_version ORDER
   BY id DESC LIMIT 1` (a store created by `Database.initialize()` carries only the
   latter); `SELECT COUNT(*)` for every table in `sqlite_master` — all of these read from
   the **backup file**, not the live database; and, when `data/platform_audit.head.json`
   exists beside the database, copy `platform_audit.jsonl` and `platform_audit.head.json`
   beside the backup **together, after step 2** (one snapshot — never one file now and the
   other later), and record the anchor's `last_hash`. Note `"encryption": "none"` (see
   owner asks). The harness's copy is in rollback-journal mode; a `.backup` made by the
   `sqlite3` shell keeps the source's WAL flag — both are complete, single files.
4. **Restore into a fresh directory.** `mkdir -m 700 <fresh>` (it must not exist or be
   empty), then copy the backup — and the audit pair if present — into it.
5. **Verify.** `sha256sum` of the copy equals the manifest; the schema head query on the
   copy equals the manifest's and the repository accepts the copy
   (`python -c 'from chronos.persistence.database import Database; d = Database("sqlite:///<copy>"); d.initialize(); d.dispose()'`
   — on an already-versioned store this checks version and drift and applies **no**
   upgrade; it raises on mismatch); every table's `COUNT(*)` equals the manifest; and
   `python -m chronos.cli --audit-file <fresh>/platform_audit.jsonl verify-audit-log` (the
   same verifier the harness calls, `chronos.auditlog.verify_chain`) reports `VALID` when
   a chain travelled with the backup.
6. **Write the two numbers down** with the manifest: `rpo_s` = `snapshot_completed_at`
   minus the newest evidence timestamp read from the **restored copy** and the retained
   audit copy (never from the live database — it has moved on); if that evidence is newer
   than `snapshot_completed_at`, do not write a negative number: record the refusal and
   check the clocks. `rto_s` from a stopwatch around steps 4–5.

## Owner asks (Kevin) — the seams, not implemented here

- **Backup encryption and key custody.** The backup is written in the clear; the
  manifest's `encryption` field is the seam and reads `"none"` tonight. Encrypting at
  rest means a key that is NOT on this host's disk beside the backup, and a custody rule
  for it — both are owner decisions before any off-host copy exists.
- **Off-host copy placement.** Nothing here copies a backup anywhere; a backup on the same
  disk as the database is a convenience, not a recovery. Where the copy goes, how it is
  encrypted in transit and at rest, and who can read it are the owner's.
- **Retention** is likewise not implemented: the harness never deletes.

## What a FAILED verdict means

| failure prefix | what it says | what to do |
|---|---|---|
| `sha256:` | the restored copy's bytes are not the backup's | the backup medium or the copy path is untrustworthy; do not use this copy |
| `schema_head:` / `schema_acceptance:` | the copy's schema head is not the recorded one, or the repository refuses it (version/drift) | never upgrade in place during a drill; find why the head moved |
| `row_counts:` | a table's count differs from the manifest (names the tables) | the copy lost or gained rows after the backup — stop, investigate |
| `audit_chain:` | the audit log beside the copy is BROKEN or missing while the manifest records a head | the tamper-evidence did not survive the copy; the restore is not trustworthy |
| `rpo: refused —` | evidence in the retained snapshot is dated after the snapshot completed | a writer's or this host's clock is wrong; the backup itself may still be VERIFIED |
| `refused:` | a typed refusal before anything was published | fix the named condition (source type, non-empty target, an existing backup of the same name, a half audit pair) and re-run |
