# N3 cutover — archive the unkeyed store, start the keyed one (strategy (iii))

Kevin's physical steps, in order, for the one-time cutover from the unkeyed account
fingerprint (schema 17) to the keyed one (schema 18, `CHRONOS_PERSISTENCE_KEY`). The rulings
this follows, verbatim: *"archive the old DB with the restore-drill envelope, verify the
restore, start a fresh keyed DB at schema 18 with CHRONOS_PERSISTENCE_KEY; schema-17 DBs
refused; no open position at cutover; mandate re-authored before first start, dated window =
0"*, and for the key: *"Kevin generates the key himself (`secrets.token_hex(32)`), places it
in the host's 0600 .env, keeps one age-encrypted escrow copy off-host. His keystrokes only —
the team never generates, sees, or handles the real key."*

The command: `src/chronos/operations/cutover_archive.py` · tests:
`tests/unit/test_ops_cutover_archive.py`. It reuses the restore drill
([`RESTORE-DRILL.md`](RESTORE-DRILL.md)) for the envelope, and the drill's custody variables
(`CHRONOS_BACKUP_AGE_IDENTITY`, `CHRONOS_BACKUP_AGE_RECIPIENTS`) name the host identity and the
two recipients exactly as for any backup. Migration 0017 refuses an old store and names this
page; nothing here runs the migration.

**What the command never does:** it never generates, reads or prints the persistence key;
never starts the service; never initializes the fresh database; never opens the old store for
writing (its writer exclusion is a kernel read lease on a read-only descriptor); never deletes
anything — the four sentinel directories it leaves at the live names included: your `rmdir`
vacates them (step 7); never calls the broker; has no `--force`. Every refusal is a typed
`code` in its JSON and exit `2`; `--apply` writes nothing before every check has passed.

## Before you start

- Every Chronos process on this host must be stopped (step 1). The command proves what it can
  from the store (no live writer lease, no uncheckpointed write-ahead frames) and refuses
  otherwise; a read-only backend holds no lease, so **your** `is-active` check is part of the
  proof.
- Run the command as the user that owns `data/chronos.db` — the service user; `systemctl --user`
  units run as you. Before the gate the command takes a kernel **read lease** on the store (and,
  at the move, on its `-wal`/`-shm`): the kernel grants it only while no process has the file
  open for writing, and from then on blocks any writer's open until the command returns. The
  lease needs the file's owner. `WRITER_OPEN` means a process still has the store (or a sidecar)
  open — a backend that is stopped but still holds the store counts, as does a read-only
  connection at the move — and `WRITER_EXCLUSION_UNAVAILABLE` means the kernel would not grant
  the lease at all (wrong user, or a filesystem without leases). Neither has an override.
- The broker account must hold **no position** (the ruling's "no open position at cutover").
  The command enforces it from the store, never from the broker: every managed-position binding
  must replay to a closed position, and the latest reconciliation run — `RECONCILED`, with a
  `RECONCILED` readiness latch, completed within the last hour — must have recorded an empty
  position list. If it refuses with `POSITION_OBSERVED` or `OPEN_BINDING`: flatten at the
  broker, start the backend, let it reconcile and record the closing fills, stop it, re-run.
  The one-hour bound is a constant (twice the longest periodic cadence): if you are slower than
  that between the stop and the command, start → let it reconcile → stop → run again.
- The code on this host is the post-N3 checkout (`SCHEMA_VERSION` 18). Write down the
  pre-cutover commit of the running code before you change anything — the Rollback section
  needs it: `git -C <checkout> rev-parse HEAD` → `<pre-cutover sha>`.

## Steps

1. **Stop the service and prove it.**
   ```bash
   systemctl --user stop chronos-worker chronos-backend
   systemctl --user is-active chronos-backend chronos-worker   # both lines: inactive
   ```
   Run the `is-active` line twice, a few seconds apart. Both units `inactive`, or stop here.

2. **Dry-run the command** (the default). It runs the whole gate, writes nothing to the store
   and creates no envelope; its one directory effect is SQLite's reader bookkeeping beside a
   WAL-mode store — an empty `chronos.db-wal` and a `chronos.db-shm` — which `--apply` later
   moves aside with the store:
   ```bash
   BROKER_MODE=demo ALLOW_ORDER_TRANSMIT=false ALLOW_LIVE_TRADING=false \
   python -m chronos.operations.cutover_archive \
     --db data/chronos.db \
     --out /var/backups/chronos/n3 \
     --restore-into /tmp/chronos-n3-verify \
     --pretty
   ```
   Expect exit `0` and `"verdict": "DRY_RUN"`; read `gate` — `bindings` (all closed),
   `reconciliation` (run id, status, `age_s`), `positions_observed: 0`, `writer_lease.live:
   false`, `schema_version` (17) — and `planned` (the envelope and the three moved-aside names).
   Exit `2` with a `code` means the store did not prove something; the `failures[0]` line says
   what to do. Do not go on until the dry-run is clean.

3. **Apply.** Same command with `--apply`. Expect exit `0` and `"verdict": "ARCHIVED"`, with
   `drill.verdict` `ARCHIVE_VERIFIED` and `drill.acceptance` `archive:schema_version:17` (the
   drill's distinct verdict for a retired-schema archive, reachable only by the drill running
   the flatten gate itself over the held store and verifying by that run: version below the
   running one, the envelope bound to the very inode the gate held, exact head — the running
   code refuses a schema-17 store by design, see [`RESTORE-DRILL.md`](RESTORE-DRILL.md),
   "Archiving a retired schema"). Write down, from the JSON: `envelope`, `manifest_sha256`, `cleartext_sha256`,
   `ciphertext_sha256`, and every `moved_aside` pair. The old store is now
   `data/chronos.db.archived-<stamp>` plus the `-wal`/`-shm` siblings the gate admitted
   (including the empty bookkeeping the command's own read-only connection left), `<stamp>`
   being the envelope's own `chronos-<stamp>` — the moved-aside files are bound by name to their
   archive. `exclusion` is the proof that the admitted set stayed quiet through the move: every
   lease whole after the last rename and the admitted `-wal` still within its 32-byte header,
   and `live_names_sealed` — see the next paragraph.
   **The four live names are now sentinel directories.** Each rename was one `renameat2`
   EXCHANGE of the admitted file with a 0500 empty directory the command made and holds, so
   `data/chronos.db`, `-wal`, `-shm` and `-journal` were never vacant for an instant and are
   now directories (`sentinels` lists each with its device:inode; the names a WAL store has no
   file for, `-journal` above all, got theirs by a plain `mkdir`). Until **your own `rmdir`** in
   step 7 nothing can put a store the gate never saw at those names: the backend, `db-preflight`
   and every Chronos writer refuse a directory (`Refusing non-regular SQLite path`) and create
   nothing, a `sqlite3` shell gets `unable to open database file`, a `mv` over a directory gets
   `Is a directory`. The command's process may exit; the sentinels stay. What `ls -la data/`
   must show now: the four sentinel directories (`dr-x------`), the three `.archived-<stamp>`
   files, and the siblings that were there before (`keys/`, the audit pair, locks, logs).
   Anything else — a file at a live name, a missing sentinel — **stop here** and find out what
   wrote under a stopped service before anything starts; only the store owner's uid can remove
   a sentinel.
   - `"verdict": "ARCHIVE_NOT_VERIFIED"` (exit 2): the drill did not verify; **nothing moved**,
     the live store is untouched, and the JSON names the envelope and the restore copy that now
     exist. Read `drill.failures` against the drill runbook's table; do not retry blindly.
   - `code: TARGET_EXISTS` (exit 2): a moved-aside name already exists; nothing moved; move that
     entry out of the way by hand and re-run (the next run makes a new envelope and stamp).
   - `code: LIVE_NAME_OCCUPIED` (exit 2): a live name the gate did not admit — a `-wal`/`-shm`
     that appeared after admission, a `-journal` (also refused at the gate, before any envelope)
     — holds an entry, so its sentinel could not take it: **nothing moved**, no sentinel was
     made. A sidecar that appeared while every process was supposedly stopped means something
     was not: find out what, move the entry out of the way by hand (it is not this run's), and
     re-run. After the renames the same code means a live name no longer held this run's
     sentinel at the final proof (vacated and refilled — only your uid can): `moved_aside` is
     exact, nothing was deleted; investigate, then Rollback or step 7 by hand, knowingly.
   - `"verdict": "MOVE_INCOMPLETE"` (exit 2): an exchange failed after an earlier one succeeded,
     or the admitted set was not quiet after the last rename (`WRITER_OPEN`,
     `WAL_FRAMES_PRESENT`); `moved_aside` and `not_moved` list exactly which. Rollback (below)
     before anything starts, then investigate.
   - `code: SOURCE_SWAPPED` with `foreign_renamed` (exit 2): at the instant of an exchange the
     live name held a foreign entry, not the file the gate admitted; the command exchanged it
     back (`restored_to`) and did not move the admitted file, whose name is in `not_moved` and
     whose device:inode the message gives. (`LIVE_NAME_OCCUPIED` with `foreign_renamed`: a
     foreign directory had been renamed over the sentinel at the archive name in that instant;
     reversed the same way.) Nothing of yours was deleted; the sentinels the run made are listed
     in `sentinels` with where each is now (one exchanged back sits at its `.archived-<stamp>`
     name — `rmdir` it before you re-run). Something swapped entries under a stopped service:
     find out what before anything starts.
   - `code: WRITER_OPEN` after the gate (exit 2): a process opened the store (or a sidecar) for
     writing while the command ran; nothing moved. Step 1 again — then re-run.

4. **Remove the restore copy.** `/tmp/chronos-n3-verify` holds a CLEARTEXT copy of the
   archived store (the drill verified it there): `rm -r /tmp/chronos-n3-verify`. Your `rm` —
   the command never deletes.

5. **Generate the key yourself, into the 0600 `.env`.** On the host, in a shell whose history is
   off for this line (`set +o history` in bash; or `HISTFILE=` for the session):
   ```bash
   python -c 'import secrets; print(secrets.token_hex(32))'
   ```
   Paste the 64 hex characters into the host's private `.env` (the one `settings.py` loads,
   untracked, mode `0600` — `chmod 600 .env` and `ls -l .env` to prove it) as
   ```
   CHRONOS_PERSISTENCE_KEY=<the value you just printed>
   ```
   Then clear the terminal. Nobody and nothing else generates, sees, logs or handles this value;
   it is never typed into an agent, a handoff, a ticket or a chat. No example appears on this
   page on purpose.

6. **Escrow one copy off-host, encrypted, and prove it decrypts back.** The tool below reads the
   key from a non-echoing prompt — type (or paste) the same 64 hex characters you put in `.env`;
   it never takes them from the environment or the command line, so an unset variable cannot
   silently escrow an empty line — refuses anything but exactly one line of 64 lowercase hex,
   encrypts the line with `age` to the two recipients every backup uses (the host identity's
   public key and `KEVIN_RECOVERY_RECIPIENT`, validated by the restore drill from
   `CHRONOS_BACKUP_AGE_IDENTITY` / `CHRONOS_BACKUP_AGE_RECIPIENTS` exactly as in step 2),
   immediately decrypts the new artifact through the host identity into a private scratch and
   compares it in process (never printing it), and only then reports:
   ```bash
   python -m chronos.operations.persistence_key_escrow --out chronos-persistence-key.age --pretty
   ```
   Expect exit `0`, `"verdict": "ESCROWED"`, `"verified": true`, the two `recipients`, and the
   artifact's `ciphertext_sha256`. Exit `2` with `code: KEY_MALFORMED` means the line was not
   64 lowercase hex (nothing was written); `ESCROW_MISMATCH` means the artifact did not decrypt
   to what you typed — do not keep it. After any failure once the file was created, the tool
   **deletes nothing** (a removal would be by name, and a name can be swapped under it in the
   last instant; only its own private scratch is ever removed): `created_artifact` in the report
   names the ciphertext this run created by `path` and device:inode (`removed` is always
   `false`) — find it (`find <dir> -inum <inode>`) and shred it yourself, at its name or under a
   renamed entry. `TARGET_SWAPPED` means the output name stopped naming the file the tool wrote
   before the proof finished (something renamed or replaced it): the tool verifies through the
   descriptor it holds, so the ciphertext this run created sits under the renamed entry; the
   foreign entry at the name is not the tool's and was not touched. Then move
   `chronos-persistence-key.age` off this host (password manager attachment, or the same place
   the backup escrow goes), and `shred -u` or `rm` the local file yourself — the tool never
   deletes a verified artifact either. One escrow copy, exactly; the `.env` on the host is the
   only working copy. Your recovery private key never exists on a fleet host; the host identity
   can also open this artifact, as it can every backup — that is what made the on-host
   decrypt-and-compare possible.

7. **Vacate the live names yourself, then start the backend.** `ls -la data/` once more (step
   3's listing: four sentinel directories, three `.archived-<stamp>` files, the old siblings).
   Then, in ONE line — until it runs, every writer refuses with `Refusing non-regular SQLite
   path` and makes no file; once `chronos.db`'s sentinel is gone a refused writer would already
   create an empty file there, which is why the four go together:
   ```bash
   rmdir data/chronos.db data/chronos.db-wal data/chronos.db-shm data/chronos.db-journal
   ```
   Your hand, never the command's: it removes nothing. With the four names absent, the backend's
   startup creates a fresh database at the running schema (18) — `python -m chronos.cli
   db-preflight` first, then `systemctl --user start chronos-backend` — and refuses to start
   without a well-formed `CHRONOS_PERSISTENCE_KEY` (demo included). `systemctl --user is-active
   chronos-backend` → `active`; `curl -fsS http://127.0.0.1:<port>/health`.

8. **Bind.** The first bound run writes the `database_scope` row with the KEYED fingerprint;
   confirm the scope line of `python -m chronos.cli b1-preflight` (or the operator CLI you use
   for the account scope) reports the bound scope and no refusal.

9. **Re-author the mandate BEFORE the first start of autonomy — dated window = 0.** The
   mandate's `account_fingerprint` must be the keyed value (`python -m chronos.cli mandate
   fingerprint` prints it from the configured key; `mandate check` compares). Its
   `effective_from` is the first instant anything accepts the keyed value; nothing accepts the
   old value at any time, so there is no window to close. Only then start the worker and enable
   autonomy per the shadow-campaign runbook.

10. **Retention — 90 days.** The envelope under `--out` is a pre-cutover backup (its manifest
    says `schema_version:17`); the pruner (`python -m chronos.operations.backup_pruner`) deletes
    it after 90 days, as the ruling says. Keep `data/chronos.db.archived-<stamp>` (and its
    siblings) until you are satisfied the keyed store is right, then the same 90 days; they are
    the unkeyed history and the rollback, nothing else reads them.

## Rollback

- **Before any new row exists in the keyed store** (nothing started, or started and stopped
  without binding): stop the service; remove the sentinel directories first — `rmdir
  data/chronos.db data/chronos.db-wal data/chronos.db-shm data/chronos.db-journal` (and any
  sentinel the report's `sentinels` lists at an `.archived-<stamp>` name after a refusal); if
  the backend already ran, the fresh keyed `chronos.db` sits at the live name instead and you
  are in the next bullet; then rename the moved-aside files back by name —
  `mv -n data/chronos.db.archived-<stamp> data/chronos.db` and the same for the `-wal`/`-shm`
  siblings that were moved — check out the pre-cutover code (`git checkout <pre-cutover sha>`,
  the sha you wrote down before step 1: a schema-17 store is refused by the post-N3 code, by
  design); start. `mv -n` refuses if the live name exists (a sentinel still there says so with
  `Is a directory`); never force it.
- **After new rows exist in the keyed store:** going back drops them. Decide that explicitly;
  the archive and the moved-aside files restore the pre-cutover state at any time, with the
  pre-cutover code.
- **The archive at any later time:** the envelope restores through the drill's `restore`
  (`age -d -i data/keys/backup-host.age`, or your recovery key) exactly as any backup does
  (`RESTORE-DRILL.md`, step 4); a restored schema-17 copy is usable only under the pre-cutover
  code.
- **The key is lost after cutover:** the keyed store cannot bind and has no proof path;
  the escrow copy is the only supported recovery (step 6): decrypt it with your recovery key
  (or the host identity), put the value back in the 0600 `.env`, start. If the escrow copy is gone too,
  this is an owner decision, not a procedure: this command refuses the keyed store
  (`SCHEMA_ALREADY_CURRENT` — a store at the running schema is not a pre-cutover store), nothing
  on this page re-keys it, and its rows written since cutover are unrecoverable as a bound store;
  the last recoverable state is the pre-cutover archive plus the moved-aside files, under the
  pre-cutover code. Re-keying or archiving a store at the running schema would need a later
  running schema and a separately reviewed procedure — none exists today; do not improvise one.

## What the gate's refusal codes mean

| code | the store did not prove | what to do |
|---|---|---|
| `SOURCE_NOT_REGULAR` | `--db`, or a `-wal`/`-shm` beside it, is a symlink, directory or FIFO | name the real file; never a link |
| `KEYS_REFUSED` | the drill's age keys (identity 0600, exactly the two recipients) | the drill runbook, "Encryption at rest" |
| `SCHEMA_UNREADABLE` / `STORE_UNREADABLE` | the store could not be read as a Chronos store | stop; do not guess — inspect by hand, read-only |
| `SCHEMA_ALREADY_CURRENT` | the store is already at the running `SCHEMA_VERSION` | this is not a pre-cutover store; nothing to do |
| `NO_SCOPE_ROW` | the store was never bound | nothing to archive; migration 0017 upgrades it in place |
| `WRITER_LEASE_LIVE` | a backend held the writer lease within the last 30 s | step 1, then re-run |
| `WAL_FRAMES_PRESENT` | uncheckpointed write-ahead frames beside the store | step 1; the last writer's clean close checkpoints them |
| `OPEN_BINDING` / `BINDING_UNREADABLE` | a managed position is open, or its ledger cannot be replayed | flatten at the broker, reconcile, re-run / inspect the stream by hand |
| `NO_RECONCILIATION_RUN` / `RECONCILIATION_STALE` / `RECONCILIATION_NOT_CLEAN` / `RECONCILIATION_UNREADABLE` | the latest run is missing, older than one hour, not `RECONCILED`, or unreadable | start → reconcile → stop → re-run within the hour |
| `POSITION_OBSERVED` / `PROVENANCE_MISSING` | the latest run recorded a broker position (the message breaks it down by origin class) | flatten at the broker, reconcile, re-run |
| `DRILL_REFUSED` | the drill refused before verifying (an envelope name collision, a rename error) | the drill runbook's `refused:` row; nothing moved |
| `TARGET_EXISTS` / `SOURCE_SWAPPED` / `MOVE_FAILED` | the move-aside could not start, or the source changed under the command | step 3's bullets |
| `LIVE_NAME_OCCUPIED` | a live name of the store's namespace (`-wal`/`-shm` not admitted by the gate, `-journal`) holds an entry, so no sentinel can take it; or, after the renames, a live name no longer holds this run's sentinel | step 3's bullet: nothing moved in the first case; find what wrote it, move it by hand, re-run |
| `WRITER_OPEN` | a process holds the store (or, at the move, a sidecar) open for writing, or opened it for writing while the command ran — the kernel read lease was refused or broken | step 1; a stopped backend that still holds the store counts; then re-run |
| `WRITER_EXCLUSION_UNAVAILABLE` | the kernel would not grant the read lease (not the file's owner, or a filesystem without leases) | run as the user that owns `data/chronos.db`, on a local filesystem |
