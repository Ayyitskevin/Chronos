# N3 cutover — archive the unkeyed store, start the keyed one (strategy (iii))

> **STOP: read this before you pick a date.** **Do not schedule or run the cutover until FIX-RT (restore-target hardening) AND N3-3 (`chronos.operations.backup_pruner`) have both merged to main.**
> Both have (#311/#312, #313). **And do not schedule or run it until N3-4 (the self-cleaning restore drill, P2-4 = B) AND the recovery-key-only escrow change have merged to main too.** **And do not schedule or run it until C1-F (the step-8 and rollback fixes from the C1 rehearsal) has merged to main too.**
> - **Why (LINE STOP #6, 2026-10-17):** this page's original restore target was a fixed
>   name under `/tmp`. Another local uid could create it first and read or replace the
>   cleartext copy of the whole pre-cutover store. FIX-RT closes that in code. N3-3 ships
>   the pruner that step 10's 90-day retention relies on.
> - **Why N3-4 (Kevin, P2-4 = B, K-20261023-002, 2026-10-23):** the drill's cleartext
>   `--restore-into` copy was removed only by your own `rm -r`; N3-4 makes the verifying
>   process remove the copy it created (`RESTORE_COPY_NOT_REMOVED` if it cannot).
> - **Why the escrow change (Kevin, recovery-key-only, K-20261023-003, 2026-10-23):** step 6's
>   escrow copy changes recipients; the change lands with its own step-6 rewording.
> - **Why C1-F (K-20261024-001, 2026-10-24):** the C1 rehearsal found that step 8's key-bound
>   check could take the key from the environment, not only from the file, and that the
>   rollback's fallback was a check-then-move that could overwrite a live name. C1-F fixes both
>   in this runbook; until it has merged, the steps below still carry those defects.
> - **How to check, on the host's checkout:** first refresh `origin/main`, then all six
>   `git grep` commands must print something and exit zero. They read the current tip, so a later
>   removal or revert shows as a failure; a `git log -- <path>` would still print history.
>   - `git fetch origin main`
>   - `git grep -n '^def prune(' origin/main -- src/chronos/operations/backup_pruner.py` (N3-3)
>   - `git grep -n RESTORE_TARGET_NOT_PROTECTED origin/main -- src/chronos/operations/cutover_archive.py`
>     (FIX-RT)
>   - `git grep -n '^RESTORE_COPY_NOT_REMOVED = ' origin/main -- src/chronos/operations/restore_drill.py`
>     (N3-4)
>   - `git grep -n 'recipient = restore_drill.KEVIN_RECOVERY_RECIPIENT' origin/main -- src/chronos/operations/persistence_key_escrow.py`
>     (escrow)
>   - `git grep -n 'ln -[T] data/chronos.db.archived-' origin/main -- docs/ops/N3-CUTOVER.md`
>     (C1-F, the rollback fix; the bracket keeps this line from matching itself)
>   - `git grep -n 'dotenv_values(".env", interpolate=[F]alse)' origin/main -- docs/ops/N3-CUTOVER.md`
>     (C1-F, step 8's literal `.env` read; a landed C1-F has both, a half-landed one fails this check or the one above)
> - **If any of them prints nothing or exits non-zero,** stop here. This notice is removed
>   only by a reviewed change, once all of them have merged.

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
writing (its writer exclusion is a kernel read lease on a read-only descriptor); deletes
nothing of yours — the four sentinel directories it leaves at the live names included: your
`rmdir` vacates them (step 7) — and only ever its own cleartext verify copy under
`--restore-into` (N3-4, step 4); never calls the broker; has no `--force`. Every refusal is a typed
`code` in its JSON and exit `2`; `--apply` writes nothing before every check has passed.

## Before you start

- **Do not schedule or run the cutover until FIX-RT (restore-target hardening) AND N3-3 (`chronos.operations.backup_pruner`) have both merged to main.** **And do not schedule or run it until N3-4 (the self-cleaning restore drill, P2-4 = B) AND the recovery-key-only escrow change have merged to main too.** **And do not schedule or run it until C1-F (the step-8 and rollback fixes from the C1 rehearsal) has merged to main too.** The STOP at the top of this page says how to check. If any is
  missing, nothing below applies yet.
- **The restore target lives under a directory only you control, never under `/tmp`
  (FIX-RT).**
  - The command restores the archive into `--restore-into` to verify it. That copy is the
    whole pre-cutover store in the clear — and the command removes it, directory included,
    before it prints its report (N3-4, Kevin's ruling 2026-10-23): the drill unlinks what it
    wrote through the directory descriptor it has held since it created the leaf, never by
    the path, and `drill.restore_copy_removed` in the JSON says it did.
  - Use `--restore-into /var/backups/chronos/n3-verify`: beside `--out`, under the backup root.
  - The leaf (`n3-verify`) must **not exist**. The command creates it, mode `0700`, and the
    drill verifies its uid and mode by `fstat`.
  - Its parent must be owned by you, and every directory from `/` down to it must be owned by
    root or by you, with no group or other write bit. This is the same rule as for `data/`
    below, and the same check. Use `ls -ld / /var /var/backups /var/backups/chronos`.
  - The command refuses otherwise — dry run and `--apply` alike, before any envelope exists:
    - `RESTORE_TARGET_NOT_PROTECTED`: the chain, `/tmp` included;
    - `RESTORE_TARGET_NOT_FRESH`: the leaf already exists, empty or not.
  - A different name under `/tmp` is not a fix: any predictable name there can be prepared
    by another uid first.
- **`data/` must be owner-private — `chmod 0700 data`**, as the owner. Removing an empty
  directory is authorized by write permission on **its parent**, not by the directory's own
  mode, so a group- or other-writable `data/` would let another uid remove one of step 3's
  sentinel directories and put a store the gate never saw at the live name. The command
  therefore refuses a `data/` that is not owned by your uid or that carries a group or other
  write bit — `PARENT_NOT_PRIVATE`, dry-run and apply alike, before it opens anything — and
  proves it again on the directory descriptor the renames use. `DEPLOYMENT.md` creates `data/`
  0700 from now on; an older deployment's `mkdir -p data` under umask 002 left it 0775
  (group-writable): fix that here, once. The sticky bit is no substitute and is not consulted.
- **Every directory above `data/` must be protected too — the path is only as safe as its
  weakest ancestor.** A uid that can write an ancestor can rename the checked `data/` away and
  plant a replacement under the same path, sentinels and all. From `/` down to `data/`, each
  directory must be a real directory (never a symlink) owned by root or by you, with no group
  or other write bit; a `/tmp`-style sticky world-writable directory is outside the supported
  deployment even though the sticky bit limits renaming to an entry's owner (other uids can
  still create entries there). Check your own chain, once: `ls -ld / /srv /srv/chronos
  /srv/chronos/data` (substitute the real path) — every line must be `root` or you, with no `w`
  in the group or other columns. The command refuses otherwise — `PATH_NOT_PROTECTED` — before
  it opens anything, holds every ancestor open through its last report, and re-proves at the
  move and at report time that each path component still names the directory it checked;
  `PATH_DISPLACED` means it did not (the message names the held directory by inode: the
  sentinels and the archived store stand in the renamed-away directory, never in a replacement). Give the command the store's **real path** — no `..`, no symlink in it: a `..` after a
  symlink is resolved through the link by the kernel, so the command refuses any `..` component
  outright (`PATH_NOT_PROTECTED`) rather than guess which directory you meant.
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
- The code on this host is the post-N3 checkout (`SCHEMA_VERSION` 18). The Rollback section
  needs the commit the host ran **before** the post-N3 deploy, not the post-N3 `HEAD`: only
  schema-17 code starts the restored schema-17 store, and the post-N3 code refuses it, by design
  (`Unsupported Chronos schema version 17; expected 18`). Write that commit down as
  `<pre-cutover sha>`: before the deploy it is `git -C <checkout> rev-parse HEAD`; after the
  deploy, it is the entry `git -C <checkout> reflog` shows just before the deploy. If you cannot
  establish it, use `8093176`, the last schema-17 commit on main (`f0a3446^1`, the first parent
  of the merge that brought schema 18). Then check it. The check parses that commit's
  `database.py` with `ast` and never imports or runs it: it prints `SCHEMA_VERSION = 17` only
  when the one place it can see setting `SCHEMA_VERSION` is a plain top-level assignment of 17.
  Two assignments, an annotated or augmented one, a binding anywhere else, a string, bytes or
  keyword argument that names it, or a `*` import each print a line beginning `STOP:` instead
  (C1R-F fix round 1, kimi P1). So does any use of `exec`, `eval`, `compile`, `__import__`,
  `setattr`, `globals`, `vars`, `locals`, `importlib`, `builtins` or `__builtins__`, whatever
  it does: code built or looked up at run time can set the name where a static read cannot
  follow it (fix round 2, kimi P1). The check guards against an honest wrong pick — a mistyped
  sha, a schema-18 commit — not against a commit written to defeat it. The backstop for that
  is the schema gate: code whose effective `SCHEMA_VERSION` is 18 refuses the restored
  schema-17 store at start. `db-preflight`, which the backend unit runs first, prints
  `REFUSE [schema] Unsupported Chronos schema version 17; expected 18` and exits 78
  (rehearsed), so such a pick fails loudly, not silently. This must print exactly one line,
  `SCHEMA_VERSION = 17`; anything else — a `STOP:` line, an error, or nothing — means that
  commit is not the rollback code, so stop:
  ```bash
  git -C <checkout> show <pre-cutover sha>:src/chronos/persistence/database.py | python -I -c '
  import ast, sys
  tree = ast.parse(sys.stdin.buffer.read())
  name = "SCHEMA_VERSION"
  runs = {"exec", "eval", "compile", "__import__", "setattr", "globals", "vars", "locals"}
  used = sorted({  # run-time code that may set it, whatever it does: a name, a def, an import
      w for n in ast.walk(tree)
      for w in (getattr(n, "id", None), getattr(n, "name", None), getattr(n, "module", None))
      if w in runs or str(w).split(".")[0] in ("importlib", "builtins", "__builtins__")
  })
  if used:
      sys.exit("STOP: the file uses " + ", ".join(used) + ", which a static read cannot follow.")
  sets = [  # every node that may set it: bind, delete, import, keyword, string, bytes, import *
      n for n in ast.walk(tree)
      if name in (getattr(n, "id", None), getattr(n, "attr", None))
      and not isinstance(n.ctx, ast.Load)
      or name in (getattr(n, "name", None), getattr(n, "asname", None), getattr(n, "arg", None))
      or name in (getattr(n, "rest", None), *getattr(n, "names", ()))
      or isinstance(n, ast.Constant) and isinstance(n.value, str) and name in n.value
      or isinstance(n, ast.Constant) and isinstance(n.value, bytes) and name.encode() in n.value
      or isinstance(n, ast.alias) and n.name == "*"
  ]
  top = [s for s in tree.body if isinstance(s, ast.Assign) and s.targets == sets]
  if len(top) != 1:
      sys.exit(f"STOP: {len(sets)} place(s) may set SCHEMA_VERSION, not one plain assignment.")
  value = top[0].value
  if not (isinstance(value, ast.Constant) and type(value.value) is int and value.value == 17):
      sys.exit(f"STOP: SCHEMA_VERSION = {ast.unparse(value)}, not 17.")
  print("SCHEMA_VERSION = 17")
  '
  ```

## Steps

1. **Stop the service and prove it.** Before you schedule this step, or start it:
   **Do not schedule or run the cutover until FIX-RT (restore-target hardening) AND N3-3 (`chronos.operations.backup_pruner`) have both merged to main.** **And do not schedule or run it until N3-4 (the self-cleaning restore drill, P2-4 = B) AND the recovery-key-only escrow change have merged to main too.** **And do not schedule or run it until C1-F (the step-8 and rollback fixes from the C1 rehearsal) has merged to main too.** (the STOP at the top of this page says how to check).
   ```bash
   systemctl --user stop chronos-worker chronos-backend
   systemctl --user is-active chronos-backend chronos-worker   # both lines: inactive
   ```
   Run the `is-active` line twice, a few seconds apart. Both units `inactive`, or stop here.

2. **Dry-run the command** (the default). It runs the whole gate and the restore-target check
   (`RESTORE_TARGET_NOT_PROTECTED`, `RESTORE_TARGET_NOT_FRESH` refuse the dry run exactly as
   they refuse `--apply`, so a clean dry run also proves `--restore-into`; C1 F5), writes
   nothing to the store and creates no envelope; its one directory effect is SQLite's
   reader bookkeeping beside a WAL-mode store — an empty `chronos.db-wal` and a
   `chronos.db-shm` — which `--apply` later moves aside with the store:
   ```bash
   BROKER_MODE=demo ALLOW_ORDER_TRANSMIT=false ALLOW_LIVE_TRADING=false \
   python -m chronos.operations.cutover_archive \
     --db data/chronos.db \
     --out /var/backups/chronos \
     --restore-into /var/backups/chronos/n3-verify \
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
   nothing, a `sqlite3` shell gets `unable to open database file`, and a `mv` onto a sentinel
   aims inside the directory and gets `Permission denied` from its 0500 mode. The command's
   process may exit; the sentinels stay. What `ls -la data/`
   must show now: the four sentinel directories (`dr-x------`), the three `.archived-<stamp>`
   files, and the siblings that were there before (`keys/`, the audit pair, locks, logs).
   Anything else — a file at a live name, a missing sentinel — **stop here** and find out what
   wrote under a stopped service before anything starts. Only your uid can remove a sentinel —
   not because of the sentinel's own 0500, but because `data/` is owner-private (the command
   proved it before opening the store and again before the exchanges; `exclusion.parent` says
   so) and removing an entry needs write permission on **its parent**.
   - `"verdict": "ARCHIVE_NOT_VERIFIED"` (exit 2): the drill did not verify; **nothing moved**,
     the live store is untouched, and the JSON names the envelope that now exists; the restore
     copy was removed all the same (`drill.restore_copy_removed`), or `drill.failures` says why
     not. Read `drill.failures` against the drill runbook's table; do not retry blindly.
   - `code: RESTORE_COPY_NOT_REMOVED` (exit 2, `"verdict": "REFUSED"`): the drill verified the
     archive but could not remove its cleartext copy (the `drill.failures` line says what remains
     under `/var/backups/chronos/n3-verify` and why); **nothing moved**, the envelope exists and
     is named. Remove that directory yourself (`rm -r`), find out why the command could not, and
     re-run — the next run makes a new envelope and stamp, and the leaf must be absent again.
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

4. **Remove the restore copy — the command already did; confirm it is gone.** The command
   removed `/var/backups/chronos/n3-verify`
   itself — the CLEARTEXT copy the drill verified there (`0700`, its `chronos.db` `0600`), and
   the directory — before it printed the report (N3-4: `drill.restore_copy_removed: true`,
   `drill.removed` lists the names; the removal went through the descriptor the drill held,
   never through the path). Check: `ls -ld /var/backups/chronos/n3-verify` must say `No such
   file or directory`. If it exists, the report said why (`RESTORE_COPY_NOT_REMOVED`) and
   nothing moved: step 3's bullet.

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
   on a line of its own: no quotes, no `export`, no spaces. `.env.example` carries the line
   commented out (`#CHRONOS_PERSISTENCE_KEY=`): replace that comment with your line. A `.env`
   copied from an older template has an uncommented empty `CHRONOS_PERSISTENCE_KEY=` line: fill
   that one in. Never add a second: Chronos refuses a key named twice, an empty line included.
   Then clear the terminal. Nobody and nothing else generates, sees, logs or handles this
   value; it is never typed into an agent, a handoff, a ticket or a chat. No example appears on
   this page on purpose.

   **One home: `<checkout>/.env`, and only there.** That is the file the backend reads under the
   shipped units: `settings.py` loads `.env` relative to the working directory
   (`src/chronos/config/settings.py:102`, `env_file=".env"`), and both units start in the checkout
   (`docs/ops/chronos-backend.service:25`, `docs/ops/chronos-worker.service:20`,
   `WorkingDirectory=%h/<CHRONOS_CHECKOUT>`). Every command on this page runs from the checkout and
   reads the same file, and step 6's compare reads it. But the units also load a private
   environment file (`docs/ops/chronos-backend.service:26`, `docs/ops/chronos-worker.service:21`,
   `EnvironmentFile=%h/<PRIVATE_ENV_DIR>/…`), and a value in the process environment beats the
   `.env`. That is where `EnvironmentFile=`, an `Environment=` line, `systemctl --user
   set-environment` and your own `export` put it: pydantic-settings reads the environment before
   the dotenv file (`BaseSettings.settings_customise_sources` returns `init_settings,
   env_settings, dotenv_settings, …` in that order), and it matches the name case-insensitively
   (`src/chronos/config/settings.py:105`, `case_sensitive=False`). A key line in any of those
   would be the key the backend runs on, and step 6's compare would never see it.

   **The one-home check** prints counts, never the value. Run it from `<checkout>` now, before
   step 6, and again in step 6 before you delete any copy. It asks systemd which environment files
   the two units load, drop-ins included (`systemctl --user show -p EnvironmentFiles`), and counts
   the key in every one. It stops if that list is not exactly the two private files the units'
   templates name, so a drop-in `EnvironmentFile=` at another path stops it too. Write
   `<PRIVATE_ENV_DIR>` as the absolute path, as systemd prints it (`%h` expanded).
   ```bash
   k=CHRONOS_PERSISTENCE_KEY; bad=0
   n=$(grep -c "^$k=" .env); echo ".env, exact line: $n (must be 1)"; [ "$n" = 1 ] || bad=1
   n=$(grep -v '^[[:space:]]*#' .env | grep -ic "$k"); echo ".env, any line naming it: $n (must be 1)"; [ "$n" = 1 ] || bad=1
   want=$(printf '%s\n' <PRIVATE_ENV_DIR>/chronos-backend.env <PRIVATE_ENV_DIR>/chronos-worker.env | sort -u)
   got=$(systemctl --user show -p EnvironmentFiles --value chronos-backend chronos-worker | sed -n 's/ (ignore_errors=[a-z]*)$//p')
   [ "$(printf '%s\n' "$got" | sort -u)" = "$want" ] || { echo "the units' environment files are not the two named here:"; printf '%s\n' "${got:-(none)}" | sed 's/^/  /'; bad=1; }
   while IFS= read -r f; do
     [ -n "$f" ] || continue
     [ -r "$f" ] || { echo "$f: not readable (fix the path)"; bad=1; continue; }
     n=$(grep -v '^[[:space:]]*#' "$f" | grep -ic "$k"); echo "$f: $n (must be 0)"; [ "$n" = 0 ] || bad=1
   done <<EOF
   $got
   EOF
   u=$(systemctl --user cat chronos-backend chronos-worker) || bad=1
   n=$(printf '%s\n' "$u" | grep -v '^[[:space:]]*#' | grep -ic "$k"); echo "unit files: $n (must be 0)"; [ "$n" = 0 ] || bad=1
   m=$(systemctl --user show-environment) || bad=1
   n=$(printf '%s\n' "$m" | grep -ic "$k"); echo "user manager environment: $n (must be 0)"; [ "$n" = 0 ] || bad=1
   n=$(env | grep -ic "$k"); echo "this shell's environment: $n (must be 0)"; [ "$n" = 0 ] || bad=1
   [ "$bad" = 0 ] && echo 'ONE HOME: OK' || echo 'STOP: the key is not in exactly one place'
   unset k n f u m bad want got
   ```
   The check must end `ONE HOME: OK`. `STOP: …` means a line above is not what it must be. Leave
   the key line in `.env` and remove every other one (or fix the path the check could not read),
   then re-run. Do not start step 6 until it says `ONE HOME: OK`.

6. **Escrow one copy off-host, encrypted to your recovery key alone, and prove it decrypts back
   off-host.** The tool below reads the key from a non-echoing prompt — type (or paste) the same
   64 hex characters you put in `.env`; it never takes them from the environment or the command
   line, so an unset variable cannot silently escrow an empty line — refuses anything but exactly
   one line of 64 lowercase hex, and encrypts the line with `age` to ONE recipient: your recovery
   key, `KEVIN_RECOVERY_RECIPIENT`, pinned in code in the restore drill (ruling K-20261023-003).
   The host identity is not a recipient. Nothing on this host can open the artifact, so the tool
   cannot prove it decrypts; that proof is yours, off-host, below.

   First, the one-home check (step 5): it must say `ONE HOME: OK`, or stop here. Then write the
   artifact into a fresh private directory **outside the checkout**. Do not write it into the
   checkout: there it would be an untracked file in the repository (`*.age` is not ignored, so a
   `git add -A` would stage the one escrow copy), and the checkout is writable by both units
   (`ReadWritePaths=` in the unit files). `ProtectHome=read-only` lets the units read
   `~/chronos-escrow` but not write it (no `ReadWritePaths=` entry covers it); what they could
   read is ciphertext that only your recovery key opens.
   `mkdir` fails if the directory already exists; then find out what it holds
   before you go on. `ls -ld` must show both lines owned by you, with no `w` in the group or other
   columns.
   ```bash
   mkdir -m 0700 ~/chronos-escrow && ls -ld ~ ~/chronos-escrow
   python -m chronos.operations.persistence_key_escrow --out ~/chronos-escrow/chronos-persistence-key.age --pretty
   ```
   Expect exit `0`, `"verdict": "ESCROWED"`, `"decrypt_verified": false`, one `recipients` entry
   (your recovery key), and the artifact's `ciphertext_sha256`.

   | `ESCROWED` proves (the report's `proved_on_host`) | It does NOT prove |
   |---|---|
   | the line you typed was exactly one line of 64 lowercase hex (an empty line is refused) | that the artifact decrypts to your key: only your recovery identity can open it, and it is never on this host |
   | `age` exited 0 | that the `.env` holds the same value you typed |
   | the artifact is a non-empty regular file that `age` created through the directory descriptor the tool holds, held open since, still at the name when the tool reports | where the artifact goes next, or that it gets there |

   Exit `2` with `code: KEY_MALFORMED` means the line was not 64 lowercase hex (nothing was
   written); `AGE_MISSING`, that `age` is not on PATH (nothing was written); `ARTIFACT_EMPTY`,
   that `age` exited 0 but the file it left is empty. After any failure once the file was
   created, the tool **deletes nothing** (a removal would be by name, and a name can be swapped
   under it in the last instant; it removes nothing, ever): `created_artifact` in the report
   names the ciphertext this run created by `path` and device:inode (`removed` is always
   `false`) — find it (`find <dir> -inum <inode>`) and shred it yourself, at its name or under a
   renamed entry. `TARGET_SWAPPED` means the output name stopped naming the file the tool wrote
   before it reported (something renamed or replaced it): the tool reads through the descriptor
   it holds, so the ciphertext this run created sits under the renamed entry; the foreign entry
   at the name is not the tool's and was not touched.

   **Then decrypt and compare, off-host. This is required before any copy is deleted.** On your
   own machine, where your recovery identity lives (never on this host, never on a fleet host),
   copy the artifact over, decrypt it with your recovery identity and compare its sha256 with
   the sha256 of the host's `.env` value: the one home, the file the check counted. The key never
   leaves the host and the decrypted escrow never leaves your machine: only the host-side digest
   crosses ssh, both values stay in shell memory, and nothing is printed or written to disk.
   `<host>` is this host's ssh name, `<checkout>` this checkout's absolute path, and the `scp`
   source is relative to your home on the host:
   ```bash
   scp <host>:chronos-escrow/chronos-persistence-key.age .
   a=$(age -d -i <your recovery identity> chronos-persistence-key.age) && [ ${#a} -eq 64 ] &&
     b=$(ssh <host> "sed -n 's/^CHRONOS_PERSISTENCE_KEY=//p' <checkout>/.env | sha256sum") &&
     [ "$(echo "$a" | sha256sum)" = "$b" ] && echo MATCH || echo 'NO MATCH: delete nothing'
   unset a b
   ```
   `MATCH` is the proof the host cannot give. On anything else (`NO MATCH`, an age error, an ssh
   error) delete nothing: keep the `.env` and every copy of the artifact, and find out why (a
   `.env` line written with quotes, `export` or a trailing space reads as a mismatch). Only after
   `MATCH`, run the one-home check (step 5) on the host once more. It must still say
   `ONE HOME: OK`; on `STOP`, delete nothing and fix the file it names, because the key the
   backend would run on is then not the one you just proved. Then put the artifact where it
   lives (password manager attachment, or the same place the backup escrow goes), and remove the
   host copy yourself, and any other transient copy (the tool never deletes an artifact):
   ```bash
   shred -u ~/chronos-escrow/chronos-persistence-key.age && rmdir ~/chronos-escrow
   ```
   One escrow copy, exactly; the `.env` on
   the host is the only working copy. Your recovery private key never exists on a fleet host,
   and no key that can open this artifact does either.

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

   That 200 proves only that the process answers: `/health` says `"status":"ok"` under a
   recovery hold too. **Steps 8 and 9 wait for the gate below.**

   **Expect the recovery hold, reason `database_replaced` (ADR-0054). It is not an incident.**
   Step 3 moved only `chronos.db` and its `-wal`/`-shm`. The installation marker
   `state_generation.json` beside the kill-switch file survives, and a fresh store has no
   `installation_identity` row. A marker with no row reads as a database replaced beneath
   surviving state files, so the backend boots held: read-only, unreconciled, no autonomy
   runtime. Read the hold from `<checkout>`. The token comes from the backend's own token file
   (`BACKEND_TOKEN_FILE`, `data/backend_api_token` by default; use your path if it differs).
   The two commands that carry it run in a fresh shell, `/usr/bin/env -i PATH=/usr/bin:/bin
   /bin/bash --noprofile --norc`, started by absolute path with an empty environment and given
   the command as its `-c` argument. That shell has none of yours: no function, alias, hash
   entry, xtrace, `BASH_ENV` or startup file, no `HOME` (so no `~/.curlrc`), and a `PATH` of two
   root-owned directories. The token is read there, handed to curl as a header file
   (`-H @<(printf ...)`, so it is in no process's argv) and never exists in your shell; `-q`, as
   curl's FIRST argument, also stops a curlrc found another way. Your shell may be bash, zsh or
   dash. What stays with you: add `-v`, `--trace`, `--trace-ascii`, `--config` or `-K` to either
   command and curl prints the token. Do not.

   Threat model. These commands protect the token from accidental disclosure by your own shell
   and tool configuration: history, xtrace, curl config, aliases, functions, hash entries,
   startup files, argv. They do not protect against anyone who can already act as your user or
   as root, who can read the token file directly: that includes a hostile `LD_PRELOAD` or
   loader environment, writable system binaries or directories, `ptrace`, a function named
   `/usr/bin/env` and a DEBUG trap under `extdebug`. The preflight below checks two things only:
   the loader variables in your own shell (`LD_PRELOAD`, `LD_AUDIT`, `LD_LIBRARY_PATH`), and the
   owner and mode of the system paths it names. That path check runs `stat` and `bash` from
   `/usr/bin` and `/bin`, so it catches a writable or non-root-owned path only while those tools
   are intact: a compromised `/usr/bin` can forge its answers, and no check run from it can prove
   otherwise. It does not check `ptrace`, the `/usr/bin/env` function or the DEBUG trap.
   The log is your `LOG_FILE` (`logs/chronos.log` by default).

   Preflight first, once. It needs `/usr/bin/env`, `/bin/bash` and a curl 7.55 or newer in
   `/usr/bin` or `/bin` (`-H @file`), each owned by root and writable by no one else, and no
   loader override in your environment:
   ```bash
   [ -z "$LD_PRELOAD$LD_AUDIT$LD_LIBRARY_PATH" ] && echo "loader: clean" || echo "loader: STOP"
   /usr/bin/env -i PATH=/usr/bin:/bin /bin/bash --noprofile --norc -c 'for p in /usr/bin /bin /usr/bin/env /bin/bash "$(command -v curl)"; do read -r o m < <(stat -L -c "%U %a" "$p"); m=${m: -3}; if [[ $o == root && $m =~ ^[0-7][0145][0145]$ ]]; then echo "ok $p"; else echo "STOP $p $o $m"; fi; done; curl -q --version'
   ```
   It must print `loader: clean`, then five lines `ok <path>` (`/usr/bin`, `/bin`, `/usr/bin/env`,
   `/bin/bash` and the curl, `/usr/bin/curl` or `/bin/curl`), then curl's version lines, the
   first `curl 7.55` or later. **STOP, and run nothing below, if** it prints `loader: STOP`, any
   `STOP <path>` line, an error, or an older curl. Then:
   ```bash
   curl -fsS http://127.0.0.1:<port>/health | python -I -c '
   import json, sys
   h = json.load(sys.stdin); o = h["observations"]
   print("startup_faults:", " ".join(o["startup_faults"]) or "none")
   print("reconciliation:", h["reconciliation_status"])
   print("tasks:", " ".join(t["name"] + "=" + t["state"] for t in o["tasks"]))
   '
   /usr/bin/env -i PATH=/usr/bin:/bin /bin/bash --noprofile --norc -c 'curl -q -sS -w " HTTP %{http_code}\n" -H @<(printf "X-Chronos-Token: %s\n" "$(<data/backend_api_token)") http://127.0.0.1:<port>/autonomy/alerts'
   grep -F '"event":"recovery_hold_engaged"' logs/chronos.log | tail -n 1
   ```
   It must print these four lines, then one log line whose message begins `This boot cannot
   prove it does not follow a restore (database_replaced)`:
   ```
   startup_faults: recovery_unverified
   reconciliation: PENDING
   tasks: autonomy=NOT_EXPECTED lease_heartbeat=RUNNING reconciliation=NOT_EXPECTED
   {"detail":"This backend booted under a recovery hold and is running read-only: the state directory names an installation this database has never witnessed; the database was replaced beneath surviving state files. Acknowledge it with a note at POST /live/recovery/acknowledge and restart."} HTTP 409
   ```
   `GET /autonomy/alerts` only reads, but it sits behind the same writer check as every mutating
   route: its 409 is the hold's refusal, and its 200 later means writes are allowed.
   **Anything else is not the cutover's state: stop the backend (`systemctl --user stop
   chronos-backend`), acknowledge nothing, and stop here.** That includes another reason in the
   log or the 409 (`state_directory_lost`, `marker_unreadable`, `installation_mismatch`,
   `restore_pending`), another startup fault beside `recovery_unverified`, and no hold at all:
   `startup_faults: none` here means the marker was not where the backend looks, so the
   live-safety state this host ran with is missing. `python -m chronos.cli campaign status` and
   `campaign preflight` cannot show this reason: until the restart after your acknowledgement they
   report the fresh store's missing `installation_identity` row as UNVERIFIED. That restart writes
   the row from the marker's id (ADR-0054, amended 2026-10-25), and from then on both read the pair
   as consistent: `recovery hold: CLEAR` and `PASS [ADR-0054] state identity`.

   **Then you acknowledge it: your act, with a typed note** saying this is the expected N3
   database replacement. The same token file, in the same fresh shell:
   ```bash
   /usr/bin/env -i PATH=/usr/bin:/bin /bin/bash --noprofile --norc -c '
     curl -q -fsS -X POST http://127.0.0.1:<port>/live/recovery/acknowledge \
       -H @<(printf "X-Chronos-Token: %s\n" "$(<data/backend_api_token)") \
       -H "Content-Type: application/json" \
       -d "{\"note\": \"N3 cutover <date>: expected database_replaced hold; the keyed store replaced the archived schema-17 store (envelope <envelope from step 3>)\"}"
   '
   ```
   Expect `{"acknowledged":true,"reason":"database_replaced","binding":"<64 hex>","effective":"next start"}`.
   A `reason` other than `database_replaced`: stop. The acknowledgement binds this exact
   observation and takes effect at the next start, so this process stays held. It does not
   disengage the kill switch, arm, or activate a mandate.

   **Restart, then prove the hold is gone, before step 8.** `systemctl --user restart
   chronos-backend`, then `systemctl --user is-active chronos-backend` → `active`, then the
   `/health` and `/autonomy/alerts` commands above again, but not the `grep`: the log still holds
   the held boot's `recovery_hold_engaged` line, which proves nothing now. They must print:
   ```
   startup_faults: none
   reconciliation: RECONCILED
   tasks: autonomy=NOT_EXPECTED lease_heartbeat=RUNNING reconciliation=RUNNING
   [] HTTP 200
   ```
   `startup_faults: none`: the hold is cleared. `HTTP 200`: writes are allowed (the body lists
   unacknowledged alerts; it may hold the mandate alert below instead of `[]`).
   `reconciliation: RECONCILED` with `reconciliation=RUNNING`: startup reconciliation ran and
   the refresher runs (`STARTING` in the first seconds: re-run). `autonomy=NOT_EXPECTED` is
   right until step 9, for one of two reasons: no `AUTONOMY_MANDATE_FILE` is configured, or the
   configured mandate still carries the unkeyed fingerprint. Then the log says `Persistent
   mandate is scoped to a different account; autonomy stays inert`, and the alert list holds
   `"kind":"autonomy.mandate_invalid"`. Step 9 re-authors the mandate; autonomy starts after it.
   **Anything else (a startup fault, a 409, `reconciliation` not `RECONCILED`,
   `autonomy=RUNNING`: a grant active before step 8 proved the key) means stop the backend and
   stop here.** A bare `/health` 200 is not this proof. Steps 8 and 9 start only after it passes.

8. **Bind — and prove the store bound under the ESCROWED key.** The first bound run writes the
   `database_scope` row with the KEYED fingerprint. Before it binds, the backend logs the one key
   source it counted, at INFO from `chronos.runtime`: `persistence key from: the dotenv file
   <checkout>/.env`. It must name `<checkout>/.env`; `the process environment` means the key
   lives where step 5 forbids: stop. A refusal at start names every source and its count
   (`CHRONOS_PERSISTENCE_KEY is named 2 times: …`), or a `$` in the key line, never the value:
   remove the extra source or replace the malformed key line (step 5) and start again. The proof
   is one comparison: the fingerprint
   computed from `<checkout>/.env`'s key ALONE — the key you escrowed in step 6; never the
   shell's, never the unit's — against the bound row. On an IBKR-paper host that is the scope
   line of `python -m chronos.cli b1-preflight`, which reads the file and nothing else:
   `CLEAR [scope] already bound to this paper account`. On a demo host `b1-preflight` cannot
   give it — it refuses before its scope line, and its scope CLEAR is `ibkr`/`paper` by
   construction (`b1_preflight.py:564`; C1 F1) — so make the same comparison from `<checkout>`
   with a command that reads the key from the FILE by construction and never asks the process
   environment. The `sqlite3` line reads the row. The `python` line reads `.env` with the
   loader's own `dotenv_values`, takes the one exact `CHRONOS_PERSISTENCE_KEY=` line — `STOP`
   when it is missing, doubled or present in another form — and prints the fingerprint through
   the repo's `account_fingerprint`; it never prints the key. It reads the file LITERALLY
   (`interpolate=False`): a value written as `${VAR}` stays the text `${VAR}` and fails the
   64-hex check, instead of becoming whatever the environment holds under that name, which is
   what `dotenv_values` does by default (C1-F fix round 2, kimi delta r1 P2-1):
   ```bash
   sqlite3 "file:data/chronos.db?mode=ro" "SELECT broker_mode, environment, account_fingerprint FROM database_scope"
   python -I -c '
   import re, sys
   from dotenv import dotenv_values
   from chronos.utils.identifiers import PersistenceKey, account_fingerprint
   try:
       lines = open(".env", encoding="utf-8").read().splitlines()
   except FileNotFoundError:
       sys.exit("STOP: no .env here; run this from <checkout>, where step 5 wrote it. Delete nothing.")
   named = [l for l in lines if re.match(r"\s*(export\s+)?chronos_persistence_key\s*=", l, re.I)]
   if len(named) != 1 or not named[0].startswith("CHRONOS_PERSISTENCE_KEY="):
       sys.exit(f"STOP: {len(named)} line(s) name the key in .env; want exactly one, in the exact form (step 5). Delete nothing.")
   try:
       key = PersistenceKey.from_hex(dotenv_values(".env", interpolate=False)["CHRONOS_PERSISTENCE_KEY"])
   except (KeyError, TypeError, ValueError):
       sys.exit("STOP: the key line is not CHRONOS_PERSISTENCE_KEY=<64 lowercase hex> (step 5). Delete nothing.")
   print(account_fingerprint(sys.argv[1], key=key))
   ' DU1234567
   ```
   `python -I` runs isolated — no `PYTHON*` variables, no user site, no cwd on the import path,
   so a file dropped in the checkout root cannot shadow `dotenv` or `chronos` — and the command
   reads no environment variable at all, so a value set by a shell, by a unit, or by Python
   startup code cannot enter it (an `env -i` form did not close that last path: a `.pth` or
   `sitecustomize` can set the key after `env` emptied the environment, and `Settings` prefers
   it; C1-F fix round 1, kimi P1-1). Trust root, stated: the checkout's venv and source are the
   code the backend itself runs — a hostile `.pth` or `sitecustomize` in that venv is outside
   what any runbook check can defend, and this step depends on the process environment not at
   all. Expect exactly one row, `demo|DEMO|<64 hex>`, and the SAME 64 hex from the `python` line.
   A line beginning `STOP:` means `.env` is not one exact `CHRONOS_PERSISTENCE_KEY=<64 hex>`
   line — or is not in this directory at all: fix the file (step 5), run from `<checkout>`, and
   delete nothing. The account id is the one the backend bound: on the demo
   broker `DEMO_ACCOUNT_ID` = `DU1234567` (`src/chronos/broker/demo.py`; a demo host has no
   `IB_ACCOUNT_ID`, so the argument is required); on IBKR paper your `IB_ACCOUNT_ID`.
   **A mismatch right after the first start — a different 64 hex, or `b1-preflight`'s
   `REFUSE [scope] this database is already bound to a different broker scope` — means the
   backend bound under a key other than the escrowed one**: a second key source reached the
   unit (step 5's one-home check names every place to look). Stop the backend; remove the extra
   source; re-archive the fresh store by hand — move `data/chronos.db` and its `-wal`/`-shm`
   aside by name; it was bound under the wrong key and holds nothing worth keeping — then start
   again, pass step 7's recovery-hold gate again (the new store holds no acknowledgement, so
   the hold comes back), and repeat this step.
   Never change `DATABASE_URL`: a new URL leaves a store bound under
   a key nobody escrowed (the REFUSE text names both causes, a different broker scope or a
   different persistence key, and no longer prescribes a new URL). No row, a second row, or a
   different value:
   stop; the store is not bound to the scope you think.

9. **Re-author the mandate BEFORE the first start of autonomy — dated window = 0.** The
   mandate's `account_fingerprint` must be the keyed value:
   `python -m chronos.cli mandate fingerprint --account-id DU1234567` prints it from the
   configured key: the fingerprint is stdout's first line, and stderr carries one line naming the
   key's source, `persistence key from: the dotenv file <checkout>/.env`. A refused key prints
   `persistence key refused: …` (the sources and counts) on stderr and `no persistence key
   available: …` on stdout: fix the source (step 5) and run it again. The argument is required
   on a demo host: without it the command reads
   `IB_ACCOUNT_ID` from settings, which a demo host does not set, and stops with
   `no account id available; pass --account-id or set IB_ACCOUNT_ID` (C1 F7). Its value is the
   account the backend bound in step 8: `DEMO_ACCOUNT_ID` = `DU1234567` on the demo broker
   (`src/chronos/broker/demo.py`), your `IB_ACCOUNT_ID` on IBKR paper (there the argument may be
   left out). Write the printed value into the mandate's `account_fingerprint`; its
   `effective_from` is the first instant anything accepts the keyed value; nothing accepts the
   old value at any time, so there is no window to close. Then
   `python -m chronos.cli mandate check --file <mandate> --account-id DU1234567` (the same id)
   must print `STATUS             VALID`, `[NOTE] account-match` and a SUMMARY line that begins
   `0 blocking` — a BLOCKING `account-mismatch` means the old fingerprint is still in the file.
   Only then start the worker and enable autonomy per the shadow-campaign runbook.

10. **Retention — 90 days.** The envelope under `--out` is a pre-cutover backup (its manifest
    says `schema_version:17`); the pruner deletes it after 90 days, as the ruling says. Run
    it with `--root /var/backups/chronos`, the same directory as `--out` (it reads only the
    direct children of `--root`; [`RESTORE-DRILL.md`](RESTORE-DRILL.md), "Pruning old backups"):
    `python -m chronos.operations.backup_pruner --root /var/backups/chronos --pretty`, then add
    `--apply`. Keep `data/chronos.db.archived-<stamp>` (and its
    siblings) until you are satisfied the keyed store is right, then the same 90 days; they are
    the unkeyed history and the rollback, nothing else reads them.

## Rollback

- **Before any new row exists in the keyed store** (nothing started, or started and stopped
  without binding): stop the service; remove the sentinel directories first — `rmdir
  data/chronos.db data/chronos.db-wal data/chronos.db-shm data/chronos.db-journal` (and any
  sentinel the report's `sentinels` lists at an `.archived-<stamp>` name after a refusal); if
  the backend already ran, the fresh keyed `chronos.db` sits at the live name instead and you
  are in the next bullet; then rename the moved-aside files back by name, with the form that
  REFUSES an occupied live name out loud —
  `mv --update=none-fail data/chronos.db.archived-<stamp> data/chronos.db` and the same for the
  `-wal`/`-shm` siblings that were moved. GNU coreutils 9.5 or newer (`mv --version`; the
  rehearsal host runs 9.7): on an occupied name it prints `mv: not replacing 'data/chronos.db'`,
  exits 1 and moves nothing. Never `mv -n`: measured on 9.7 it declines an occupied name
  SILENTLY with exit 0, so a rollback onto a keyed `chronos.db` the backend already created
  looks done and is not (C1 F3). An older coreutils rejects the flag itself (`invalid argument`,
  exit 1, nothing moved); there — and never with a check-then-move, which a live name created
  between the check and the move would defeat — link the archived file to the live name and
  remove the archived name only after the link succeeded:
  `ln -T data/chronos.db.archived-<stamp> data/chronos.db && rm data/chronos.db.archived-<stamp>`
  and the same for `-wal`/`-shm`. `link(2)` refuses an existing name inside the one call
  (`Already exists`, exit 1, nothing changed): nothing is renamed or unlinked over the live
  name, so a store that appeared there a moment earlier is never overwritten; `-T` makes a
  directory at the live name a refusal too, never a link inside it; the two names share
  `data/`, one filesystem. Both forms are one no-clobber kernel call each — on an occupied name
  `mv --update=none-fail` issues a single `renameat2(RENAME_NOREPLACE)` and `ln -T` a single
  `link(2)`, measured with strace (C1-F fix round 1, kimi P1-2). Then
  **check**: `ls -la data/` must show no `.archived-<stamp>` name and the three regular files at
  the live names; a `not replacing` or `Already exists` line means a live name was occupied —
  stop and look before anything else. Then check out the pre-cutover code (`git checkout
  <pre-cutover sha>`, the schema-17 commit you wrote down and checked under "Before you start" —
  never the post-N3 `HEAD`: a schema-17 store is refused by the post-N3 code, by design);
  start. The move refuses if the live name exists; a sentinel still there says
  `mv: cannot move 'data/chronos.db.archived-<stamp>' to
  'data/chronos.db/chronos.db.archived-<stamp>': Permission denied` — `mv` aims inside the
  directory and its 0500 mode refuses (C1 F4); `rmdir` the sentinel first. Never force it.
- **After new rows exist in the keyed store:** going back drops them. Decide that explicitly;
  the archive and the moved-aside files restore the pre-cutover state at any time, with the
  pre-cutover code.
- **The archive at any later time:** the envelope restores through the drill's `restore`
  (`age -d -i data/keys/backup-host.age`, or your recovery key) exactly as any backup does
  (`RESTORE-DRILL.md`, step 4); a restored schema-17 copy is usable only under the pre-cutover
  code.
- **The key is lost after cutover:** the keyed store cannot bind and has no proof path;
  the escrow copy is the only supported recovery (step 6): decrypt it off-host with your
  recovery identity (the host identity cannot open it), put the value back in the 0600 `.env`, start. If the escrow copy is gone too,
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
| `PARENT_NOT_PRIVATE` | `data/` (the store's parent) is owned by another uid, or writable by its group or by others — the sentinels would be removable by another uid | as the owner: `chmod 0700 data` (Before you start); re-run |
| `PATH_NOT_PROTECTED` | a directory above `data/` is not part of the protected chain: owned by neither root nor you, writable by its group or by others (a sticky `/tmp`-style one included), or a symlink — or the path you gave carries a `..` component | move the deployment under a protected chain, or fix the named directory as its owner (Before you start, `ls -ld`); re-run |
| `PATH_DISPLACED` | after the check, a path component stopped naming the directory the command holds open (renamed away or replaced) | nothing moved, or `moved_aside` is exact inside the held directory (named by inode); find out who wrote under a stopped service before anything starts |
| `KEYS_REFUSED` | the drill's age keys (identity 0600, exactly the two recipients) | the drill runbook, "Encryption at rest" |
| `SCHEMA_UNREADABLE` / `STORE_UNREADABLE` | the store could not be read as a Chronos store | stop; do not guess — inspect by hand, read-only |
| `SCHEMA_ALREADY_CURRENT` | the store is already at the running `SCHEMA_VERSION` | this is not a pre-cutover store; nothing to do |
| `NO_SCOPE_ROW` | the store was never bound | nothing to archive; migration 0017 upgrades it in place |
| `WRITER_LEASE_LIVE` | a backend held the writer lease within the last 30 s | step 1, then re-run |
| `WAL_FRAMES_PRESENT` | uncheckpointed write-ahead frames beside the store | step 1; the last writer's clean close checkpoints them |
| `OPEN_BINDING` / `BINDING_UNREADABLE` | a managed position is open, or its ledger cannot be replayed | flatten at the broker, reconcile, re-run / inspect the stream by hand |
| `NO_RECONCILIATION_RUN` / `RECONCILIATION_STALE` / `RECONCILIATION_NOT_CLEAN` / `RECONCILIATION_UNREADABLE` | the latest run is missing, older than one hour, not `RECONCILED`, or unreadable | start → reconcile → stop → re-run within the hour |
| `POSITION_OBSERVED` / `PROVENANCE_MISSING` | the latest run recorded a broker position (the message breaks it down by origin class) | flatten at the broker, reconcile, re-run |
| `RESTORE_TARGET_NOT_PROTECTED` | `--restore-into`'s parent chain is not protected: a directory from `/` to its parent is owned by neither root nor you, writable by its group or by others (`/tmp` included), or a symlink; or the parent is not yours | use `/var/backups/chronos/n3-verify` under a chain you own (Before you start); re-run — nothing was backed up |
| `RESTORE_TARGET_NOT_FRESH` | `--restore-into` already exists (empty or not), or could not be created `0700` and yours | name a path that does not exist; never reuse or pre-create it — the command creates it; re-run |
| `RESTORE_COPY_NOT_REMOVED` | the drill verified the archive but could not remove its cleartext copy under `--restore-into` (the `drill.failures` line names what remains and why: an entry that is not the inode it verified, an entry it did not write, an errno, a renamed directory) | nothing moved and the envelope is named; remove `/var/backups/chronos/n3-verify` by hand, find out why, re-run (new envelope and stamp) |
| `DRILL_REFUSED` | the drill refused before verifying (an envelope name collision, a rename error) | the drill runbook's `refused:` row; nothing moved |
| `TARGET_EXISTS` / `SOURCE_SWAPPED` / `MOVE_FAILED` | the move-aside could not start, or the source changed under the command | step 3's bullets |
| `LIVE_NAME_OCCUPIED` | a live name of the store's namespace (`-wal`/`-shm` not admitted by the gate, `-journal`) holds an entry, so no sentinel can take it; or, after the renames, a live name no longer holds this run's sentinel | step 3's bullet: nothing moved in the first case; find what wrote it, move it by hand, re-run |
| `WRITER_OPEN` | a process holds the store (or, at the move, a sidecar) open for writing, or opened it for writing while the command ran — the kernel read lease was refused or broken | step 1; a stopped backend that still holds the store counts; then re-run |
| `WRITER_EXCLUSION_UNAVAILABLE` | the kernel would not grant the read lease (not the file's owner, or a filesystem without leases) | run as the user that owns `data/chronos.db`, on a local filesystem |
