"""N3 cutover archive — strategy (iii): archive the pre-N3 store, verify it, move it aside.

One command, owner-run, with every Chronos process stopped::

    python -m chronos.operations.cutover_archive --db data/chronos.db \\
        --out /var/backups/chronos \\
        --restore-into /var/backups/chronos/n3-verify [--apply] [--pretty]

Dry-run (the default) runs the gate below, proves ``--restore-into`` by the same read-only
check ``--apply`` uses (a protected chain, an absent leaf — a ``/tmp`` target is refused at the
dry run, C1 F5), prints what ``--apply`` would do, writes nothing to the store and creates no
envelope. One directory effect is SQLite's, not the command's: a
read-only connection on a WAL-mode store leaves its reader bookkeeping beside it — an empty
``chronos.db-wal`` and a ``chronos.db-shm`` — and nothing else; ``--apply`` moves them aside
with the store. ``--apply`` runs, in this order, each step a typed stop:

0. **One source capability** (:class:`chronos.operations.restore_drill.BoundSource`): the
   store is opened no-follow and HELD until the last rename is proven, and the one read-only
   connection every fact is read through is proven to have opened that very inode (SQLite
   resolves a pathname before it opens, so the proof is the descriptors this process gained);
   a swap at the name between the two opens is ``SOURCE_SWAPPED`` before any fact is read.
   The drill backs up through the same connection and its manifest records the inode.
   **Writer exclusion (r2):** before the gate the command takes a kernel READ LEASE on the held
   store (``fcntl(F_SETLEASE)``): the kernel grants it only while no process has the file open
   for writing — a writable SQLite connection, idle or not, holds it O_RDWR — and from then on
   blocks every writer's open until the command returns; a writer already holding the store is
   ``WRITER_OPEN`` before any fact is read, a lease the kernel will not grant for another reason
   (not the file's owner, a filesystem without leases) is ``WRITER_EXCLUSION_UNAVAILABLE``. The
   store is never opened for writing; the lease lives on the read-only descriptor.
1. **The flatten gate** — read-only, from the store ALONE: never a broker call, never an
   operator override, no ``--force``. The rows are read through the bound connection over a
   plain engine (never ``immutable=1``, so a WAL that exists is honoured; never ``Database``,
   whose pragma listener and ``initialize()`` are for the running schema). What the store must
   prove: it is a regular file with regular sidecars; its ``schema_version`` is BELOW the running
   ``SCHEMA_VERSION`` (a store already at the running version is never archived); it carries a
   ``database_scope`` row; no writer lease is live (the backend holds and renews the row while it
   runs and deletes it on a clean stop); no uncheckpointed write-ahead frames sit beside it
   (an unclean stop, or a process still open); EVERY ``managed_position_bindings`` row replays
   — hash chain verified — to a CLOSED position (``rehydrate_position``, remaining quantity 0);
   the latest ``reconciliation_runs`` row exists, is ``RECONCILED`` with a ``RECONCILED``
   readiness latch, completed within ``MAX_RECONCILIATION_AGE``, and recorded NO broker
   position (the ruling: "no open position at cutover"; when it refuses, the count is broken
   down by origin class from the run's own ``position_provenance`` rows). At the end of the
   gate the sidecars present (``-wal``/``-shm``, each a regular file, the ``-wal`` without
   frames) are ADMITTED: opened no-follow and held, their identities pinned; this is the whole
   set the move may rename — a sidecar that appears later is never renamed: it occupies a live
   name the move needs for a sentinel and refuses it (``LIVE_NAME_OCCUPIED``; with frames,
   ``WAL_FRAMES_PRESENT``). A ``-journal`` beside the store refuses at the gate for the same
   reason.
2. **The R-2 restore drill**, reused whole through
   :func:`chronos.operations.restore_drill.run_archive_drill` over the same capability: the
   drill runs the flatten gate ITSELF over the held source (r3, Daybreak P1-2 — the run above
   served the dry-run report and the sidecar admission; a store the gate refuses never reaches
   a backup) and keeps that run's archive ACCEPTANCE (the head read, its numeric version —
   below the running one — and the held inode) as a closure inside its own frame (r4): not a
   method on the capability, not a value or type a caller could construct, not a parameter of
   any function at module scope — every module-level function of the drill performs the
   repository's acceptance only. Then sqlite's online backup API through the bound
   connection, ``age`` to the two recipients named by the existing custody variables, one
   atomically published envelope whose manifest records the source inode, an isolated restore
   and its verifiers — by that admission: the envelope's source identity, the exact head (a
   retired schema that the running repository refuses by design; see the drill).
3. **Verdict ``ARCHIVE_VERIFIED`` required** — the drill's distinct verdict for a bound
   retired-schema archive, never the repository's ``VERIFIED``. Otherwise nothing moves: the
   live store is untouched and the report names the envelope that exists (the drill's cleartext
   restore copy is removed either way — N3-4 — or the report says why not).
4. **ONLY THEN the store moves aside — by EXCHANGE with a sentinel, never delete, and the
   live namespace is never vacant (r3):** in the store's directory, reached by the drill's
   no-follow walk: the bound connection is closed and every admitted sidecar is leased likewise
   (a process holding one open read-write — a read-only SQLite connection does — is
   ``WRITER_OPEN``); the store's lease must be whole; the manifest's ``source_identity`` must be
   the held inode (the archive IS of this store); the admitted ``-wal`` must still be frame-free
   by ``fstat`` on the held descriptor (the same inode can grow in place: identity is not
   content — ``WAL_FRAMES_PRESENT``); the store by name must be the held inode, every admitted
   sidecar by name its held inode; every live name of the store's namespace the gate did NOT
   admit (a ``-wal``/``-shm`` that appeared since, a ``-journal``) must be absent
   (``LIVE_NAME_OCCUPIED``); every archive name absent — all before any rename. Then the
   SENTINELS: 0500 directories this run makes and holds, ``mkdir``'d at every non-admitted live
   name and at every archive name (the mkdir is the atomic absence check: EEXIST is
   ``LIVE_NAME_OCCUPIED`` / ``TARGET_EXISTS``), all before any exchange, so a refusal moves
   nothing. Then ONE ``renameat2(RENAME_EXCHANGE)`` per admitted file, store first: in one
   syscall ``chronos.db.archived-<stamp>`` becomes the admitted inode and ``chronos.db`` becomes
   its sentinel — at no instant is a live name vacant, so nothing (a Chronos writer, which
   refuses a non-regular path and makes no file; a sqlite3 shell, which cannot open a
   directory; a rename, which cannot replace a directory with a file) can put a store the gate
   never saw at the live name — not during the command and not after it exits, until the
   owner's own ``rmdir`` (runbook step 7). That holds exactly as far as the PARENT is private
   (r4, Daybreak r3 P1-1): removing an empty directory is authorized by write permission on
   its parent, not by its own mode, so before the source is even opened — and again on the
   descriptor the exchanges use — the database parent must be owner-private: owned by the
   effective uid, no group or other write bit (``PARENT_NOT_PRIVATE`` otherwise, dry-run and
   apply alike; the documented deployment creates ``data/`` 0700). Each exchange is re-proved
   on BOTH names: the archive name must be the held inode and the live name this run's
   sentinel; otherwise (a foreign entry
   at the live name, or a foreign directory renamed over the sentinel, in the instant before the
   syscall) it is exchanged BACK — exact — and reported in ``foreign_renamed``; the admitted
   object was not moved, so its name is in ``not_moved``. ``<stamp>`` is the envelope's own
   (``chronos-<stamp>``), so the moved-aside files are bound by name to their archive. After the
   last exchange every lease must still be whole and the admitted ``-wal`` still frame-free, and
   then every live name must still BE its sentinel and every archive name its admitted inode —
   the proof, in the report's ``exclusion``, that the admitted set was quiet through the
   irreversible boundary and the namespace sealed after it. A failure after the first exchange
   is ``MOVE_INCOMPLETE`` with ``moved_aside`` and ``not_moved`` exact; the runbook's rollback
   removes the sentinels (the owner's rmdir) and renames back by name. The sentinels a run made
   are always listed (``sentinels``, with where each is now): the command removes none.
5. **A JSON report**: envelope, manifest path and sha256, cleartext and ciphertext sha256,
   the moved-aside names, the sentinels, and the next MANUAL steps (the runbook's) — never a
   fingerprint, a position id or a row payload; counts and the run's own id, trigger and times
   only.

What this command never does: it never generates, reads or prints the persistence key (the
module does not touch the environment or ``Settings`` — the drill reads its own age-key
variables); never starts the service; never initializes the fresh database; never opens the
old store for writing; deletes nothing of the owner's — the sentinel directories it leaves at
the four live names included: the owner's ``rmdir`` vacates them — and only ever its own
cleartext verify copy under ``--restore-into`` (the drill's N3-4 removal, reported as
``drill.restore_copy_removed``). Runbook: ``docs/ops/N3-CUTOVER.md``.

Trust root (r5, stated for review): (a) the adversary does not hold the store owner's uid or
root; (b) code running in the same Python process is trusted — it could replace ``flatten_gate``
itself — and the drill's guarantee against it is only that no verifier object outlives or escapes
``run_archive_drill``; (c) the database path's ancestor chain is the protected trust root: every
directory from ``/`` down to ``data/`` is a real directory owned by root or the effective uid
with no group or other write bit (a ``/tmp``-style sticky world-writable directory is outside
it), proved before the source is opened, held open through the last report, and re-proved on the
move path and at report time (``PATH_NOT_PROTECTED``, ``PATH_DISPLACED``) — the raw path's own
components, never normalised: a ``..`` is refused outright (a ``..`` after a symlink resolves
through the link in the kernel; r6), and the same held chain opens the source and runs the
move. Within it, the
sentinels are the owner's uid's alone to remove.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import sqlite3
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from chronos.domain.enums import ReconciliationStatus
from chronos.operations.restore_drill import (
    _DIR_FLAGS,
    _RENAME_EXCHANGE,
    ARCHIVE_VERIFIED,
    MANIFEST_NAME,
    RESTORE_COPY_NOT_REMOVED,
    AgeKeys,
    BoundSource,
    Clock,
    DrillRefused,
    SourceBusy,
    SourceSwapped,
    _entry_is_real_directory,
    _fresh_directory,
    _lease_intact,
    _lease_read,
    _open_regular,
    _renameat2,
    _schema_head_on,
    _sha256_of,
    load_age_keys,
    open_bound_source,
    run_archive_drill,
    system_clock,
)
from chronos.persistence.database import SCHEMA_VERSION
from chronos.persistence.schema import (
    DatabaseScopeRow,
    ManagedPositionBindingRow,
    PositionProvenanceRow,
    ReconciliationRunRow,
    SchemaVersionRow,
    WriterLeaseRow,
)
from chronos.supervisor.position_management import PositionManagementError, rehydrate_position

RUNBOOK = "docs/ops/N3-CUTOVER.md"
#: Twice the longest periodic reconciliation cadence (settings: 1800 s with the market closed),
#: so a backend that was running reconciled inside it. A constant, deliberately not a flag.
MAX_RECONCILIATION_AGE = timedelta(hours=1)
#: A write-ahead log holds a 32-byte header; anything beyond it is frames (uncheckpointed writes).
WAL_HEADER_BYTES = 32
ARCHIVED_INFIX = ".archived-"
SIDECAR_SUFFIXES = ("-wal", "-shm")
#: the store's whole live namespace beside the main file — the names the repository's R-21
#: admission checks (``database._SQLITE_SIDECAR_SUFFIXES``, pinned equal by the tests); after
#: ``--apply`` each holds a sentinel directory until the owner's ``rmdir`` (r3, Daybreak P1-1)
LIVE_NAME_SUFFIXES = ("-wal", "-shm", "-journal")
#: a sentinel is a directory the owner can list and remove but nobody can create inside of
SENTINEL_MODE = 0o500
ENVELOPE_PREFIX = "chronos-"
CLEAN = ReconciliationStatus.RECONCILED.value
ORIGIN_CLASSES = ("MANAGED", "WHEEL", "MANUAL", "FOREIGN")


class CutoverRefused(RuntimeError):
    """A typed refusal: ``code`` names the check, the message is the whole diagnosis.
    Nothing was written unless the message says what exists."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        #: the sentinel directories the move made before refusing, and where each is now (r3):
        #: the command deletes nothing, a sentinel included
        self.sentinels: tuple[dict[str, str | None], ...] = ()


class MoveIncomplete(CutoverRefused):
    """A rename failed AFTER an earlier one succeeded: the report carries both lists."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        moved: tuple[dict[str, str], ...],
        not_moved: tuple[str, ...],
        foreign: tuple[dict[str, str | None], ...] = (),
    ) -> None:
        super().__init__(code, message)
        self.moved = moved
        self.not_moved = not_moved
        self.foreign = foreign


# ----------------------------------------------------------------- the flatten gate


@dataclass(frozen=True, slots=True)
class GateReport:
    """What the store proved, counts only — no fingerprint, position id or payload."""

    schema_version: int
    schema_head: str
    source_identity: tuple[int, int]
    writer_lease: dict[str, object]
    wal: dict[str, object]
    bindings: dict[str, int]
    reconciliation: dict[str, object]
    positions_observed: int
    checks_passed: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "schema_head": self.schema_head,
            "writer_lease": dict(self.writer_lease),
            "wal": dict(self.wal),
            "bindings": dict(self.bindings),
            "reconciliation": dict(self.reconciliation),
            "positions_observed": self.positions_observed,
            "checks_passed": list(self.checks_passed),
        }


def _read_only_engine(connection: sqlite3.Connection) -> Engine:
    """A plain engine over the ONE bound connection (a static pool returns that connection
    to every checkout): no WAL pragma, no foreign-key pragma, no ``Database`` construction,
    nothing written, and never a second pathname open. The caller disposes it with
    ``close=False`` — the connection belongs to the capability."""

    return create_engine("sqlite://", creator=lambda: connection, poolclass=StaticPool)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _sidecar_facts(db_path: Path) -> dict[str, object]:
    """The ``-wal``/``-shm`` siblings by lstat (the precheck, before any row is read): each
    must be absent or a regular file; frames beyond the WAL header are a refusal (the last
    writer did not checkpoint and close). Admission — holding them — comes after the gate."""

    facts: dict[str, object] = {"present": False, "size": 0}
    for suffix in LIVE_NAME_SUFFIXES:
        sibling = db_path.with_name(db_path.name + suffix)
        try:
            st = os.lstat(sibling)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(st.st_mode):
            raise CutoverRefused(
                "SOURCE_NOT_REGULAR",
                f"{sibling} is not a regular file (a symlink or a directory beside the store); "
                "the command never follows a link",
            )
        if suffix == "-journal":
            raise CutoverRefused(
                "LIVE_NAME_OCCUPIED",
                f"{sibling} exists beside the store: a rollback journal is not part of a WAL-mode "
                "store (a hot one means an interrupted writer), and the move needs that name for "
                "its sentinel. Stop every Chronos process, inspect it by hand, move it out of the "
                "way yourself and re-run; the command never removes anything",
            )
        if suffix == "-wal":
            facts = {"present": True, "size": int(st.st_size)}
            if st.st_size > WAL_HEADER_BYTES:
                raise CutoverRefused(
                    "WAL_FRAMES_PRESENT",
                    f"{sibling} holds {st.st_size} bytes of write-ahead log (frames beyond the "
                    f"{WAL_HEADER_BYTES}-byte header): the last writer did not checkpoint and "
                    "close, or a process still has the store open. Stop every Chronos process "
                    f"({RUNBOOK} step 1), let the last writer's close checkpoint the WAL, and "
                    "re-run; the command never checkpoints or writes the store",
                )
    return facts


def _schema_version_of(session: Session) -> int:
    version = session.scalar(
        select(SchemaVersionRow.version).order_by(SchemaVersionRow.id.desc()).limit(1)
    )
    if version is None:
        raise CutoverRefused(
            "SCHEMA_UNREADABLE", "the store's schema_version table carries no version record"
        )
    return int(version)


def _writer_lease_facts(session: Session, now: datetime) -> dict[str, object]:
    row = session.get(WriterLeaseRow, 1)
    if row is None:
        return {"present": False, "live": False, "holder": None, "expires_at": None}
    try:
        expires = _as_utc(datetime.fromisoformat(str(row.expires_at)))
    except ValueError:
        raise CutoverRefused(
            "WRITER_LEASE_UNREADABLE",
            f"the writer lease row carries an unreadable expires_at ({row.expires_at!r})",
        ) from None
    live = expires > now
    if live:
        raise CutoverRefused(
            "WRITER_LEASE_LIVE",
            f"the writer lease is held by {row.holder} until {expires.isoformat()} (now "
            f"{now.isoformat()}): the backend renews it while it runs and deletes it on a clean "
            f"stop. Stop every Chronos process ({RUNBOOK} step 1) and re-run",
        )
    return {
        "present": True,
        "live": False,
        "holder": str(row.holder),
        "expires_at": expires.isoformat(),
    }


def _binding_facts(session: Session) -> dict[str, int]:
    """Every binding replayed through the position-management ledger; open or unreadable refuses."""

    rows = session.scalars(
        select(ManagedPositionBindingRow).order_by(ManagedPositionBindingRow.id.asc())
    ).all()
    total = len(rows)
    open_count = 0
    for index, row in enumerate(rows, start=1):
        try:
            state = rehydrate_position(
                session,
                account_fingerprint=row.account_fingerprint,
                position_id=row.position_id,
            )
        except (PositionManagementError, ValueError, TypeError) as error:
            raise CutoverRefused(
                "BINDING_UNREADABLE",
                f"managed position binding {index} of {total} could not be replayed from its "
                f"position-management stream: {error}. The store cannot prove that position "
                "closed; the command never guesses",
            ) from None
        if not state.closed:
            open_count += 1
    if open_count:
        raise CutoverRefused(
            "OPEN_BINDING",
            f"{open_count} of {total} managed position binding(s) replay to an OPEN position "
            "(remaining quantity above zero). Flatten at the broker, let the backend reconcile "
            "and record the closing fills, then re-run; the command never flattens and has no "
            "override",
        )
    return {"total": total, "closed": total}


def _reconciliation_facts(session: Session, now: datetime) -> tuple[dict[str, object], int]:
    run = session.scalars(
        select(ReconciliationRunRow)
        .order_by(ReconciliationRunRow.started_at.desc(), ReconciliationRunRow.id.desc())
        .limit(1)
    ).first()
    if run is None:
        raise CutoverRefused(
            "NO_RECONCILIATION_RUN",
            "the store holds no reconciliation run, so it cannot prove what the broker held. "
            "Start the backend, let it reconcile, stop it, and re-run within the hour",
        )
    label = f"latest reconciliation run {run.id} ({run.trigger}, started {run.started_at})"
    if run.status != CLEAN:
        raise CutoverRefused(
            "RECONCILIATION_NOT_CLEAN", f"{label} has status {run.status}; {CLEAN} is required"
        )
    if run.completed_at is None:
        raise CutoverRefused("RECONCILIATION_NOT_CLEAN", f"{label} did not complete")
    snapshot: Any = run.broker_snapshot
    if not isinstance(snapshot, dict):
        raise CutoverRefused(
            "RECONCILIATION_UNREADABLE", f"{label} carries no readable broker snapshot"
        )
    readiness = snapshot.get("readiness")
    if not isinstance(readiness, dict) or "status" not in readiness:
        raise CutoverRefused(
            "RECONCILIATION_UNREADABLE", f"{label} recorded no readiness latch in its snapshot"
        )
    if readiness["status"] != CLEAN:
        raise CutoverRefused(
            "RECONCILIATION_NOT_CLEAN",
            f"{label} recorded readiness {readiness['status']!r}; {CLEAN} is required",
        )
    completed = _as_utc(run.completed_at)
    age = now - completed
    if age < timedelta(0):
        raise CutoverRefused(
            "RECONCILIATION_UNREADABLE",
            f"{label} completed at {completed.isoformat()}, after now ({now.isoformat()}); a "
            "clock is wrong and that is the finding",
        )
    if age > MAX_RECONCILIATION_AGE:
        raise CutoverRefused(
            "RECONCILIATION_STALE",
            f"{label} completed {age.total_seconds():.0f} s ago; the bound is "
            f"{MAX_RECONCILIATION_AGE.total_seconds():.0f} s (twice the longest periodic "
            "cadence). Start the backend, let it reconcile, stop it, and re-run within the hour",
        )
    positions = snapshot.get("positions")
    if not isinstance(positions, list):
        raise CutoverRefused(
            "RECONCILIATION_UNREADABLE", f"{label} carries no readable position list"
        )
    facts: dict[str, object] = {
        "run_id": str(run.id),
        "trigger": str(run.trigger),
        "status": str(run.status),
        "started_at": _as_utc(run.started_at).isoformat(),
        "completed_at": completed.isoformat(),
        "age_s": age.total_seconds(),
        "readiness_status": str(readiness["status"]),
    }
    if positions:
        _refuse_positions(session, run, len(positions))
    return facts, len(positions)


def _refuse_positions(session: Session, run: ReconciliationRunRow, count: int) -> None:
    classes = session.scalars(
        select(PositionProvenanceRow.origin_class).where(
            PositionProvenanceRow.observed_run_id == run.id
        )
    ).all()
    if not classes:
        raise CutoverRefused(
            "PROVENANCE_MISSING",
            f"latest reconciliation run {run.id} observed {count} position(s) but carries no "
            "position_provenance rows for them (the provenance hook wrote nothing), so their "
            "origin cannot be told from the store. The cutover needs an EMPTY broker position "
            "list: flatten at the broker, let the backend reconcile, and re-run",
        )
    counted = {origin: classes.count(origin) for origin in ORIGIN_CLASSES}
    unclassified = max(0, count - len(classes))
    breakdown = ", ".join(f"{origin} {counted[origin]}" for origin in ORIGIN_CLASSES)
    raise CutoverRefused(
        "POSITION_OBSERVED",
        f"latest reconciliation run {run.id} observed {count} position(s): {breakdown}, "
        f"unclassified {unclassified}. The ruling requires no open position at cutover: flatten "
        "at the broker, let the backend reconcile and record it, and re-run; the command never "
        "flattens",
    )


def flatten_gate(source: BoundSource, *, now: datetime) -> GateReport:
    """Every proof the OLD store must give before a single byte is written anywhere.

    The first refusal stops the gate (:class:`CutoverRefused`, ``code`` names it). Reads only,
    all through the one bound capability: the source is the held descriptor (regular,
    no-follow, its inode pinned), the sidecars by lstat, the head and every row through the
    bound ``mode=ro`` connection — never a second pathname open. No broker, no override, no
    write.
    """

    db_path = source.path
    identity = source.identity
    passed: list[str] = ["source_regular"]
    wal = _sidecar_facts(db_path)
    passed.append("wal_checkpointed")
    try:
        head = _schema_head_on(source.connection)
    except sqlite3.Error as error:
        raise CutoverRefused("SCHEMA_UNREADABLE", f"{type(error).__name__}: {error}") from None
    engine = _read_only_engine(source.connection)
    try:
        with Session(engine) as session:
            try:
                version = _schema_version_of(session)
                if version >= SCHEMA_VERSION:
                    raise CutoverRefused(
                        "SCHEMA_ALREADY_CURRENT",
                        f"the store is at schema version {version} and the running "
                        f"SCHEMA_VERSION is {SCHEMA_VERSION}: this is not a pre-cutover store "
                        "(a fresh keyed store is never archived by this command)",
                    )
                passed.append("schema_below_running")
                if session.get(DatabaseScopeRow, 1) is None:
                    raise CutoverRefused(
                        "NO_SCOPE_ROW",
                        "the store carries no database_scope row: it was never bound, holds no "
                        "account-scoped history and has nothing to archive (the migration "
                        "upgrades an unbound store in place)",
                    )
                passed.append("scope_bound")
                lease = _writer_lease_facts(session, now)
                passed.append("writer_lease_not_live")
                bindings = _binding_facts(session)
                passed.append("bindings_closed")
                reconciliation, observed = _reconciliation_facts(session, now)
                passed.append("reconciliation_clean_fresh_empty")
            except (SQLAlchemyError, sqlite3.Error) as error:
                raise CutoverRefused(
                    "STORE_UNREADABLE", f"{type(error).__name__}: {error}"
                ) from None
    finally:
        engine.dispose(close=False)  # the connection is the capability's, not the engine's
    return GateReport(
        schema_version=version,
        schema_head=head,
        source_identity=identity,
        writer_lease=lease,
        wal=wal,
        bindings=bindings,
        reconciliation=reconciliation,
        positions_observed=observed,
        checks_passed=tuple(passed),
    )


# ----------------------------------------------------------------- move aside


def _entry_exists(dfd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _archived_name(name: str, stamp: str) -> str:
    return f"{name}{ARCHIVED_INFIX}{stamp}"


@dataclass(frozen=True, slots=True)
class AdmittedSidecar:
    """A ``-wal``/``-shm`` present at the end of the gate: opened no-follow, regular, HELD."""

    name: str
    fd: int
    identity: tuple[int, int]


def _admit_sidecars(source: BoundSource) -> tuple[AdmittedSidecar, ...]:
    """The stable set the move may rename, fixed at the end of the gate — after the bound
    connection's own reader bookkeeping exists, so it is in the set: each present sidecar
    opened no-follow (a symlink or a directory refuses), its identity pinned by the held
    descriptor, the ``-wal`` proven frame-free by ``fstat``."""

    admitted: list[AdmittedSidecar] = []
    try:
        for suffix in SIDECAR_SUFFIXES:
            sibling = source.path.with_name(source.path.name + suffix)
            try:
                os.lstat(sibling)
            except FileNotFoundError:
                continue
            try:
                fd = _open_regular(sibling, "sidecar")
            except DrillRefused as error:
                raise CutoverRefused("SOURCE_NOT_REGULAR", str(error)) from None
            st = os.fstat(fd)
            admitted.append(AdmittedSidecar(sibling.name, fd, (st.st_dev, st.st_ino)))
            if suffix == "-wal" and st.st_size > WAL_HEADER_BYTES:
                raise CutoverRefused(
                    "WAL_FRAMES_PRESENT",
                    f"{sibling} holds {st.st_size} bytes of write-ahead log at admission: a "
                    "writer is active; nothing moves",
                )
    except BaseException:
        for sidecar in admitted:
            os.close(sidecar.fd)
        raise
    return tuple(admitted)


def _identity_by_name(dfd: int, name: str) -> tuple[int, int] | None:
    """``(device, inode)`` of a regular file at ``name`` under the directory fd, by lstat;
    ``None`` when absent or not a regular file."""

    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return st.st_dev, st.st_ino


def _parent_problem(st: os.stat_result, euid: int) -> str | None:
    """Why the database parent is NOT an owner-private directory, or ``None`` (r4, Daybreak r3
    P1-1). Removing an entry — a sentinel — is authorized by WRITE and SEARCH permission on its
    PARENT, never by the entry's own mode, so the sentinels are exactly as private as this
    directory: it must be owned by the effective uid and carry no group or other write bit. The
    sticky bit is not consulted (it would only narrow a writable parent, and a writable parent
    is refused outright)."""

    if not stat.S_ISDIR(st.st_mode):
        return "is not a directory"
    mode = stat.S_IMODE(st.st_mode)
    if st.st_uid != euid:
        return (
            f"is owned by uid {st.st_uid}, not this process's effective uid {euid} (mode "
            f"{mode:04o})"
        )
    if mode & 0o022:
        return (
            f"is writable by its group or by others (mode {mode:04o}): removing an empty "
            "directory is authorized by write permission on its PARENT, so another uid could "
            "remove a sentinel there and plant a store the gate never saw"
        )
    return None


def _private_parent_facts(st: os.stat_result, parent_abs: Path, when: str) -> dict[str, object]:
    """``PARENT_NOT_PRIVATE`` unless the directory is owner-private; the facts for the report."""

    problem = _parent_problem(st, os.geteuid())
    if problem is not None:
        raise CutoverRefused(
            "PARENT_NOT_PRIVATE",
            f"{parent_abs} {problem}; nothing {when}. As the owner, make it owner-private — "
            f"chmod 0700 {parent_abs} — ({RUNBOOK}, Before you start) and re-run",
        )
    return {
        "path": str(parent_abs),
        "mode": f"{stat.S_IMODE(st.st_mode):04o}",
        "owner_uid": st.st_uid,
        "owner_is_effective_uid": True,
    }


#: an ancestor is held O_PATH|O_NOFOLLOW|O_DIRECTORY: no read permission on it is needed, a
#: symlink component is refused, and the *at calls can re-prove the chain through it (r5)
_PATH_FLAGS = os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC


def _ancestor_problem(st: os.stat_result, euid: int) -> str | None:
    """Why a directory on the way to the store is NOT part of the protected trust root (r5,
    Daybreak r4 P1-1), or ``None``: it must be a directory owned by root or the effective uid
    with no group or other write bit — a uid that can write an ancestor can rename the checked
    ``data/`` away and plant a replacement under the same lexical path. A sticky, group/other-
    writable directory (``/tmp``-style) is refused too: the sticky bit lets only an entry's
    owner rename or remove it, but other uids can still create entries there, and a store under
    such a directory is outside the supported deployment."""

    if not stat.S_ISDIR(st.st_mode):
        return "is not a directory"
    mode = stat.S_IMODE(st.st_mode)
    if st.st_uid not in (0, euid):
        return (
            f"is owned by uid {st.st_uid}, neither root nor this process's effective uid {euid} "
            f"(mode {mode:04o})"
        )
    if mode & 0o022:
        if mode & 0o1000:
            return (
                f"is a sticky, group/other-writable directory (mode {mode:04o}, /tmp-style): "
                "other uids can create entries in it, and although the sticky bit lets only an "
                "entry's owner rename or remove it, a store under such a directory is outside "
                "the supported deployment"
            )
        return (
            f"is writable by its group or by others (mode {mode:04o}): another uid could rename "
            "the checked directory below it away and plant a replacement at the same path"
        )
    return None


class ProtectedPath:
    """The database parent's whole no-follow ancestor chain, from ``/`` down to ``data/`` (r5,
    Daybreak r4 P1-1): every directory proved part of the protected trust root
    (:func:`_ancestor_problem`; the leaf also :func:`_parent_problem` — owned by the effective
    uid), each HELD open through the last report, so that the move path and the report can
    re-prove by identity that every path component still names the directory that was checked
    — a ``data/`` renamed away and replaced under the same lexical path is ``PATH_DISPLACED``,
    never ``ARCHIVED``. The chain is walked by name without following links — the RAW
    components, never normalised (r6: a ``..`` component is refused outright, since the kernel
    resolves ``link/..`` through the link) — and the same held chain opens the source and runs
    the move; a symlink component is ``PATH_NOT_PROTECTED``."""

    __slots__ = ("abs_path", "components", "facts", "fds", "identities")

    def __init__(self, parent: Path, when: str) -> None:
        absolute = parent if parent.is_absolute() else Path.cwd() / parent
        # r6 (Daybreak r5 P1-1): NO lexical normalisation — POSIX resolves ``link/..`` THROUGH
        # the link, so collapsing ``..`` before the walk would prove a chain the kernel never
        # uses. A ``..`` component is refused outright; the raw components are walked
        # descriptor-relatively without following links, and the same held chain opens the
        # source and runs the move.
        if any(component == ".." for component in absolute.parts):
            raise CutoverRefused(
                "PATH_NOT_PROTECTED",
                f"{absolute} carries a '..' component; give the store's real path (no '..', no "
                "symlink in it): a '..' after a symlink is resolved through the link by the "
                f"kernel, which no lexical walk can follow; nothing {when} ({RUNBOOK}, Before you "
                "start)",
            )
        self.abs_path = absolute
        self.components: list[str] = ["/"]
        self.fds: list[int] = []
        self.identities: list[tuple[int, int]] = []
        self.facts: list[dict[str, object]] = []
        euid = os.geteuid()
        try:
            self._admit(os.open("/", _PATH_FLAGS), Path("/"), euid, when, leaf=False)
            parts = self.abs_path.parts[1:]
            walked = Path("/")
            for index, component in enumerate(parts):
                walked = walked / component
                try:
                    fd = os.open(component, _PATH_FLAGS, dir_fd=self.fds[-1])
                except OSError as error:
                    kind = (
                        "is a symlink or not a directory (the chain is walked without "
                        "following links)"
                        if error.errno in (errno.ELOOP, errno.ENOTDIR)
                        else f"could not be opened: [errno {error.errno}] {error.strerror}"
                    )
                    raise CutoverRefused(
                        "PATH_NOT_PROTECTED",
                        f"{walked} {kind}; nothing {when}. Every directory from / down to "
                        f"{self.abs_path} must be a real directory owned by root or by you with "
                        f"no group or other write bit — check each with ls -ld ({RUNBOOK}, "
                        "Before you start)",
                    ) from None
                self.components.append(component)
                self._admit(fd, walked, euid, when, leaf=index == len(parts) - 1)
        except BaseException:
            self.close()
            raise

    def _admit(self, fd: int, walked: Path, euid: int, when: str, *, leaf: bool) -> None:
        try:
            st = os.fstat(fd)
            if leaf:
                _private_parent_facts(st, walked, when)  # PARENT_NOT_PRIVATE: owned by this uid
            problem = _ancestor_problem(st, euid)
            if problem is not None:
                raise CutoverRefused(
                    "PATH_NOT_PROTECTED",
                    f"{walked} {problem}; nothing {when}. Every directory from / down to "
                    f"{self.abs_path} must be a real directory owned by root or by you with no "
                    f"group or other write bit — check each with ls -ld ({RUNBOOK}, Before you "
                    "start)",
                )
        except BaseException:
            os.close(fd)
            raise
        self.fds.append(fd)
        self.identities.append((st.st_dev, st.st_ino))
        self.facts.append(
            {"path": str(walked), "owner_uid": st.st_uid, "mode": f"{stat.S_IMODE(st.st_mode):04o}"}
        )

    @property
    def leaf_fd(self) -> int:
        """The held ``O_PATH`` descriptor of ``data/`` itself: the source is opened relative to it
        and the move works through it (r6) — the same chain for check, open, move and report."""

        return self.fds[-1]

    @property
    def leaf_identity(self) -> tuple[int, int]:
        return self.identities[-1]

    @property
    def leaf_facts(self) -> dict[str, object]:
        """The r4 parent facts, as the report has carried them since r4."""

        return _private_parent_facts(os.fstat(self.fds[-1]), self.abs_path, "reported")

    def problem(self, when: str) -> tuple[str, str] | None:
        """Re-prove the chain on the held descriptors: every directory still protected (by
        ``fstat``), and every component name, resolved without following links under the held
        directory above it, still IS the held directory — ``PATH_DISPLACED`` otherwise."""

        euid = os.geteuid()
        for index, fd in enumerate(self.fds):
            st = os.fstat(fd)
            walked = Path(str(self.facts[index]["path"]))
            problem = _ancestor_problem(st, euid)
            if problem is None and index == len(self.fds) - 1:
                problem = _parent_problem(st, euid)
            if problem is not None:
                return ("PATH_NOT_PROTECTED", f"{walked} {problem} (changed after the check)")
            if index == 0:
                continue
            found: tuple[int, int] | None
            try:
                by_name = os.stat(
                    self.components[index], dir_fd=self.fds[index - 1], follow_symlinks=False
                )
                found = (by_name.st_dev, by_name.st_ino) if stat.S_ISDIR(by_name.st_mode) else None
            except FileNotFoundError:
                found = None
            if found != self.identities[index]:
                held = self.identities[index]
                return (
                    "PATH_DISPLACED",
                    f"{walked} no longer names the directory that was checked (device:inode "
                    f"{held[0]}:{held[1]}, still held open by this process): it was renamed away "
                    "or replaced after the check, so whatever is at that path now is not what "
                    "the command inspected — find the checked directory by inode (find "
                    f"{walked.parent} -inum {held[1]}) before anything starts",
                )
        return None

    def verify(self, when: str) -> None:
        problem = self.problem(when)
        if problem is not None:
            raise CutoverRefused(problem[0], f"{problem[1]}; nothing {when}")

    def report(self, *, linked: bool) -> dict[str, object]:
        return {
            "path": str(self.abs_path),
            "chain": [dict(fact) for fact in self.facts],
            "held_through_report": True,
            "linked_at_report": linked,
        }

    def close(self) -> None:
        for fd in self.fds:
            with contextlib.suppress(OSError):
                os.close(fd)
        self.fds.clear()


def _require_protected_absent_target(restore_into: Path) -> None:
    """FIX-RT (Daybreak §7 (ii), LINE STOP #6): the cutover's ``--restore-into`` must sit under a
    protected ancestor chain — every directory from ``/`` to its parent a real directory owned
    by root or the effective uid with no group or other write bit, the parent owned by the
    effective uid (the same :class:`ProtectedPath` walk the store's own path gets; ``/tmp`` and
    anything under it is refused) — and the leaf must be ABSENT: the command creates it 0700
    itself and the drill verifies uid and mode. Either failure stops the run before any
    envelope exists. The chain is closed after the check: only root or this uid can change a
    protected chain, so the drill's own no-follow walk meets the same directories."""

    absolute = restore_into if restore_into.is_absolute() else Path.cwd() / restore_into
    try:
        chain = ProtectedPath(absolute.parent, "was backed up or restored")
    except CutoverRefused as error:
        raise CutoverRefused(
            "RESTORE_TARGET_NOT_PROTECTED",
            f"--restore-into {absolute}: {error}. The restore copy is the CLEARTEXT store, so its "
            "parent must be a directory only you control under a protected chain — a /tmp path "
            f"never is ({RUNBOOK}, step 2)",
        ) from None
    try:
        try:
            os.stat(absolute.name, dir_fd=chain.leaf_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise CutoverRefused(
            "RESTORE_TARGET_NOT_FRESH",
            f"--restore-into {absolute} already exists; it must not exist: the command creates the "
            "restore target itself (0700, this uid) so no one else can have prepared it — name a "
            f"new path and re-run; nothing was backed up or restored ({RUNBOOK}, step 2)",
        )
    finally:
        chain.close()


def _require_excluded(source: BoundSource, when: str) -> None:
    """The store's read lease must still be whole: a process that opened the store for writing
    since the lease was taken broke it, and nothing read or written after that is trusted."""

    if not source.writers_excluded:
        raise CutoverRefused(
            "WRITER_OPEN",
            f"a process opened {source.path} for writing {when} (the kernel read lease taken "
            f"before the gate is broken): the store is not quiet. Stop every Chronos process "
            f"({RUNBOOK} step 1) and re-run",
        )


def _wal_of(sidecars: tuple[AdmittedSidecar, ...]) -> AdmittedSidecar | None:
    return next((s for s in sidecars if s.name.endswith("-wal")), None)


def _not_quiet(
    source: BoundSource, sidecars: tuple[AdmittedSidecar, ...], parent_abs: Path
) -> tuple[str, str] | None:
    """``(code, message)`` when the admitted set is not provably quiet: a lease broken (some
    process opened the store or a sidecar for writing) or frames in the admitted ``-wal`` by
    ``fstat`` on the held descriptor — the same inode grown in place. ``None`` when every lease
    is whole and the WAL is within its header."""

    if not source.writers_excluded:
        return "WRITER_OPEN", f"a process opened {source.path} for writing after the gate"
    for sidecar in sidecars:
        if not _lease_intact(sidecar.fd):
            return "WRITER_OPEN", f"a process opened {parent_abs / sidecar.name} for writing"
    wal = _wal_of(sidecars)
    if wal is not None:
        size = os.fstat(wal.fd).st_size
        if size > WAL_HEADER_BYTES:
            return (
                "WAL_FRAMES_PRESENT",
                f"the admitted {parent_abs / wal.name} (the same inode the gate admitted) now "
                f"holds {size} bytes of write-ahead log: a writer appended frames after admission",
            )
    return None


@dataclass(frozen=True, slots=True)
class Sentinel:
    """A 0500 directory this run made and holds open (r3, Daybreak P1-1): from the exchange on it
    occupies a live name of the store's namespace, so that name is never vacant — nothing can
    create, rename or open a file there until the owner's own ``rmdir``."""

    name: str  #: the live name it is for
    made_at: str  #: where it was made: the archive name (admitted file) or the live name itself
    fd: int
    identity: tuple[int, int]


def _sentinel_at(dfd: int, parent_abs: Path, *, name: str, made_at: str, code: str) -> Sentinel:
    """``mkdir`` the sentinel at ``made_at`` under the held directory — the mkdir IS the atomic
    "nothing is there" check (EEXIST is the caller's typed refusal ``code``) — open it no-follow,
    make it 0500, and prove it is ours (a directory, this uid, empty); hold it."""

    try:
        os.mkdir(made_at, SENTINEL_MODE, dir_fd=dfd)
    except FileExistsError:
        raise CutoverRefused(
            code,
            f"{parent_abs / made_at} exists; the command never renames over an entry and never "
            "removes one. Nothing moved: the live store is in place and the envelope is published "
            "— move that entry out of the way by hand (it is not this run's) and re-run",
        ) from None
    fd = os.open(made_at, _DIR_FLAGS, dir_fd=dfd)
    try:
        os.fchmod(fd, SENTINEL_MODE)
        st = os.fstat(fd)
        if not (stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid() and not os.listdir(fd)):
            raise CutoverRefused(
                "LIVE_NAME_OCCUPIED",
                f"{parent_abs / made_at} is not the sentinel directory this run just made (it was "
                "replaced in the instant after mkdir); nothing moved — inspect by hand",
            )
    except BaseException:
        os.close(fd)
        raise
    return Sentinel(name, made_at, fd, (st.st_dev, st.st_ino))


def _identity_any(dfd: int, name: str) -> tuple[int, int] | None:
    """``(device, inode)`` of whatever entry is at ``name``, by lstat; ``None`` when absent."""

    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return st.st_dev, st.st_ino


def _describe_entry(dfd: int, name: str) -> str:
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return "absent"
    kind = (
        "a regular file"
        if stat.S_ISREG(st.st_mode)
        else "a directory"
        if stat.S_ISDIR(st.st_mode)
        else "a symlink"
        if stat.S_ISLNK(st.st_mode)
        else "another kind of entry"
    )
    return f"{kind} (device:inode {st.st_dev}:{st.st_ino})"


def _sentinels_report(
    dfd: int, parent_abs: Path, sentinels: list[Sentinel], order: list[str]
) -> tuple[dict[str, str | None], ...]:
    """Where each sentinel this run made is NOW, by identity: at its live name, still at the
    archive name it was made at, or at neither (``at: null`` — replaced or removed). In the
    namespace's order (store, -wal, -shm, -journal)."""

    report: list[dict[str, str | None]] = []
    for sentinel in sorted(sentinels, key=lambda s: order.index(s.name)):
        at: str | None = None
        for candidate in (sentinel.name, sentinel.made_at):
            if _entry_is_real_directory(dfd, candidate, sentinel.identity):
                at = str(parent_abs / candidate)
                break
        report.append(
            {
                "name": str(parent_abs / sentinel.name),
                "at": at,
                "identity": f"{sentinel.identity[0]}:{sentinel.identity[1]}",
            }
        )
    return tuple(report)


def _exchange_back(dfd: int, name: str, archived: str) -> bool:
    """Reverse an exchange whose post-proof failed: ONE more ``renameat2(RENAME_EXCHANGE)`` of
    the same two names — exact, never a rename over anything. False when it could not be done
    (one of the names vanished meanwhile)."""

    try:
        _renameat2(dfd, archived, dfd, name, _RENAME_EXCHANGE)
    except OSError:
        return False
    return True


def _live_names_problem(
    dfd: int,
    parent_abs: Path,
    sentinels: list[Sentinel],
    expected: dict[str, tuple[int, int]],
    targets: dict[str, str],
) -> tuple[str, str] | None:
    """The report-time proof (after the last exchange): every live name of the namespace IS this
    run's sentinel by identity, every archive name IS its admitted inode. ``(code, message)``
    otherwise; ``None`` when the namespace is sealed."""

    for sentinel in sentinels:
        if not _entry_is_real_directory(dfd, sentinel.name, sentinel.identity):
            return (
                "LIVE_NAME_OCCUPIED",
                f"{parent_abs / sentinel.name} is no longer this run's sentinel directory "
                f"(device:inode {sentinel.identity[0]}:{sentinel.identity[1]}) but "
                f"{_describe_entry(dfd, sentinel.name)}: the live name was vacated and refilled "
                "after the exchange — in an owner-private parent (proved before the move) only "
                "the owner's uid can remove a sentinel: removing an entry is authorized by write "
                "permission on its parent",
            )
    for name, identity in expected.items():
        if _identity_by_name(dfd, targets[name]) != identity:
            return (
                "SOURCE_SWAPPED",
                f"{parent_abs / targets[name]} is no longer the admitted object (device:inode "
                f"{identity[0]}:{identity[1]}) but {_describe_entry(dfd, targets[name])}: the "
                "archive name changed after the exchange",
            )
    return None


def _move_aside(
    source: BoundSource,
    sidecars: tuple[AdmittedSidecar, ...],
    manifest_identity: tuple[int, int] | None,
    stamp: str,
    protected: ProtectedPath,
) -> tuple[tuple[dict[str, str], ...], dict[str, object], tuple[dict[str, str | None], ...]]:
    """Exchange the store and its ADMITTED sidecars with sentinel directories at
    ``<name>.archived-<stamp>`` in their own directory, with the admitted set proven quiet
    through the boundary (r2) and the live namespace never vacant (r3, Daybreak P1-1).

    Before ANY rename: every admitted sidecar is leased (the bound connection held them
    read-write until it closed; a process still holding one is ``WRITER_OPEN``); the store's
    lease must be whole; the manifest's ``source_identity`` must be the held inode; the admitted
    ``-wal`` must be frame-free by ``fstat`` on its held descriptor; the store by name must be the
    held inode, every admitted sidecar by name its held inode; every live name of the namespace
    that is NOT admitted (``-wal``/``-shm`` that appeared after admission, a ``-journal``) must
    be absent (``LIVE_NAME_OCCUPIED``; with frames in a ``-wal``, ``WAL_FRAMES_PRESENT``); every
    archive name is proved absent. Then the sentinels — 0500 directories this run makes and
    holds — are placed: at every non-admitted live name (``mkdir``, atomic: EEXIST is
    ``LIVE_NAME_OCCUPIED``) and at every archive name (EEXIST is ``TARGET_EXISTS``), all before any
    exchange, so a refusal here moves nothing. Then ONE ``renameat2(RENAME_EXCHANGE)`` per
    admitted name, store first: in one syscall the archive name becomes the admitted inode and the
    live name becomes its sentinel — there is no instant at which a live name is vacant. Each
    exchange is re-proved by identity on BOTH names; a mismatch (a foreign entry put at the live
    name, or a foreign directory renamed over the sentinel, in the instant before the syscall) is
    reversed by exchanging back — exact — and reported in ``foreign_renamed``; the admitted name
    counts as not moved. After the last exchange the leases and the WAL are re-proved
    (``exclusion``), then the namespace: every live name IS its sentinel, every archive name IS
    its admitted inode. Nothing is ever deleted or renamed over — a sentinel included: the ones
    this run made are returned with where each is now, and only the owner's ``rmdir`` (the
    runbook's step 7) vacates the names — because the parent is owner-private, which the move
    requires (``PARENT_NOT_PRIVATE`` otherwise, re-proved here on the descriptor the exchanges
    use; r4): removing an entry is authorized by write permission on its parent, never by the
    entry's own 0500.
    """

    if manifest_identity != source.identity:
        recorded = (
            "none"
            if manifest_identity is None
            else f"{manifest_identity[0]}:{manifest_identity[1]}"
        )
        raise CutoverRefused(
            "SOURCE_SWAPPED",
            f"the envelope's manifest records source identity {recorded} (device:inode), not the "
            f"gated store's {source.identity[0]}:{source.identity[1]}: the archive is not of this "
            "store; nothing moved — inspect by hand",
        )
    # r6: the move works through the held chain's leaf — opened for reading via "." relative to
    # the held O_PATH descriptor, never re-walked by name, so no late, untyped refusal can arise
    dfd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=protected.leaf_fd)
    parent_abs = protected.abs_path
    namespace = [source.path.name, *(source.path.name + suffix for suffix in LIVE_NAME_SUFFIXES)]
    sentinels: list[Sentinel] = []
    try:
        # r4: re-proved on the very descriptor the exchanges will use — owner-private, or nothing
        parent = _private_parent_facts(os.fstat(dfd), parent_abs, "moved")
        # r5: that descriptor must BE the directory the held chain checked, and every component of
        # the chain must still name its held directory — a displaced data/ moves nothing
        walked = os.fstat(dfd)
        if (walked.st_dev, walked.st_ino) != protected.leaf_identity:
            held = protected.leaf_identity
            raise CutoverRefused(
                "PATH_DISPLACED",
                f"{parent_abs} is not the directory checked before the source was opened "
                f"(device:inode {held[0]}:{held[1]}, still held open by this process) but "
                f"{walked.st_dev}:{walked.st_ino}: the path was displaced; nothing moved — find "
                f"the checked directory by inode (find {parent_abs.parent} -inum {held[1]})",
            )
        protected.verify("moved")
        for sidecar in sidecars:
            try:
                _lease_read(sidecar.fd, f"sidecar {parent_abs / sidecar.name}")
            except SourceBusy as error:
                raise CutoverRefused(
                    "WRITER_OPEN",
                    f"{error} — every SQLite connection, a read-only one included, holds the "
                    f"sidecars read-write; nothing moved. Stop every Chronos process ({RUNBOOK} "
                    "step 1) and re-run",
                ) from None
            except DrillRefused as error:
                raise CutoverRefused(
                    "WRITER_EXCLUSION_UNAVAILABLE", f"{error}; nothing moved"
                ) from None
        problem = _not_quiet(source, sidecars, parent_abs)
        if problem is not None:
            raise CutoverRefused(
                problem[0],
                f"{problem[1]}; nothing moved — stop every Chronos process ({RUNBOOK} step 1), "
                "inspect by hand, and re-run",
            )
        expected: dict[str, tuple[int, int]] = {source.path.name: source.identity}
        for sidecar in sidecars:
            expected[sidecar.name] = sidecar.identity
        for name, identity in expected.items():
            if _identity_by_name(dfd, name) != identity:
                raise CutoverRefused(
                    "SOURCE_SWAPPED",
                    f"{parent_abs / name} is no longer the object the gate admitted (identity "
                    "changed between the gate and the move); nothing moved — inspect by hand",
                )
        vacant = [name for name in namespace if name not in expected]
        for name in vacant:
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if name.endswith("-wal") and stat.S_ISREG(st.st_mode) and st.st_size > WAL_HEADER_BYTES:
                raise CutoverRefused(
                    "WAL_FRAMES_PRESENT",
                    f"{parent_abs / name} appeared after the gate admitted the sidecars and holds "
                    f"{st.st_size} bytes of write-ahead log: a writer is active or a foreign file "
                    "was planted; nothing moved — stop every Chronos process and inspect by hand",
                )
            raise CutoverRefused(
                "LIVE_NAME_OCCUPIED",
                f"{parent_abs / name} exists but is not in the admitted set (it appeared after the "
                "gate admitted the sidecars, or it is a rollback journal), so its live name cannot "
                "take a sentinel; nothing moved — stop every Chronos process, find out what wrote "
                "it, move it out of the way by hand (it is not this run's) and re-run",
            )
        names = list(expected)
        targets = {name: _archived_name(name, stamp) for name in names}
        for target in targets.values():
            if _entry_exists(dfd, target):
                raise CutoverRefused(
                    "TARGET_EXISTS",
                    f"{parent_abs / target} already exists; the command never renames over an "
                    "entry. Nothing moved: the live store is in place and the envelope is "
                    "published — move that entry out of the way by hand (it is not this run's) "
                    "and re-run",
                )
        # the sentinels, all before any exchange: a refusal here moves nothing
        for name in vacant:
            sentinels.append(
                _sentinel_at(dfd, parent_abs, name=name, made_at=name, code="LIVE_NAME_OCCUPIED")
            )
        by_name: dict[str, Sentinel] = {}
        for name in names:
            by_name[name] = _sentinel_at(
                dfd, parent_abs, name=name, made_at=targets[name], code="TARGET_EXISTS"
            )
            sentinels.append(by_name[name])
        moved: list[dict[str, str]] = []
        for name in names:
            sentinel = by_name[name]
            try:
                _renameat2(dfd, name, dfd, targets[name], _RENAME_EXCHANGE)
            except OSError as error:
                detail = (
                    f"exchange of {parent_abs / name} with {parent_abs / targets[name]} failed: "
                    f"[errno {error.errno}] {error.strerror}"
                )
                if not moved:
                    raise CutoverRefused("MOVE_FAILED", f"{detail}; nothing moved") from None
                not_moved = tuple(str(parent_abs / other) for other in names[len(moved) :])
                raise MoveIncomplete(
                    "MOVE_FAILED",
                    f"{detail}; {len(moved)} file(s) already moved — rmdir the sentinels, then "
                    f"rename them back by name ({RUNBOOK}, Rollback) before anything starts",
                    moved=tuple(moved),
                    not_moved=not_moved,
                ) from None
            found = _identity_by_name(dfd, targets[name])
            if found != expected[name]:
                # a foreign entry sat at the live name in the instant of the syscall: it is now
                # at the archive name and the sentinel at the live name — exchange them back
                restored = _exchange_back(dfd, name, targets[name]) and (
                    _identity_by_name(dfd, name) == found
                )
                foreign: dict[str, str | None] = {
                    "renamed_to": str(parent_abs / targets[name]),
                    "restored_to": str(parent_abs / name) if restored else None,
                    "identity": "absent" if found is None else f"{found[0]}:{found[1]}",
                }
                where = (
                    f"exchanged back to {foreign['restored_to']}"
                    if restored
                    else f"left at {foreign['renamed_to']} (it could not be exchanged back)"
                )
                not_moved = tuple(str(parent_abs / other) for other in names[len(moved) :])
                raise MoveIncomplete(
                    "SOURCE_SWAPPED",
                    f"the entry at {parent_abs / name} in the instant of its exchange was not the "
                    f"object the gate admitted (device:inode {expected[name][0]}:"
                    f"{expected[name][1]}) but a foreign one ({foreign['identity']}), now "
                    f"{where}; the admitted object was not moved and is not at its name — this "
                    f"process holds it open until it exits (find it by inode); "
                    f"{len(moved)} file(s) moved before it — inspect by hand before anything "
                    f"starts ({RUNBOOK}, Rollback)",
                    moved=tuple(moved),
                    not_moved=not_moved,
                    foreign=(foreign,),
                ) from None
            if not _entry_is_real_directory(dfd, name, sentinel.identity):
                # the admitted object did move, but what the exchange put at its live name is not
                # this run's sentinel: a foreign directory was renamed over the sentinel in the
                # instant before the syscall (the owner's uid can) — exchange them back
                intruder = _identity_any(dfd, name)
                restored = _exchange_back(dfd, name, targets[name]) and (
                    _identity_any(dfd, targets[name]) == intruder
                )
                foreign = {
                    "renamed_to": str(parent_abs / name),
                    "restored_to": str(parent_abs / targets[name]) if restored else None,
                    "identity": "absent" if intruder is None else f"{intruder[0]}:{intruder[1]}",
                }
                not_moved = tuple(str(parent_abs / other) for other in names[len(moved) :])
                outcome = (
                    f"the admitted object is back at its name and the foreign entry back at "
                    f"{parent_abs / targets[name]}"
                    if restored
                    else f"the admitted object is at {parent_abs / targets[name]} and the "
                    "foreign entry at its live name (they could not be exchanged back)"
                )
                raise MoveIncomplete(
                    "LIVE_NAME_OCCUPIED",
                    f"the entry exchanged into {parent_abs / name} was not this run's sentinel "
                    f"(device:inode {sentinel.identity[0]}:{sentinel.identity[1]}) but a foreign "
                    f"one ({foreign['identity']}) renamed over the sentinel in the instant before "
                    f"the syscall; the exchange was reversed: {outcome} (the admitted object is "
                    f"device:inode {expected[name][0]}:{expected[name][1]}); {len(moved)} file(s) "
                    f"moved before it — inspect by hand before anything starts ({RUNBOOK}, "
                    "Rollback)",
                    moved=tuple(moved),
                    not_moved=not_moved,
                    foreign=(foreign,),
                ) from None
            moved.append({"from": str(parent_abs / name), "to": str(parent_abs / targets[name])})
        os.fsync(dfd)
        problem = _not_quiet(source, sidecars, parent_abs)
        if problem is not None:
            raise MoveIncomplete(
                problem[0],
                f"{problem[1]} — every exchange succeeded, but the admitted set was not quiet "
                f"through the boundary: rmdir the sentinels, rename the moved files back by name "
                f"({RUNBOOK}, Rollback), stop every Chronos process, and investigate before "
                "anything starts",
                moved=tuple(moved),
                not_moved=(),
            )
        problem = _live_names_problem(dfd, parent_abs, sentinels, expected, targets)
        if problem is not None:
            raise MoveIncomplete(
                problem[0],
                f"{problem[1]} — every exchange succeeded and the admitted set is at the archive "
                f"names ({'moved_aside'}), but the namespace is not sealed; nothing was deleted: "
                f"investigate before anything starts ({RUNBOOK}, step 3)",
                moved=tuple(moved),
                not_moved=(),
            )
        problem = protected.problem("reported")
        if problem is not None:
            raise MoveIncomplete(
                problem[0],
                f"{problem[1]} — every exchange succeeded inside the held directory and the "
                "admitted set is at the archive names there (moved_aside), but the path no "
                "longer names it; nothing was deleted: investigate before anything starts "
                f"({RUNBOOK}, Before you start)",
                moved=tuple(moved),
                not_moved=(),
            )
        wal = _wal_of(sidecars)
        exclusion: dict[str, object] = {
            "mechanism": "kernel read lease (fcntl F_SETLEASE) on the held store and every "
            "admitted sidecar; sentinel directories exchanged into every live name",
            "leased": [source.path.name, *(sidecar.name for sidecar in sidecars)],
            "intact_after_move": True,
            "wal_bytes_after_move": None if wal is None else os.fstat(wal.fd).st_size,
            "live_names": [str(parent_abs / name) for name in namespace],
            "live_names_sealed": True,
            "sentinel_mode": f"{SENTINEL_MODE:04o}",
            "parent": parent,
            "protected_path": protected.report(linked=True),
        }
        report = _sentinels_report(dfd, parent_abs, sentinels, namespace)
    except CutoverRefused as error:
        error.sentinels = _sentinels_report(dfd, parent_abs, sentinels, namespace)
        raise
    finally:
        for sentinel in sentinels:
            os.close(sentinel.fd)
        os.close(dfd)
    return tuple(moved), exclusion, report


# ----------------------------------------------------------------- the command


@dataclass(frozen=True, slots=True)
class ArchiveReport:
    verdict: str  # DRY_RUN | ARCHIVED | REFUSED | ARCHIVE_NOT_VERIFIED | MOVE_INCOMPLETE
    dry_run: bool
    gate: GateReport | None = None
    code: str | None = None
    failures: tuple[str, ...] = ()
    planned: dict[str, object] | None = None
    envelope: str | None = None
    manifest_path: str | None = None
    manifest_sha256: str | None = None
    cleartext_sha256: str | None = None
    ciphertext_sha256: str | None = None
    schema_head: str | None = None
    restore_dir: str | None = None
    drill: dict[str, object] | None = None
    moved_aside: tuple[dict[str, str], ...] = ()
    not_moved: tuple[str, ...] = ()
    #: an entry that sat at an admitted name at the instant of its exchange and was NOT the
    #: admitted object — or a foreign directory that had replaced the sentinel: exchanged back
    #: (``restored_to``) or left where the exchange put it
    foreign_renamed: tuple[dict[str, str | None], ...] = ()
    #: the sentinel directories this run made, each with where it is now by identity (``at`` is
    #: the live name after ARCHIVED; the archive name after a reversal; ``null`` when replaced or
    #: removed) — the command never removes one; the owner's ``rmdir`` does (r3)
    sentinels: tuple[dict[str, str | None], ...] = ()
    #: ARCHIVED only: the writer-exclusion proof through the move and the live-name proof after it
    exclusion: dict[str, object] | None = None
    next_manual_steps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "dry_run": self.dry_run,
            "code": self.code,
            "failures": list(self.failures),
            "gate": None if self.gate is None else self.gate.to_dict(),
            "planned": self.planned,
            "envelope": self.envelope,
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "cleartext_sha256": self.cleartext_sha256,
            "ciphertext_sha256": self.ciphertext_sha256,
            "schema_head": self.schema_head,
            "restore_dir": self.restore_dir,
            "drill": self.drill,
            "moved_aside": [dict(entry) for entry in self.moved_aside],
            "not_moved": list(self.not_moved),
            "foreign_renamed": [dict(entry) for entry in self.foreign_renamed],
            "sentinels": [dict(entry) for entry in self.sentinels],
            "exclusion": None if self.exclusion is None else dict(self.exclusion),
            "next_manual_steps": list(self.next_manual_steps),
            "runbook": RUNBOOK,
        }


def _live_names_of(db_abs: Path) -> tuple[str, ...]:
    return (str(db_abs), *(str(db_abs.with_name(db_abs.name + s)) for s in LIVE_NAME_SUFFIXES))


def _next_manual_steps(
    restore_dir: str, schema_head_value: str, live_names: tuple[str, ...]
) -> tuple[str, ...]:
    return (
        f"Confirm the restore copy is gone: this run removed the CLEARTEXT copy it verified under "
        f"{restore_dir}, and that directory, before printing this report "
        "(drill.restore_copy_removed above is true; drill.removed names what went) — "
        f"`ls -ld {restore_dir}` must say no such file. Nothing of it is left for you to delete.",
        "Record the envelope path, manifest_sha256 and ciphertext_sha256 printed above in the "
        f"cutover log ({RUNBOOK} step 3).",
        "Generate the persistence key YOURSELF: python -c 'import secrets; "
        "print(secrets.token_hex(32))' — paste it into the host's 0600 .env as "
        "CHRONOS_PERSISTENCE_KEY. Nobody and nothing else generates, sees or handles it.",
        "Age-encrypt ONE escrow copy of that key off-host to your recovery key "
        f"({RUNBOOK} step 6).",
        "Vacate the live names YOURSELF, only when you are ready to start: rmdir "
        f"{' '.join(live_names)} — the four sentinel directories this run left; until your rmdir "
        "the backend, db-preflight and any sqlite3 shell refuse them (a directory is not a "
        f"database), so nothing but your hand can create a fresh store there ({RUNBOOK} step 7).",
        "Start the backend: a fresh database initializes at the running schema. Bind it. "
        "Re-author the mandate BEFORE the first start of autonomy (dated window = 0).",
        f"Retention: the envelope is a pre-cutover backup ({schema_head_value}); the pruner "
        "deletes it after 90 days. Keep the moved-aside files until you are satisfied, then "
        "the same 90 days.",
    )


def archive(
    db_path: Path,
    out_dir: Path,
    restore_into: Path,
    *,
    apply: bool,
    clock: Clock | None = None,
    keys: AgeKeys | None = None,
) -> ArchiveReport:
    """Gate → restore-target check → (dry-run stops here) → drill → VERIFIED required → move
    aside → report.

    Refusals BEFORE the drill raise :class:`CutoverRefused` (nothing exists); after the
    drill they are returned as reports that name the envelope (the drill's cleartext restore
    copy is removed, N3-4; one it could not remove is ``RESTORE_COPY_NOT_REMOVED``, nothing
    moved).
    """

    clock = clock or system_clock()
    now = clock.wall()
    db_path = Path(db_path)
    out_dir = Path(out_dir)
    restore_into = Path(restore_into)
    try:
        keys = keys or load_age_keys()  # every key refusal precedes the source open
    except DrillRefused as error:
        raise CutoverRefused("KEYS_REFUSED", str(error)) from None
    # r4/r5: the parent's privacy — and its whole ancestor chain's — is the sentinels' guarantee:
    # proved before the source is opened, every directory held open through the last report
    protected = ProtectedPath(db_path.parent, "was opened")
    try:
        source = open_bound_source(db_path, "source database", dir_fd=protected.leaf_fd)
    except SourceSwapped as error:
        protected.close()
        raise CutoverRefused("SOURCE_SWAPPED", str(error)) from None
    except DrillRefused as error:
        protected.close()
        raise CutoverRefused("SOURCE_NOT_REGULAR", str(error)) from None
    try:
        try:
            source.exclude_writers()  # from here, no process can open the store for writing
        except SourceBusy as error:
            raise CutoverRefused(
                "WRITER_OPEN",
                f"{error}; no fact was read. Stop every Chronos process ({RUNBOOK} step 1) — "
                "a backend that is stopped but still holds the store counts — and re-run",
            ) from None
        except DrillRefused as error:
            raise CutoverRefused("WRITER_EXCLUSION_UNAVAILABLE", str(error)) from None
        gate = flatten_gate(source, now=now)
        sidecars = _admit_sidecars(source)
        try:
            return _archive_admitted(
                source,
                sidecars,
                gate,
                out_dir,
                restore_into,
                apply=apply,
                now=now,
                clock=clock,
                keys=keys,
                protected=protected,
            )
        finally:
            for sidecar in sidecars:
                os.close(sidecar.fd)
    finally:
        source.close()
        protected.close()  # after the report: the chain was held through it


def _archive_admitted(
    source: BoundSource,
    sidecars: tuple[AdmittedSidecar, ...],
    gate: GateReport,
    out_dir: Path,
    restore_into: Path,
    *,
    apply: bool,
    now: datetime,
    clock: Clock,
    keys: AgeKeys,
    protected: ProtectedPath,
) -> ArchiveReport:
    db_path = source.path
    _require_excluded(source, "after the gate")
    # C1 F5: the restore target — a protected chain, the leaf absent (FIX-RT) — is proved HERE,
    # dry run and --apply alike, read-only (nothing is created until --apply's _fresh_directory
    # below), so a clean dry run also proves --restore-into and a /tmp target refuses at step 2
    _require_protected_absent_target(restore_into)
    if not apply:
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        out_abs = out_dir if out_dir.is_absolute() else Path.cwd() / out_dir
        db_abs = db_path if db_path.is_absolute() else Path.cwd() / db_path
        planned: dict[str, object] = {
            "envelope": str(out_abs / f"{ENVELOPE_PREFIX}{stamp}"),
            "moved_aside": [
                str(db_abs.with_name(_archived_name(db_abs.name + suffix, stamp)))
                for suffix in ("", *SIDECAR_SUFFIXES)
            ],
            "sentinels": list(_live_names_of(db_abs)),
            "parent": protected.leaf_facts,
            "protected_path": protected.report(linked=protected.problem("planned") is None),
            "note": "the stamp is the envelope's; --apply takes it from the backup's taken_at, "
            "moves a sidecar only when it is present, and leaves a sentinel directory at every "
            "live name until your rmdir — in an owner-private parent, proved before anything",
        }
        return ArchiveReport(
            verdict="DRY_RUN",
            dry_run=True,
            gate=gate,
            planned=planned,
            schema_head=gate.schema_head,
            restore_dir=str(restore_into),
            next_manual_steps=_next_manual_steps(
                str(restore_into), gate.schema_head, _live_names_of(db_abs)
            ),
        )
    try:
        # the drill's own rule, applied BEFORE the envelope exists: a reused --restore-into
        # would otherwise publish an envelope and then refuse the restore, leaving an orphan.
        # FIX-RT: the protected chain and the absent leaf were proved above; now the drill
        # creates the leaf 0700 and verifies uid and mode by fstat
        for descriptor in _fresh_directory(restore_into, "restore target"):
            os.close(descriptor)
    except DrillRefused as error:
        raise CutoverRefused("RESTORE_TARGET_NOT_FRESH", str(error)) from None
    _require_excluded(source, "before the drill")
    try:
        # the drill runs the flatten gate ITSELF over the held capability, at this command's
        # instant, and verifies by what that run returned (r3, P1-2): the gate run above served
        # the dry-run report and the sidecar admission; a refusal here is the gate's own
        gate, report = run_archive_drill(
            source, out_dir, restore_into, now=now, clock=clock, keys=keys
        )
    except DrillRefused as error:
        return ArchiveReport(
            verdict="REFUSED",
            dry_run=False,
            gate=gate,
            code="DRILL_REFUSED",
            failures=(f"DRILL_REFUSED: {error}",),
            schema_head=gate.schema_head,
        )
    envelope = Path(report.manifest.backup_path).parent
    manifest_path = envelope / MANIFEST_NAME
    restore_dir = report.restore.target_dir if report.restore is not None else str(restore_into)
    facts: dict[str, Any] = {
        "gate": gate,
        "envelope": str(envelope),
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256_of(manifest_path, "manifest"),
        "cleartext_sha256": report.manifest.sha256,
        "ciphertext_sha256": report.manifest.ciphertext_sha256,
        "schema_head": report.manifest.schema_head,
        "restore_dir": restore_dir,
        "drill": {
            "verdict": report.verdict,
            "rpo_s": report.rpo_s,
            "rto_s": report.rto_s,
            "failures": list(report.failures),
            "acceptance": None if report.restore is None else report.restore.acceptance,
            # N3-4: the drill removed its cleartext copy under --restore-into, and the leaf
            "restore_copy_removed": (
                None if report.restore is None else report.restore.restore_copy_removed
            ),
            "removed": [] if report.restore is None else list(report.restore.removed),
        },
    }
    if report.verdict != ARCHIVE_VERIFIED:
        # N3-4: the archive verified but the drill could not remove its cleartext copy — the
        # envelope is good and named, the live store untouched; the move-aside waits until the
        # copy is gone (remove it by hand, then re-run: the next run makes a new envelope)
        only_removal = bool(report.failures) and all(
            failure.startswith(f"{RESTORE_COPY_NOT_REMOVED}:") for failure in report.failures
        )
        return ArchiveReport(
            verdict="REFUSED" if only_removal else "ARCHIVE_NOT_VERIFIED",
            dry_run=False,
            code=RESTORE_COPY_NOT_REMOVED if only_removal else "ARCHIVE_NOT_VERIFIED",
            failures=tuple(f"drill: {failure}" for failure in report.failures)
            or ("drill: verdict " + report.verdict,),
            **facts,
        )
    stamp = envelope.name.removeprefix(ENVELOPE_PREFIX)
    source.close_connection()  # the move needs the held descriptor only, never the connection
    try:
        moved, exclusion, sentinels = _move_aside(
            source, sidecars, report.manifest.source_identity, stamp, protected
        )
    except MoveIncomplete as error:
        return ArchiveReport(
            verdict="MOVE_INCOMPLETE" if error.moved else "REFUSED",
            dry_run=False,
            code=error.code,
            failures=(f"{error.code}: {error}",),
            moved_aside=error.moved,
            not_moved=error.not_moved,
            foreign_renamed=error.foreign,
            sentinels=error.sentinels,
            **facts,
        )
    except CutoverRefused as error:
        return ArchiveReport(
            verdict="REFUSED",
            dry_run=False,
            code=error.code,
            failures=(f"{error.code}: {error}",),
            sentinels=error.sentinels,
            **facts,
        )
    db_abs = db_path if db_path.is_absolute() else Path.cwd() / db_path
    return ArchiveReport(
        verdict="ARCHIVED",
        dry_run=False,
        moved_aside=moved,
        sentinels=sentinels,
        exclusion=exclusion,
        next_manual_steps=_next_manual_steps(
            restore_dir, report.manifest.schema_head, _live_names_of(db_abs)
        ),
        **facts,
    )


def main(argv: list[str] | None = None, *, clock: Clock | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m chronos.operations.cutover_archive",
        description=(
            "N3 cutover: prove the pre-cutover store flat and quiet, archive it with the "
            "restore drill's encrypted envelope, require a VERIFIED restore, and only then "
            "move the old store aside by rename. Dry-run by default."
        ),
    )
    parser.add_argument(
        "--db", required=True, type=Path, help="the pre-cutover store (never opened for writing)"
    )
    parser.add_argument(
        "--out", required=True, type=Path, help="directory that receives the envelope"
    )
    parser.add_argument(
        "--restore-into",
        required=True,
        type=Path,
        help="a FRESH directory for the isolated restore (a cleartext copy, removed by this run)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="run the drill and, on VERIFIED, move the old store aside (default: dry-run)",
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = archive(args.db, args.out, args.restore_into, apply=args.apply, clock=clock)
    except CutoverRefused as error:
        report = ArchiveReport(
            verdict="REFUSED",
            dry_run=not args.apply,
            code=error.code,
            failures=(f"{error.code}: {error}",),
        )
    print(json.dumps(report.to_dict(), indent=2 if args.pretty else None, sort_keys=True))
    return 0 if report.verdict in ("DRY_RUN", "ARCHIVED") else 2


if __name__ == "__main__":
    sys.exit(main())
