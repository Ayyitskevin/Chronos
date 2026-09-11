"""Hash-chained JSONL audit log.

Each record embeds the SHA-256 of the previous record, so any in-place edit,
deletion, or reordering breaks the chain and is detected by ``verify_chain``.
Writes are flushed and fsynced before returning; a failed write raises so the
caller (execution engine / halt monitor) can halt trading on audit failure.

Appends are serialized against a fresh head (2026-09-11, D1 Gap B). Until then
each ``AuditLog`` cached sequence and head at construction and advanced only its
private copy, so two instances on one path — an accidental double start, a
second process — both minted the same successor, and recovery read only the
last row, so a writer extended a chain whose middle was already broken. Every
append is now one transaction: a per-path thread lock plus
``fcntl.flock(LOCK_EX)`` on an owner-only sibling ``<log>.lock``, acquired
BEFORE recovery; a fresh verification of the WHOLE chain under the lock; then
write, flush, fsync, release. Full verification is O(n) per append — the
platform log grows by a handful of records per cycle, and correctness was
preferred over the constant factor (D1 §4 row C). ``verify_chain`` takes no
lock and creates nothing: monitoring and campaign status are read-only consumers.

No secrets, credentials, or raw account identifiers may be written here;
callers pass already-sanitized payloads. Payload values are JSON-serializable
primitives only.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import IO, NoReturn

from chronos.utils.secure_files import secure_owner_only

_GENESIS = "0" * 64
_LOCK_SUFFIX = ".lock"
# O_NOFOLLOW refuses a symlink planted at the lock path; O_NONBLOCK keeps a planted
# FIFO from turning the refusal into a hang (the S_ISREG check below then refuses it).
_LOCK_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class AuditLogCorruptionError(RuntimeError):
    """The audit log could not be safely recovered or extended.

    Raised when the chain is unreadable or broken anywhere — construction and
    ``append`` both re-verify the whole chain — or when the sibling lock file
    cannot be trusted (a symlink, not a regular file, not ours, replaced while
    the lock was being taken). One specific, catchable exception rather than a
    raw ``json.JSONDecodeError``, ``KeyError`` or ``OSError`` so a caller can
    halt trading cleanly (see ``HaltReason.AUDIT_LOG_FAILURE``). Nothing is
    repaired: the writer refuses and says why.
    """


@dataclass(frozen=True, slots=True)
class AuditRecord:
    sequence: int
    at_utc: str
    kind: str
    payload: dict[str, object]
    previous_hash: str
    record_hash: str


def _hash_record(sequence: int, at_utc: str, kind: str, payload_json: str, prev: str) -> str:
    material = f"{sequence}|{at_utc}|{kind}|{payload_json}|{prev}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class _ChainBreak(Exception):
    """One line failed verification; ``str()`` is the ``line N: reason`` detail."""

    def __init__(self, line_number: int, reason: str) -> None:
        super().__init__(f"line {line_number}: {reason}")
        self.line_number = line_number
        self.reason = reason


def _walk_chain(handle: IO[str]) -> tuple[int, str]:
    """Verify every record in order. Return ``(count, last_hash)`` or raise ``_ChainBreak``.

    Shared by ``verify_chain`` (which reports) and the writer's recovery (which
    refuses), so the two cannot disagree about what a broken chain is.
    """

    previous = _GENESIS
    expected_sequence = 0
    for line_number, line in enumerate(handle, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            payload_json = json.dumps(record["payload"], sort_keys=True, separators=(",", ":"))
            recomputed = _hash_record(
                int(record["sequence"]),
                str(record["at_utc"]),
                str(record["kind"]),
                payload_json,
                str(record["previous_hash"]),
            )
        except (KeyError, ValueError, TypeError) as error:
            raise _ChainBreak(line_number, f"unreadable record: {error}") from error
        if int(record["sequence"]) != expected_sequence:
            raise _ChainBreak(line_number, "sequence gap")
        if record["previous_hash"] != previous:
            raise _ChainBreak(line_number, "chain break")
        if recomputed != record["record_hash"]:
            raise _ChainBreak(line_number, "hash mismatch")
        previous = str(record["record_hash"])
        expected_sequence += 1
    return expected_sequence, previous


def _thread_lock_for(key: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = _THREAD_LOCKS[key] = threading.Lock()
        return lock


def _refuse_lock(lock_path: Path, problem: str, cause: BaseException | None = None) -> NoReturn:
    raise AuditLogCorruptionError(
        f"audit log lock at {lock_path} {problem}; refusing to touch the log"
    ) from cause


class AuditLog:
    """Append-only writer over one JSONL file, serialized on ``<path>.lock``."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock_path = path.with_name(path.name + _LOCK_SUFFIX)
        # Keyed lexically (no symlink resolution) so two spellings of one path
        # share a lock in-process; the flock covers the cross-process case.
        self._thread_lock = _thread_lock_for(os.path.abspath(os.fspath(path)))
        with self._exclusive():
            self._sequence, self._last_hash = self._recover()

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Hold the per-path thread lock and an exclusive ``flock`` on the lock file.

        Both, because neither substitutes for the other: ``flock`` binds an
        open file description, which a second thread in this process does not
        share, and a ``threading.Lock`` says nothing about another process
        (the registry reasons the same way, ``registry/ledger.py``). Neither is
        reentrant; nothing here re-enters.
        """

        with self._thread_lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = self._open_lock()
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self._assert_lock_still_named(descriptor)
                try:
                    yield
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _open_lock(self) -> int:
        """Open (creating if absent) the sibling lock as an owned, owner-only regular file.

        Every check runs on the descriptor that is then locked, so nothing can
        be swapped between check and use; the mode is set with ``fchmod`` so it
        is exact regardless of the process umask.
        """

        lock_path = self._lock_path
        try:
            descriptor = os.open(lock_path, _LOCK_FLAGS, 0o600)
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.EMLINK):
                # O_NOFOLLOW reports ELOOP for a symlink at the final component.
                _refuse_lock(lock_path, "is a symlink", error)
            _refuse_lock(lock_path, f"could not be opened safely: {error}", error)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                _refuse_lock(lock_path, "is not a regular file")
            if opened.st_uid != os.geteuid():
                _refuse_lock(
                    lock_path,
                    f"is owned by uid {opened.st_uid}, not this process's effective user",
                )
            if opened.st_nlink != 1:
                _refuse_lock(lock_path, f"has {opened.st_nlink} links; it must have exactly one")
            os.fchmod(descriptor, 0o600)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _assert_lock_still_named(self, descriptor: int) -> None:
        """After ``flock``: the lock path must still name the inode we hold.

        ``flock`` binds an inode, not a name. If the lock file were unlinked
        and recreated between our open and our lock, two writers would each
        hold an exclusive lock on a different inode and neither would exclude
        the other. A symlink or a fresh file at the name has a different inode.
        """

        held = os.fstat(descriptor)
        try:
            named = os.stat(self._lock_path, follow_symlinks=False)
        except FileNotFoundError as error:
            _refuse_lock(self._lock_path, "was removed while the lock was being acquired", error)
        if (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino):
            _refuse_lock(self._lock_path, "was replaced while the lock was being acquired")

    def _recover(self) -> tuple[int, str]:
        """Walk the whole chain; return ``(next_sequence, last_hash)``. Caller holds the lock.

        The whole chain, not the last row: a writer that trusted the last row
        alone extended a chain whose middle was already broken, and the break
        surfaced only when a separate verifier ran (D1 §6).
        """

        if not self._path.exists():
            return 0, _GENESIS
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                return _walk_chain(handle)
        except _ChainBreak as error:
            raise AuditLogCorruptionError(
                f"audit log is broken at {error}; refusing to append past it: {self._path}"
            ) from error

    def append(self, kind: str, payload: dict[str, object]) -> AuditRecord:
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._exclusive():
            # Fresh head, under the lock: never the cached one.
            sequence, previous_hash = self._recover()
            at = datetime.now(tz=UTC).isoformat()
            record_hash = _hash_record(sequence, at, kind, payload_json, previous_hash)
            record = AuditRecord(
                sequence=sequence,
                at_utc=at,
                kind=kind,
                payload=payload,
                previous_hash=previous_hash,
                record_hash=record_hash,
            )
            line = json.dumps(
                {
                    "sequence": record.sequence,
                    "at_utc": record.at_utc,
                    "kind": record.kind,
                    "payload": payload,
                    "previous_hash": record.previous_hash,
                    "record_hash": record.record_hash,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            secure_owner_only(self._path)
            self._sequence = sequence + 1
            self._last_hash = record_hash
        return record


class ChainState(StrEnum):
    """The three distinguishable outcomes of verifying an audit chain.

    ABSENT is not a weaker VALID. A missing audit log means the chain could not be
    examined at all, so nothing about tamper-evidence has been established — the same
    distinction the certification plane draws between NOT_CERTIFIED and UNVERIFIED.
    """

    VALID = "VALID"
    BROKEN = "BROKEN"
    ABSENT = "ABSENT"


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """The verdict and its detail.

    Deliberately NOT a ``(bool, str)`` tuple and deliberately not unpackable: the old
    signature returned ``True`` for a missing file, so every caller that wrote
    ``ok, detail = verify_chain(...)`` silently reported an absent chain as verified.
    Making the type un-unpackable turns each of those into a loud failure rather than a
    wrong answer, which is why the migration is by type rather than by convention.

    No ``.ok`` is provided on purpose: it would have to choose a truthiness for ABSENT,
    which is the defect this exists to remove. ``__bool__`` RAISES for the same reason —
    omitting it is not enough, because a dataclass without ``__bool__`` is truthy by
    default, so ``if verify_chain(path):`` answered True for a missing chain and
    reproduced the original bug one layer down.
    """

    state: ChainState
    detail: str

    def __bool__(self) -> bool:
        """Refuse truth-testing; there is no correct answer for ABSENT.

        Raising means ``if verify_chain(path):`` and ``assert verify_chain(path)`` fail
        loudly at the call site instead of silently reporting an unexamined chain as a
        verified one. Compare ``.state`` against a :class:`ChainState` member instead.
        """

        raise TypeError(
            f"ChainVerification({self.state.value}) has no truth value: compare .state "
            f"against a ChainState member (VALID, BROKEN or ABSENT) instead"
        )


def verify_chain(path: Path) -> ChainVerification:
    """Verify the whole chain, distinguishing absent from valid from broken.

    Read-only and lock-free: it creates no lock file, because its consumers
    (monitoring, campaign status, the CLI verifier) are read-only by contract.
    """

    if not path.exists():
        return ChainVerification(ChainState.ABSENT, "no audit log yet")
    try:
        with path.open("r", encoding="utf-8") as handle:
            count, _last_hash = _walk_chain(handle)
    except _ChainBreak as error:
        return ChainVerification(ChainState.BROKEN, str(error))
    return ChainVerification(ChainState.VALID, f"chain intact ({count} records)")
