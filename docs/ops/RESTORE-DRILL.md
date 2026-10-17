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
  --out /var/backups/chronos \
  --restore-into /var/backups/chronos/restore-$(date -u +%Y%m%dT%H%M%SZ) \
  --pretty
```

- exit `0` and `"verdict": "VERIFIED"` — the restored copy is byte-identical to the backup,
  its schema head is the one the backup recorded and the repository accepts it, every
  table's row count matches, and the audit chain (when one travelled with the backup)
  verifies.
- exit `2` — `"verdict": "FAILED"` with `failures[]` naming the verifier that spoke, or a
  `refused:` line (the source was not a regular file, the target was not empty, …). Nothing
  was written to the source in either case.

The manifest is inside the envelope as `<stem>-<stamp>/manifest.json`.

**`--out` is the pruner's root (N3-3 r6).** The envelope `chronos-<stamp>/` is published directly
under `--out`, and the pruner (below) reads only the direct children of its `--root`. Publish
under `/var/backups/chronos` and prune `/var/backups/chronos`, so the two commands meet. Older
runs of this page used a dated `--out /var/backups/chronos/<YYYYMMDD>`; see the pruner section
for how those are pruned.

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
exist or is empty; the harness refuses anything else and refuses a symlink.

**The restore target is yours alone (FIX-RT, 2026-10-17).** The restored `chronos.db` is the
whole store in the clear, so:
- `--restore-into` must be **absent**, in which case the drill creates it mode `0700`, or an
  empty directory **owned by your effective uid with no group or other permission bit**.
- The drill judges that by `fstat` on the directory descriptor it holds, never by the path.
  It refuses anything else before a byte is decrypted: `… is owned by uid …`, or `… has mode …,
  which grants group or other access`. A directory that appears between the drill's absence
  check and its `mkdir` is judged the same way.
- To prepare one by hand, run `mkdir -m 700 <path>` as yourself. Never reuse a directory
  someone else could have made.
- Put the target under a directory only you control, such as the backup root
  `/var/backups/chronos` (yours, 0700), **never under `/tmp`**. A predictable name in a
  world-writable directory lets another uid create it first, mode 0777, and read or replace
  the cleartext store.
- The decrypted `chronos.db` is created **exactly `0600`** whatever your umask, because `age`
  runs under umask `0077`, set in its own process only. It is proved `0600` and yours by
  `fstat` before anything reads it, and a wider or foreign file is removed and refused. The
  audit copies are created `0600` too, and SQLite gives any journal it opens beside the copy
  the copy's own mode.
- The lifetime of the restore copy (who removes it, and when) is a separate, open decision;
  nothing here deletes it. The restored
copy is for verification and for a deliberate, separate cut-over decision — not for the
running backend. The source database is opened read-only (`mode=ro`, SQLite's normal
locking — never `immutable=1`, which would ignore a WAL that appears after any check) and
is never written; on a WAL-mode store a read-only reader may create empty `-wal`/`-shm`
sidecars beside it (sqlite's own bookkeeping, a directory write, not a database write).

**Publication is ONE envelope, renamed into place atomically.** A backup is the whole triple
in one directory `chronos-<stamp>/` (a fixed prefix — the source's basename never leaks into
the name): the database — staged in rollback-journal mode as one self-contained file and
published ONLY as the ciphertext `chronos.db.age` (see *Encryption at rest*; the cleartext
staged copy is removed before the rename) — the audit log copy and its head anchor copy
(only when a pair sits beside the source), and `manifest.json`. The harness stages all of it in a **dot-prefixed
temp directory** under `--out` — the database written into the exclusively created temp
file's own descriptor (`/proc/self/fd/<n>`, identity re-checked), the audit pair read
**once, as a pair, after the backup completed**, every file fsynced, then the directory —
and publishes it with a single `renameat2(RENAME_NOREPLACE)` to the final name, then fsyncs
`--out`. What that buys, exactly: **a crash before the rename leaves only the dot-temp
directory**, which `restore` refuses by name whatever it contains (the CLI never treats a
dot-prefixed envelope as a backup); **a crash after the rename leaves the whole envelope**.
There is no state in which part of a backup is public. An existing entry at the final name
— a file or a directory — is a refusal (`already exists`), the temp is removed and **nothing
is published**; any other rename error is a typed refusal naming the envelope and the
errno, with the same cleanup. An anchor without its log (or the reverse) is likewise a
refusal with nothing published.

**The directory capability is held through every read.** The manifest's sha256, schema head
and row counts are read through the descriptor the walk admitted (the database opened
descriptor-relative, sqlite over `/proc/self/fd/<n>`), never through a re-resolved path; and
before the manifest is returned the envelope's name must still BE the directory that
was written (by `lstat` — a symlink to the same inode is refused) AND every component of
the reported `--out` path must be a real directory reached without following a link —
otherwise
the harness refuses with a message that says the truth: `published: the envelope … exists
in the directory the walk admitted … but the path … no longer names that directory`. The
restore holds its target descriptor through the copy **and** the verification the same way
(the repository's own acceptance check opens the copy by path, so it is bracketed by
identity checks); a swapped restore target is a `restore target swapped` failure, never
`VERIFIED`.

**Directories are reached component by component.** The backup directory and the restore
target are opened by an `O_DIRECTORY|O_NOFOLLOW` walk from `/` — every path component,
not just the last one — with missing components created descriptor-relative (mode 0700).
A symlinked ancestor (`alias -> elsewhere`) is refused by name before anything is created
past it, so an alias cannot redirect backup or restore bytes into a tree the operator did
not name. Every later create, link, unlink and fsync is relative to the retained descriptor.

## By hand — the same steps the harness runs

1. **Refuse a source that is not a regular file.** `test -f data/chronos.db && ! test -L
   data/chronos.db` — a symlink, FIFO or directory at that path is a stop, not a backup.
2. **Take the backup with sqlite's online backup API, never `cp`.** A file copy of a
   WAL-mode database misses committed rows still in `-wal`; the backup API reads through
   the WAL and produces a consistent single file:
   `mkdir -m 700 /var/backups/chronos/.chronos-<stamp>.<16 hex>.tmp && sqlite3 -readonly data/chronos.db ".backup '/var/backups/chronos/.chronos-<stamp>.<16 hex>.tmp/chronos.db'"`
   (`-readonly` is required, not a nicety: a read-write `sqlite3` client closing beside a live backend can delete
   the live `-wal`, and commits the backend acknowledged are then lost if it later crashes or is killed — see
   [`../BACKUP_AND_RECOVERY.md`](../BACKUP_AND_RECOVERY.md#sqlite-safe-backup). With every Chronos process stopped the
   read-write form is also safe.)
   (stage into a temp directory named by the exact grammar `.chronos-<stamp>.<16 hex>.tmp`
   — 16 lowercase hex digits, e.g. from `openssl rand -hex 8`; the envelope is renamed into
   place in step 3b). The name rule: a published envelope is always `chronos-<stamp>` — a
   fixed prefix, never the source's basename — and a crash temp is recognised ONLY by that
   exact grammar, never by a leading dot alone.
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
   other later), and record the anchor's `last_hash`. The harness's copy is in
   rollback-journal mode; a `.backup` made by the `sqlite3` shell keeps the source's WAL
   flag — both are complete, single files.
3a. **Encrypt the staged database to the two recipients, then remove the cleartext.**
   `age -R data/keys/backup-recipients.txt -o <temp>/chronos.db.age <temp>/chronos.db && rm <temp>/chronos.db`
   (the harness passes each VALIDATED recipient as `-r <key>` rather than re-reading the
   file, and reads and writes through the envelope's directory descriptor). Record in the
   manifest: `"sha256"` = the digest of the CLEARTEXT `chronos.db` from step 3 (what a restore
   verifies), `"ciphertext_sha256"` = `sha256sum <temp>/chronos.db.age`, and
   `"encryption": {"scheme": "age-x25519-v1", "recipients": [<the two public keys, sorted>], "tool": "age v1.3.2"}` (the `tool` string is `age` plus whatever `age --version` prints — `v1.3.2` for the release binary, `1.1.1` for the Ubuntu package).
   A temp that still holds `chronos.db` is not ready to publish.
3b. **Publish the envelope with the harness's exclusive rename.** With `manifest.json`
   written into the temp directory:
   `python -m chronos.operations.restore_drill publish-envelope /var/backups/chronos/.chronos-<stamp>.<16 hex>.tmp /var/backups/chronos/chronos-<stamp>`
   — the command publishes the checked envelope or refuses; a PUBLISHED line means the
   final name IS that directory. It opens the temp `O_DIRECTORY|O_NOFOLLOW` and keeps that
   identity, renames by the same name with ONE `renameat2(RENAME_NOREPLACE)` — an existing
   file **or** directory at the final name is a refusal (exit 2, nothing moves, the temp
   stays for you to inspect) — fsyncs the parent, then re-proves the final name: by
   `lstat` it must be a directory with the retained identity and the no-follow walk of the
   final path must reach it; anything else (a link planted at the freed temp name between
   the check and the rename, a swapped ancestor) is a `published:` refusal, exit 2, and no
   PUBLISHED line — the entry at the final name is then NOT a backup. A plain `mv -T` is
   **not atomic** against a concurrent name: it renames over an existing empty directory
   and reports success, so it is never the by-hand step — use the command above, or run the
   harness for a real drill. From this point the backup is the directory `chronos-<stamp>/`
   and nothing else.
4. **Restore into a fresh directory.** `mkdir -m 700 <fresh>`, as yourself, under a
   directory only you control (it must not exist; never under `/tmp`). Then decrypt the backup
   into it through the host identity, under a private umask, so the cleartext file is `0600`
   from its first byte:
   `(umask 077 && age -d -i data/keys/backup-host.age -o <fresh>/chronos.db <envelope>/chronos.db.age)`.
   Then copy the audit pair beside it if present. A non-zero exit (a corrupted `.age`,
   an identity that is not a recipient) is a stop: the harness records it as
   `decrypt: refused — …`, `verified: false`, and leaves nothing partial in the target.
5. **Verify.** `sha256sum` of the decrypted copy equals the manifest's `sha256`; the schema head query on the
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

## Encryption at rest — two recipients (Muse ruling 2026-09-16, delegated by Kevin)

Every backup's database is encrypted with [age](https://github.com/FiloSottile/age)
(X25519) to exactly **two recipients**, and is never published in the clear:

- **The host operational key** — the identity this host restores with. The operator
  generates it once per host, untracked, readable by the operator alone:
  `mkdir -m 700 data/keys && age-keygen -o data/keys/backup-host.age && chmod 600 data/keys/backup-host.age`
  — then its public key, `age-keygen -y data/keys/backup-host.age`, is line one of
  `data/keys/backup-recipients.txt`. The harness refuses to back up or restore if the
  identity is missing, not a regular file, not mode 0600, or not owned by the process uid
  (the paths come from `CHRONOS_BACKUP_AGE_IDENTITY` and `CHRONOS_BACKUP_AGE_RECIPIENTS`;
  the defaults are the two above). **Rotation on host rebuild** = a new identity + a new
  line one; older backups stay readable by Kevin's key, and only by it — that is the point
  of the second recipient.
- **Kevin's recovery key** — line two of the recipients file is his PUBLIC key,
  `age1ddwtdaexpp2k0dp22fj3rtqfw9y5trr3we77rsmwq9s0y70ase3sgu07kf`; the private half lives
  in his password manager and is **never on a fleet host**. No fleet code, test or runbook
  step generates, stores, prints or logs it. A backup without a readable host identity is
  recoverable ONLY through Kevin's key. The recovery recipient is **pinned in code**
  (`KEVIN_RECOVERY_RECIPIENT` in `src/chronos/operations/restore_drill.py`, the value Muse
  recorded 2026-09-16): the recipients file names it, it never chooses it. Rotating
  Kevin's key is a **reviewed code change** plus a new recipients line — never a file edit
  alone — so nobody with write access to `data/keys/` can make a different key the second
  decrypting party.

The recipients file must hold exactly these two lines (each a valid `age1…` key, distinct)
and the validator refuses **any set other than {host, Kevin}** as a typed refusal before any
database is opened. What the refusal says, exactly: a wrong line count (one line, three
lines, an empty file) names the count, which required key(s) are missing — by role and the
first 12 characters of the expected key — and the line number(s) that are neither required
key; a two-line file with a wrong key (the host plus any other key, Kevin's key without the
host's) names the offending line number, the key on it and the missing required key; a line
that is not a valid `age1…` key names its line number; the same key twice names the
duplication. The manifest's `encryption` block records the
scheme, both recipients (sorted) and the tool version, so a reader can tell which keys a
given envelope was minted for; `sha256` is the CLEARTEXT digest (what step 5 verifies) and
`ciphertext_sha256` the `.age` file's. The by-hand decrypt is step 4's `age -d -i …` line.

Install `age` from the distro package (`sudo apt-get install -y age` — the CI runner does
exactly this) or the official release; the harness refuses without it with the sentence
*age is not installed: install the distro `age` package or the official release
(https://github.com/FiloSottile/age/releases) so that `age` and `age-keygen` are on PATH —
docs/ops/RESTORE-DRILL.md, section 'Encryption at rest'*, and the test file skips with the
same sentence on a host without it.

## Archiving a retired schema — the N3 cutover (a distinct verdict, never a substitute)

The N3 cutover archive (`python -m chronos.operations.cutover_archive`,
[`N3-CUTOVER.md`](N3-CUTOVER.md)) runs this harness over the **pre-cutover** store — a schema
the running repository refuses by design (`database.py`: "Unsupported Chronos schema
version"). Step 5's repository acceptance can therefore never pass for that archive. The
archive command's `--apply` hands its open bound source capability to the drill's
`run_archive_drill`, which runs the archive command's **flatten gate itself** over that
capability, inside its own frame — a store the gate refuses raises the gate's own refusal and
no envelope is made — and keeps that run's acceptance (the head it read, `schema_version:17`;
that head's numeric version, which must be BELOW the running `SCHEMA_VERSION` — a current or
newer store is never an archive; and the inode of the store it gated) as a closure inside
that frame: not a value, not a type, not a parameter. There is no other way to it: the
capability carries no admission state or method; `run_drill` / `restore` / `verify_restored`
and every other function the module defines at its top level — underscore-prefixed ones
included — perform the repository's acceptance only (`restore` and `verify_restored` take no
capability either; `run_drill(source=)` binds the backup's identity and yields `VERIFIED` or
`FAILED` alone), and no function at module scope has a parameter that selects or carries an
archive acceptance — a caller cannot reach `ARCHIVE_VERIFIED`, or any `archive:` acceptance,
without the real gate passing over the very capability it hands in. The drill then verifies by
that closure — the envelope's manifest must record the gated inode (`source_identity`: the
backup was of the gated object, through the one bound connection), and `restored head ==
manifest head == the head the gate read`; sha256, row counts and the audit chain verify
exactly as before. On that branch nothing is opened by pathname and no T-3 name check runs: the copy's
bytes are the held descriptor's (sha256), while its head and row counts come through
`_readonly_fd`, a resolved-pathname open. All good, the verdict is `ARCHIVE_VERIFIED` with
`"acceptance": "archive:<head>"` — **not** `VERIFIED`, which is only ever the repository's
acceptance (`"acceptance": "repository"`, every ordinary drill). Every public drill path over a
retired store ends in the repository's own refusal of the retired version; a later `restore` of
the envelope under the running code reports every verifier and exactly that refusal
(`verified: false`, one `schema_acceptance:` failure) — the copy is there, usable only under
the code that last ran it. The CLI never admits anything: to USE a restored copy of a retired
schema, check out the code that last ran it (the pre-cutover sha the cutover runbook records).

Every manifest now records `source_identity` (`[device, inode]`) of the file the backup
connection was proven to have opened; a manifest written before this field exists reads as
`null` and restores as before.

## Owner asks (Kevin) — the seams, not implemented here

- **Off-host copy placement.** Nothing here copies a backup anywhere; a backup on the same
  disk as the database is a convenience, not a recovery. Where the copy goes, how it is
  encrypted in transit and at rest, and who can read it are the owner's.
- **Retention** is not done by the harness, which never deletes. Kevin's ruling (2026-10-15):
  the encrypted archive and pre-cutover backups live 90 days, then are deleted — by the
  separate Kevin-run pruner below.

## Pruning old backups — Kevin-run, 90 days (N3-3)

`python -m chronos.operations.backup_pruner --root /var/backups/chronos --pretty` lists what
it WOULD delete and deletes nothing. Add `--apply` to delete.

- **Which `--root`:** the directory the envelopes were published into (their `--out`), because
  only its direct children are read; the pruner never descends into other directories.
  - Backups made with the commands on this page and in [`N3-CUTOVER.md`](N3-CUTOVER.md) sit
    directly under `/var/backups/chronos`: one run, the command above.
  - Backups made earlier under a dated `--out` (`/var/backups/chronos/<YYYYMMDD>/chronos-<stamp>/`)
    are pre-cutover backups too and fall under the same 90 days. They are not reached by the
    run above (it lists each dated directory as `unclassified`). Prune each one with its own
    run, dry run first:
    ```bash
    for d in /var/backups/chronos/[0-9]*/; do
      python -m chronos.operations.backup_pruner --root "$d" --pretty
    done
    ```
    Read every report, then repeat with `--apply` for the ones you accept. The cutover command
    of an earlier page version used `--out /var/backups/chronos/n3`; if you ran that, add
    `--root /var/backups/chronos/n3` to the same routine.

- **What it deletes:** a published envelope `chronos-<stamp>/` whose own `manifest.json`
  records a pre-cutover schema (`schema_head` of `schema_version:N`, N below 18 — the archive
  of the old schema-17 database is one), an `encryption.scheme` exactly equal to the restore
  drill's `age-x25519-v1`, and a `snapshot_completed_at` more than 90 days before now. Exactly
  90 days is kept. Age and class are read from the manifest, never from file times.
- **What it never deletes:** a post-cutover envelope (schema 18 and up) however old; anything
  it cannot classify (an `alembic:`/`unversioned` head, a bad or missing manifest, an
  unexpected file, a future-dated snapshot, any other encryption scheme, crash temps
  `.chronos-…tmp`, restore copies, stray files) — these are listed under `unclassified` so you can decide by hand; anything
  outside `--root`.
- **Symlinks:** a symlink ANYWHERE under the root (a direct child, a file inside an envelope,
  or deep inside any other directory), or a symlinked `--root`, refuses the whole run (exit 2)
  before anything is deleted. It is never followed. A tree too deep or too large to check is
  refused the same way.
- **How a delete is bound to the right directory:** the envelope is opened once (no-follow) and
  its identity must equal the one that was classified; it is then renamed to a private
  `.n3-prune-<random>` name inside the root and checked again; its files are unlinked through the
  open descriptor and the quarantine is removed. If a different directory takes the name, what
  happens depends on when:
  1. **Before the quarantine identity check passes:** the classified envelope's files are not
     touched. If the rename moved a substitute into quarantine, that substitute is put back to
     the envelope name; a classified envelope the swap moved aside stays there. The report names
     the envelope under `failed`, and the exit is 2.
  2. **After that check, before its files are unlinked:** the classified envelope's files ARE
     removed (through the descriptor that was opened and checked; the swap cannot redirect
     that), the substitute's content is not touched, the report names the envelope under
     `failed`, the exit is 2, and the emptied envelope directory is wherever the swap moved it.
  3. **After the last identity check, before the final `rmdir`:** someone with write access to
     the root can swap in a directory of their own. If it is empty it is removed (they made it;
     the run reports success); if it holds anything it stays untouched and the delete is
     reported as `failed`. Only an empty directory an attacker created can be removed this way.

  In every case a substitute's content is never deleted. A `.n3-prune-…` leftover means a delete was
  interrupted; the next run lists it under `unclassified`.
- **Exit codes:** `0` nothing to prune · `10` dry run found candidates (nothing deleted) ·
  `11` pruned · `2` refused (or a delete failed; the report names it).
- **The 90 is fixed** (`RETENTION_DAYS`, the ruling); there is no flag to change it and no
  `--now`. Output is JSON of counts, names and fixed reasons only — no manifest value and no
  file name from inside an envelope is ever echoed.
- **Restore copies:** under the default targets above (`/var/backups/chronos/restore-<stamp>`,
  `/var/backups/chronos/n3-verify`) the cleartext copies `--restore-into` writes sit INSIDE the
  backup root. The pruner reports each as `unclassified` ("not a published envelope name") and
  never deletes it (measured: a dry run and an `--apply` left such a directory byte-identical).
  Removing them is a by-hand owner step; their lifetime is the open decision stated under
  "The isolation rule". A target pointed elsewhere is outside the root and equally yours to
  remove. **Not covered:** off-host copies of an
  envelope are yours to prune too.

## What a FAILED verdict means

| failure prefix | what it says | what to do |
|---|---|---|
| `sha256:` | the restored copy's bytes are not the backup's | the backup medium or the copy path is untrustworthy; do not use this copy |
| `schema_head:` / `schema_acceptance:` | the copy's schema head is not the recorded one, or the repository refuses it (version/drift) | never upgrade in place during a drill; find why the head moved |
| `row_counts:` | a table's count differs from the manifest (names the tables) | the copy lost or gained rows after the backup — stop, investigate |
| `audit_chain:` | the audit log beside the copy is BROKEN or missing while the manifest records a head | the tamper-evidence did not survive the copy; the restore is not trustworthy |
| `rpo: refused —` | evidence in the retained snapshot is dated after the snapshot completed | a writer's or this host's clock is wrong; the backup itself may still be VERIFIED |
| `refused:` | a typed refusal — nothing is published (a `published:` refusal is the one exception: the envelope exists in the admitted directory but the path was swapped underneath) | fix the named condition (source type, non-empty target, an existing entry at the envelope's name, a half audit pair, a symlinked path component, a rename error) and re-run |
