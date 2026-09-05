# ADR-0054: A boot that follows a restore comes up read-only and unreconciled

- Status: Accepted (owner review required before merge)
- Date: 2026-09-04
- Deciders: opus-3 seat (author), owner (merge gate)
- Related: R-72, D-69, ADR-0049 (R-66, the kill-engaged half), ADR-0017 (mandate
  auto-activation), ADR-0020 (readiness cadence), R-24 (the writer lease),
  `docs/VISION_COMPLETION_PLAN.md` §6 finding 3, `docs/BACKUP_AND_RECOVERY.md`

## Context

ADR-0049 closed one half of §6 finding 3: a live-safety file missing *after this
installation wrote one* reads closed instead of reading as a fresh install. Its own
Consequences section named what it left open, and this ADR is that list:

> Only the *kill-engaged* half of §6 finding 3 is closed. Booting **read-only** and
> **unreconciled** after a restore is untouched here, as is the mandate file's
> auto-activation on boot (ADR-0017).

It also disclosed a residual: a restore that brings back **nothing** — the marker
included — still presents as a fresh install. That residual is not an oversight in
ADR-0049; it is forced by the marker being a single store. The absence of the marker
and the absence of everything are the same bytes, so no rule read from inside the
state directory can tell them apart.

Three things therefore still happen on a boot that follows a restore: the backend
comes up as a full writer, it publishes submission readiness from a reconciliation
run against broker state it has no reason to trust, and a restored
`AUTONOMY_MANDATE_FILE` auto-activates without an operator anywhere near it.

## Decision

Add a **second witness in an independent durable store**: record the state
directory's installation id in the Chronos database, and compare the two at every
writer startup. Disagreement is a **recovery hold**.

| marker | database identity row | reading |
|---|---|---|
| absent | present | the state directory is gone — **hold** |
| present | absent | the database was replaced beneath surviving files — **hold** |
| present | present, different id | two snapshots mixed — **hold** |
| unreadable | any | proves nothing; fail closed — **hold** |
| present | present, same id | consistent — no hold |
| absent | absent | a fresh install — no hold |

Row 1 is ADR-0049's disclosed residual, closed whenever the database survived to
witness it. To make it reachable, the writer **seeds** the marker at startup with an
empty `materialized` set: seeding says "this installation exists", never "a file was
written", so every R-66 reading is unchanged.

A hold means three things, and each is enforced where the callers actually are:

1. **Read-only at the route layer.** `BackendState.may_write` is the one predicate —
   lease held *and* no hold — and `require_writer` refuses on it. The two endpoints
   `routes/live.py` deliberately leaves lease-free (disarm, engage kill) stay
   reachable: both only ever *remove* authority, and a restore is not a reason to put
   the emergency stop out of reach.
2. **Read-only at the submission boundary.** The lifespan's lease verifier is built
   from the same `may_write`, so the autonomy handoff — which never passes through a
   route — is refused at the transmit boundary too.
3. **Unreconciled.** Startup reconciliation is skipped, so the latch stays `PENDING`,
   and the ADR-0020 refresher task is not created — a refresher would re-arm exactly
   the readiness the hold exists to withhold. The autonomy runtime is not built, which
   is what keeps ADR-0017's auto-activation from re-arming a restored mandate.

**The hold does not touch the lease.** This process still acquires and heartbeats it.
Releasing it would hand write authority to the next process to start, which reads the
same unverified state and would decide the same way; single-writer semantics stay
intact and the hold is orthogonal to them.

**Clearing it is a typed operator act with a note**, the shape
`LiveKillSwitch.disengage` already uses. `POST /live/recovery/acknowledge` records
the **exact** observation — reason, both installation ids, and the restore witness
token — so it retires the restore it was written for and never becomes a standing
permission to ignore restores. It is gated on lease ownership rather than
`require_writer`, because an acknowledgement a hold locks out is no escape at all. It
takes effect at the **next start**. It does **not** disengage the kill switch and does
**not** activate a mandate; those are their own typed acts, each with its own note.

**Upgrades adopt rather than hold.** Migration 0012 writes an adoption sentinel (an
identity row with a NULL id) so an existing deployment adopts the marker beside it
instead of reading its own upgrade as a replaced database. A database created by
`create_all` has no row at all, and that difference is exactly what keeps the
adoption from swallowing the replaced-database case.

**And the window that buys.** The sentinel is resolved by the first writer boot after
the upgrade, and until it is resolved there is no second witness to disagree with. So
on that one boot, and only that one: a pre-0012 database restored beside surviving
state files *adopts* the marker rather than holding, and a pre-0012 database beside a
lost state directory *mints* a fresh identity rather than holding. Both close the
moment the row carries a real id. The rule this implies is an operator rule, not a
code one, and it is in `docs/BACKUP_AND_RECOVERY.md`: **the first boot after upgrading
to migration 0012 must not be a boot after a restore.** Making the sentinel itself hold
was rejected — it would boot every existing deployment held, on an upgrade none of them
asked to be interrogated about.

## Why not the alternatives

- **Key on the database file's `(st_dev, st_ino)`.** A genuine restore detector — a
  `cp` or `tar` restore makes a new inode — but device numbering is not stable across
  host reboots on every filesystem, so it would fail closed on ordinary restarts.
- **A per-boot monotone epoch in both stores.** Catches a same-installation rewind
  that id equality cannot. A crash between the two writes then false-positives, and
  tolerating one step of skew reopens precisely the "yesterday's snapshot, one restart
  a day" case the epoch was for.
- **Fold the hold into `read_only`.** That flag means "another process holds the
  lease"; the heartbeat and the startup-abort path both branch on it, so a process
  under a hold would stop renewing a lease it still holds and let a second backend
  take it.
- **Re-run the skipped startup work when the acknowledgement lands.** More machinery
  than the case earns. "Acknowledge, then restart" is the procedure
  `docs/BACKUP_AND_RECOVERY.md` already walks.

## Consequences

- A partial or mixed restore now boots refusing every write and never publishing
  readiness, until an operator types a note. Disruptive by design; the reason string
  says exactly which of the six rows fired.
- Disengaging the kill switch is writer-gated and therefore also held. That is the
  intended order: acknowledge where the state came from first, then decide about the
  emergency stop.
- **The residual (R-72), stated plainly.** `data/live_kill_switch.json` and
  `data/chronos.db` live in the same directory by default, so a **self-consistent
  wholesale restore** of that directory carries both witnesses from one snapshot and
  remains byte-indistinguishable from a clean restart. Only a witness from outside the
  directory can close it: `chronos.recovery restore` now leaves `recovery_pending.json`
  behind, and the manual restore procedure asks the operator to create one. A wholesale
  restore done by hand *without* that step is not detected, and the manual step in
  `docs/BACKUP_AND_RECOVERY.md` remains its only control.
- Only the writer evaluates. A read-only backend must not write a first witness, and it
  already satisfies everything a hold enforces.
- **A second residual, one boot wide:** the 0012 upgrade window above. It is disclosed in
  R-72 and carries an operator line rather than a guard, because the alternative holds
  every existing deployment on upgrade.

## Verification

```bash
.venv/bin/python -m pytest -q tests/safety/test_restore_recovery_hold.py
```

Twenty-three tests: the six-row detection matrix and the witness binding as pure
functions, then the three consequences, the lease that is deliberately kept, the
acknowledgement's note requirement, reachability, pair binding, next-start effect, and
its independence from the kill switch — each over the real app and its real lifespan,
booted twice with state deleted in between. Migration 0012's adoption sentinel is
covered in `tests/integration/test_migrations.py`, and the restore witness in
`tests/integration/test_recovery_measurement.py`.
