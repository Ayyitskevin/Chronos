"""N3 cutover archive — strategy (iii): archive the pre-N3 store, verify it, move it aside.

One command, owner-run, with every Chronos process stopped::

    python -m chronos.operations.cutover_archive --db data/chronos.db \\
        --out /var/backups/chronos/n3 --restore-into /tmp/chronos-n3-verify [--apply] [--pretty]

Dry-run (the default) runs the gate below, prints what ``--apply`` would do, writes nothing
to the store and creates no envelope. One directory effect is SQLite's, not the command's: a
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
   set the move may rename — a sidecar that appears later is never renamed (with frames it
   refuses the move; without, it is reported ``left_in_place``).
2. **The R-2 restore drill**, reused whole (:func:`chronos.operations.restore_drill.run_drill`)
   over the same capability: sqlite's online backup API through the bound connection, ``age``
   to the two recipients named by the existing custody variables, one atomically published
   envelope whose manifest records the source inode, an isolated restore and its verifiers —
   with an ``ArchiveBinding`` (the head read in step 1, its numeric version, the held inode)
   that the DRILL checks: version below the running one, the envelope's source identity, the
   exact head (a retired schema that the running repository refuses by design; see the drill).
3. **Verdict ``ARCHIVE_VERIFIED`` required** — the drill's distinct verdict for a bound
   retired-schema archive, never the repository's ``VERIFIED``. Otherwise nothing moves: the
   live store is untouched and the report names the envelope and the restore copy that exist.
4. **ONLY THEN the store moves aside, by rename, never delete:** in the store's directory,
   reached by the drill's no-follow walk: the bound connection is closed and every admitted
   sidecar is leased likewise (a process holding one open read-write — a read-only SQLite
   connection does — is ``WRITER_OPEN``); the store's lease must be whole; the manifest's
   ``source_identity`` must be the held inode (the archive IS of this store); the admitted
   ``-wal`` must still be frame-free by ``fstat`` on the held descriptor (the same inode can
   grow in place: identity is not content — ``WAL_FRAMES_PRESENT``); the store by name must be
   the held inode, every admitted sidecar by name its held inode, every target name absent — all
   before any rename — then ONE ``renameat2(RENAME_NOREPLACE)`` per admitted file, each
   re-proved after it: the archived name must be the held inode, and when it is NOT (the name
   was exchanged in the instant before the syscall) the foreign entry is renamed BACK by name
   (``RENAME_NOREPLACE``, never over anything) and reported in ``foreign_renamed`` — the admitted
   object was not moved, so its name is in ``not_moved``. ``chronos.db`` →
   ``chronos.db.archived-<stamp>``, the admitted ``-wal``/``-shm`` likewise, ``<stamp>`` being
   the envelope's own (``chronos-<stamp>``), so the moved-aside files are bound by name to their
   archive. After the last rename every lease must still be whole and the admitted ``-wal`` still
   frame-free — the proof, in the report's ``exclusion``, that the admitted set was quiet through
   the irreversible boundary. A failure after the first rename is ``MOVE_INCOMPLETE`` with
   ``moved_aside`` and ``not_moved`` exact; the runbook's rollback renames back by name.
5. **A JSON report**: envelope, manifest path and sha256, cleartext and ciphertext sha256,
   the moved-aside names, and the next MANUAL steps (the runbook's) — never a fingerprint,
   a position id or a row payload; counts and the run's own id, trigger and times only.

What this command never does: it never generates, reads or prints the persistence key (the
module does not touch the environment or ``Settings`` — the drill reads its own age-key
variables); never starts the service; never initializes the fresh database; never opens the
old store for writing; never deletes anything. Runbook: ``docs/ops/N3-CUTOVER.md``.
"""

from __future__ import annotations

import argparse
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
    _RENAME_NOREPLACE,
    ARCHIVE_VERIFIED,
    MANIFEST_NAME,
    AgeKeys,
    ArchiveBinding,
    BoundSource,
    Clock,
    DrillRefused,
    SourceBusy,
    SourceSwapped,
    _fresh_directory,
    _lease_intact,
    _lease_read,
    _open_regular,
    _renameat2,
    _schema_head_on,
    _sha256_of,
    _walk_directory,
    load_age_keys,
    open_bound_source,
    run_drill,
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
ENVELOPE_PREFIX = "chronos-"
CLEAN = ReconciliationStatus.RECONCILED.value
ORIGIN_CLASSES = ("MANAGED", "WHEEL", "MANUAL", "FOREIGN")


class CutoverRefused(RuntimeError):
    """A typed refusal: ``code`` names the check, the message is the whole diagnosis.
    Nothing was written unless the message says what exists."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
    for suffix in SIDECAR_SUFFIXES:
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


def _rename_back(
    dfd: int, parent_abs: Path, archived: str, name: str, found: tuple[int, int] | None
) -> dict[str, str | None]:
    """The entry our rename moved to ``archived`` was not the admitted object: rename it back to
    ``name`` (``RENAME_NOREPLACE`` — never over anything) and say where it is now."""

    restored_to: str | None = None
    try:
        _renameat2(dfd, archived, dfd, name, _RENAME_NOREPLACE)
    except OSError:
        pass
    else:
        if _identity_by_name(dfd, name) == found:
            restored_to = str(parent_abs / name)
    return {
        "renamed_to": str(parent_abs / archived),
        "restored_to": restored_to,
        "identity": "absent" if found is None else f"{found[0]}:{found[1]}",
    }


def _move_aside(
    source: BoundSource,
    sidecars: tuple[AdmittedSidecar, ...],
    manifest_identity: tuple[int, int] | None,
    stamp: str,
) -> tuple[tuple[dict[str, str], ...], tuple[str, ...], dict[str, object]]:
    """Rename the store and its ADMITTED sidecars to ``<name>.archived-<stamp>`` in their own
    directory, with the admitted set proven quiet through the boundary (r2, Daybreak P1-1).

    Before ANY rename: every admitted sidecar is leased (the bound connection held them
    read-write until it closed; a process still holding one is ``WRITER_OPEN``); the store's
    lease must be whole; the manifest's ``source_identity`` must be the held inode; the admitted
    ``-wal`` must be frame-free by ``fstat`` on its held descriptor; the store by name must be the
    held inode, every admitted sidecar by name its held inode; a non-admitted ``-wal`` with frames
    refuses; every target is proved absent. Then ONE ``renameat2(RENAME_NOREPLACE)`` per file,
    each re-proved after it by the archived name's identity — a foreign entry the rename moved
    (the name exchanged in the instant before the syscall) is renamed BACK by name and reported,
    and the admitted name counts as not moved. After the last rename the leases and the WAL are
    re-proved; the proof is returned as the report's ``exclusion``. Nothing is ever deleted or
    renamed over; a non-admitted sidecar is never renamed and is returned as ``left_in_place``.
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
    dfd, parent_abs = _walk_directory(source.path.parent, "database directory", create=False)
    try:
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
        left_in_place: list[str] = []
        for suffix in SIDECAR_SUFFIXES:
            name = source.path.name + suffix
            if name in expected:
                continue
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if suffix == "-wal" and stat.S_ISREG(st.st_mode) and st.st_size > WAL_HEADER_BYTES:
                raise CutoverRefused(
                    "WAL_FRAMES_PRESENT",
                    f"{parent_abs / name} appeared after the gate admitted the sidecars and holds "
                    f"{st.st_size} bytes of write-ahead log: a writer is active or a foreign file "
                    "was planted; nothing moved — stop every Chronos process and inspect by hand",
                )
            left_in_place.append(str(parent_abs / name))
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
        moved: list[dict[str, str]] = []
        for name in names:
            try:
                _renameat2(dfd, name, dfd, targets[name], _RENAME_NOREPLACE)
            except OSError as error:
                code = "TARGET_EXISTS" if isinstance(error, FileExistsError) else "MOVE_FAILED"
                detail = (
                    f"rename of {parent_abs / name} to {parent_abs / targets[name]} failed: "
                    f"[errno {error.errno}] {error.strerror}"
                )
                if not moved:
                    raise CutoverRefused(code, f"{detail}; nothing moved") from None
                not_moved = tuple(str(parent_abs / other) for other in names[len(moved) :])
                raise MoveIncomplete(
                    code,
                    f"{detail}; {len(moved)} file(s) already moved — rename them back by name "
                    f"({RUNBOOK}, Rollback) before anything starts",
                    moved=tuple(moved),
                    not_moved=not_moved,
                ) from None
            found = _identity_by_name(dfd, targets[name])
            if found != expected[name]:
                foreign = _rename_back(dfd, parent_abs, targets[name], name, found)
                where = (
                    f"renamed back to {foreign['restored_to']}"
                    if foreign["restored_to"] is not None
                    else f"left at {foreign['renamed_to']} (it could not be renamed back)"
                )
                not_moved = tuple(str(parent_abs / other) for other in names[len(moved) :])
                raise MoveIncomplete(
                    "SOURCE_SWAPPED",
                    f"the entry at {parent_abs / name} in the instant of its rename was not the "
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
            moved.append({"from": str(parent_abs / name), "to": str(parent_abs / targets[name])})
        os.fsync(dfd)
        problem = _not_quiet(source, sidecars, parent_abs)
        if problem is not None:
            raise MoveIncomplete(
                problem[0],
                f"{problem[1]} — every rename succeeded, but the admitted set was not quiet "
                f"through the boundary: rename the moved files back by name ({RUNBOOK}, "
                "Rollback), stop every Chronos process, and investigate before anything starts",
                moved=tuple(moved),
                not_moved=(),
            )
        wal = _wal_of(sidecars)
        exclusion: dict[str, object] = {
            "mechanism": "kernel read lease (fcntl F_SETLEASE) on the held store and every "
            "admitted sidecar",
            "leased": [source.path.name, *(sidecar.name for sidecar in sidecars)],
            "intact_after_move": True,
            "wal_bytes_after_move": None if wal is None else os.fstat(wal.fd).st_size,
        }
    finally:
        os.close(dfd)
    return tuple(moved), tuple(left_in_place), exclusion


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
    left_in_place: tuple[str, ...] = ()
    #: an entry that sat at an admitted name at the instant of its rename and was NOT the
    #: admitted object: renamed back by name (``restored_to``) or left where the rename put it
    foreign_renamed: tuple[dict[str, str | None], ...] = ()
    #: ARCHIVED only: the writer-exclusion proof through the move
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
            "left_in_place": list(self.left_in_place),
            "foreign_renamed": [dict(entry) for entry in self.foreign_renamed],
            "exclusion": None if self.exclusion is None else dict(self.exclusion),
            "next_manual_steps": list(self.next_manual_steps),
            "runbook": RUNBOOK,
        }


def _next_manual_steps(restore_dir: str, schema_head_value: str) -> tuple[str, ...]:
    return (
        f"Remove the restore copy under {restore_dir} (rm -r): it is a CLEARTEXT copy of the "
        "archived store and the drill has already verified it.",
        "Record the envelope path, manifest_sha256 and ciphertext_sha256 printed above in the "
        f"cutover log ({RUNBOOK} step 3).",
        "Generate the persistence key YOURSELF: python -c 'import secrets; "
        "print(secrets.token_hex(32))' — paste it into the host's 0600 .env as "
        "CHRONOS_PERSISTENCE_KEY. Nobody and nothing else generates, sees or handles it.",
        "Age-encrypt ONE escrow copy of that key off-host to your recovery key "
        f"({RUNBOOK} step 6).",
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
    """Gate → (dry-run stops here) → drill → VERIFIED required → move aside → report.

    Refusals BEFORE the drill raise :class:`CutoverRefused` (nothing exists); after the
    drill they are returned as reports that name the envelope and the restore copy.
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
    try:
        source = open_bound_source(db_path, "source database")
    except SourceSwapped as error:
        raise CutoverRefused("SOURCE_SWAPPED", str(error)) from None
    except DrillRefused as error:
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
                source, sidecars, gate, out_dir, restore_into, apply=apply, clock=clock, keys=keys
            )
        finally:
            for sidecar in sidecars:
                os.close(sidecar.fd)
    finally:
        source.close()


def _archive_admitted(
    source: BoundSource,
    sidecars: tuple[AdmittedSidecar, ...],
    gate: GateReport,
    out_dir: Path,
    restore_into: Path,
    *,
    apply: bool,
    clock: Clock,
    keys: AgeKeys,
) -> ArchiveReport:
    db_path = source.path
    now = clock.wall()
    _require_excluded(source, "after the gate")
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
            "note": "the stamp is the envelope's; --apply takes it from the backup's taken_at, "
            "and moves a sidecar only when it is present",
        }
        return ArchiveReport(
            verdict="DRY_RUN",
            dry_run=True,
            gate=gate,
            planned=planned,
            schema_head=gate.schema_head,
            restore_dir=str(restore_into),
            next_manual_steps=_next_manual_steps(str(restore_into), gate.schema_head),
        )
    try:
        # the drill's own rule, applied BEFORE the envelope exists: a reused --restore-into
        # would otherwise publish an envelope and then refuse the restore, leaving an orphan
        os.close(_fresh_directory(restore_into, "restore target"))
    except DrillRefused as error:
        raise CutoverRefused("RESTORE_TARGET_NOT_FRESH", str(error)) from None
    _require_excluded(source, "before the drill")
    try:
        report = run_drill(
            db_path,
            out_dir,
            restore_into,
            clock=clock,
            keys=keys,
            source=source,
            archive=ArchiveBinding(
                schema_head=gate.schema_head,
                schema_version=gate.schema_version,
                source_identity=source.identity,
            ),
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
        },
    }
    if report.verdict != ARCHIVE_VERIFIED:
        return ArchiveReport(
            verdict="ARCHIVE_NOT_VERIFIED",
            dry_run=False,
            code="ARCHIVE_NOT_VERIFIED",
            failures=tuple(f"drill: {failure}" for failure in report.failures)
            or ("drill: verdict " + report.verdict,),
            **facts,
        )
    stamp = envelope.name.removeprefix(ENVELOPE_PREFIX)
    source.close_connection()  # the move needs the held descriptor only, never the connection
    try:
        moved, left_in_place, exclusion = _move_aside(
            source, sidecars, report.manifest.source_identity, stamp
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
            **facts,
        )
    except CutoverRefused as error:
        return ArchiveReport(
            verdict="REFUSED",
            dry_run=False,
            code=error.code,
            failures=(f"{error.code}: {error}",),
            **facts,
        )
    return ArchiveReport(
        verdict="ARCHIVED",
        dry_run=False,
        moved_aside=moved,
        left_in_place=left_in_place,
        exclusion=exclusion,
        next_manual_steps=_next_manual_steps(restore_dir, report.manifest.schema_head),
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
        help="a FRESH directory for the drill's isolated restore (a cleartext copy; remove it)",
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
