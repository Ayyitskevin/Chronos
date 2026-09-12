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

The log and its lock are reached descriptor-relative and no-follow, the way the
registry ledger reaches its files (``registry/ledger.py``): the parent directory
is opened by an ``O_NOFOLLOW`` component walk from the root, the log and lock
are opened by name against that directory with ``O_NOFOLLOW``, every check runs
on the descriptor that is then used, and a running writer pins the parent's and
the log's inode identity so a path swapped underneath it refuses instead of
being followed. Before this, the writer followed the log target for read and
append and only afterwards let ``secure_owner_only`` notice a symlink (D1 §6).

The capability is held in NAME space for the whole transaction, not only at the
opens (F2 security review, HOLD at c770267): the canonical parent path, the lock
name and the log name are re-established to designate the held descriptors
after the lock is taken, immediately before the write, and after fsync before
success is reported. A lock unlinked and recreated after the first check, a
parent renamed away and replaced, or a log swapped after open used to let two
writers each complete against the same head; each now refuses, and a refusal
after fsync says plainly that the record may be durable in a displaced file and
that success is not reported. ``_assert_transaction_bound`` is that check, one
helper for every boundary, called again before and after the head anchor is
published.

The head anchor (2026-09-12, D1 Gap A / D2). A hash chain alone has no
expectation about its own length: delete a complete tail, or restore an older
copy of the file, and the surviving prefix is internally perfect. Every append
therefore also publishes a sibling ``<stem>.head.json`` holding exactly
``{"count": N, "last_hash": H}`` — the registry ledger's convention and bytes —
through a unique same-directory temp opened ``O_EXCL`` at 0600, fsynced, renamed
over the anchor with directory descriptors, then a parent fsync; after the log
fsync, before the cache update and the unlock. ``verify_chain`` judges the pair:
the chain first, so its precise first-line failure still wins, then the anchor
(missing, malformed, behind, ahead, or naming a different head is BROKEN). A log
with no anchor is BROKEN — deployed legacy files exist, and creating their anchor
automatically would certify whatever they happen to hold, rollback included —
until the owner runs ``bootstrap_anchor`` (``python -m chronos.cli
bootstrap-audit-anchor``), which verifies the entire bare chain first and appends
no record. Both files absent is the only fresh state. A crash after the log fsync
and before the anchor replace leaves the log one record ahead of its anchor:
that pair is BROKEN by design ("crash window"), refused by every writer, and
needs reviewed recovery; a lock-free reader racing an append can see the same
window. The anchor detects accidental tail loss, single-file rollback, and
concurrent-writer forks. It is not an off-host root of trust: an owner-user
actor can recompute or co-restore log and anchor consistently.

Log, lock, anchor and temp are capability entries: owned regular files with one
link and mode exactly 0600. Entries this module creates are ``fchmod``-ed to
0600; an existing entry found looser is refused and reported, never tightened —
unlike ``secure_owner_only`` for the halt and ledger files, whose silent
tightening would here hide an exposure event from the owner (HOLD F3). Every
failure of anchor publication is the one catchable ``AuditLogCorruptionError``
naming the step and the cause, and the unwind removes the unique temp name only
while it still designates this transaction's inode (HOLD F1, F2).

Residual, not fixable here: ``flock`` that FAILS is a catchable refusal
(``AuditLogCorruptionError``), but a filesystem that returns success without
enforcing advisory locks — some NFS mounts — cannot be told apart from one that
does. Two writers on such a mount can still fork the chain; the platform's data
directory is expected to be local.

No secrets, credentials, or raw account identifiers may be written here;
callers pass already-sanitized payloads. Payload values are JSON-serializable
primitives only.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import io
import json
import os
import stat
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import IO, NoReturn

_GENESIS = "0" * 64
_LOCK_SUFFIX = ".lock"
_ANCHOR_SUFFIX = ".head.json"
_ANCHOR_KEYS = frozenset({"count", "last_hash"})
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# Directory components of the parent walk: real directories only, never through a link.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC | _NOFOLLOW
# The lock: O_NOFOLLOW refuses a planted symlink; O_NONBLOCK keeps a planted FIFO from
# turning the refusal into a hang (the S_ISREG check below then refuses it). O_CREAT is added
# only when the name was observed absent; an existing lock is opened as found and must be exact.
_LOCK_FLAGS = os.O_RDWR | _CLOEXEC | _NOFOLLOW | _NONBLOCK
# The log: one descriptor per transaction, read from the start and appended at the end.
_LOG_FLAGS = os.O_RDWR | os.O_APPEND | _CLOEXEC | _NOFOLLOW | _NONBLOCK
# A leaf read by name (the anchor; the verifier's log): no-follow, and non-blocking so a
# planted FIFO is refused by the S_ISREG check instead of hanging the reader.
_ENTRY_FLAGS = os.O_RDONLY | _CLOEXEC | _NOFOLLOW | _NONBLOCK
# The anchor's unique temp: created exclusively, never through a link.
_TEMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW | _NONBLOCK
# A name resolved to an inode that a racing atomic replace has just unlinked shows zero
# links; the reader re-resolves the name a bounded number of times before refusing.
_ZERO_LINK_REOPEN_ATTEMPTS = 8
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
#: Lock keys held by the current thread. ``threading.Lock`` is not reentrant, so an
#: append that reaches ``append`` again on the same thread would deadlock on itself;
#: this lets it refuse instead.
_HELD_BY_THIS_THREAD = threading.local()


class AuditLogCorruptionError(RuntimeError):
    """The audit log could not be safely recovered or extended.

    Raised when the chain is unreadable or broken anywhere — construction and
    ``append`` both re-verify the whole chain — or when its head anchor is missing,
    malformed, or names a different head, or when the log, its lock, its anchor, or
    a parent directory cannot be trusted (a symlink, not a regular file or real
    directory, not ours, more than one link, replaced under a running writer,
    replaced while the lock was being taken, displaced during the transaction),
    or when the OS lock itself cannot be taken. One specific, catchable
    exception rather than a raw ``json.JSONDecodeError``, ``KeyError`` or
    ``OSError`` so a caller can halt trading cleanly (see
    ``HaltReason.AUDIT_LOG_FAILURE``). Nothing is repaired: the writer refuses
    and says why.
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


def _walk_chain(lines: Iterable[str]) -> tuple[int, str]:
    """Verify every record in order. Return ``(count, last_hash)`` or raise ``_ChainBreak``.

    Shared by ``verify_chain`` (which reports) and the writer's recovery (which
    refuses), so the two cannot disagree about what a broken chain is.
    """

    previous = _GENESIS
    expected_sequence = 0
    for line_number, line in enumerate(lines, start=1):
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


# ------------------------------------------------------------------------ the head anchor


def _anchor_name_for(log_name: str) -> str:
    """``<stem>.head.json`` beside the log: ``platform_audit.jsonl`` owns
    ``platform_audit.head.json``.

    (The registry's convention, ``registry/ledger.py``.)
    """

    return Path(log_name).stem + _ANCHOR_SUFFIX


def _anchor_bytes(count: int, last_hash: str) -> bytes:
    """The one representation: ``json.dumps(sort_keys=True) + "\\n"``, as the registry writes it."""

    return (json.dumps({"count": count, "last_hash": last_hash}, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    decoded: dict[str, object] = {}
    for key, value in pairs:
        if key in decoded:
            raise ValueError(f"duplicate JSON key {key!r}")
        decoded[key] = value
    return decoded


def _reject_non_finite(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number {value!r}")


def _decode_anchor(raw: bytes) -> tuple[int, str]:
    """Exactly ``{"count": <true int >= 0>, "last_hash": <64 lowercase hex>}``, or ``ValueError``.

    Booleans, duplicate keys, missing or unknown keys, non-UTF-8, trailing values, and
    invalid hashes are all rejected; a count of zero must name the genesis hash.
    """

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"not UTF-8: {error}") from error
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except json.JSONDecodeError as error:
        raise ValueError(f"not valid JSON: {error.msg}") from error
    if not isinstance(decoded, dict):
        raise ValueError("not an object")
    if set(decoded) != _ANCHOR_KEYS:
        raise ValueError(
            "keys do not match schema; "
            f"missing={sorted(_ANCHOR_KEYS - set(decoded))}, "
            f"unknown={sorted(set(decoded) - _ANCHOR_KEYS)}"
        )
    count = decoded["count"]
    if type(count) is not int or count < 0:
        raise ValueError("count must be a true integer >= 0")
    last_hash = decoded["last_hash"]
    if (
        type(last_hash) is not str
        or len(last_hash) != 64
        or any(character not in "0123456789abcdef" for character in last_hash)
    ):
        raise ValueError("last_hash must be 64 lowercase hex characters")
    if count == 0 and last_hash != _GENESIS:
        raise ValueError("count 0 requires the genesis hash")
    return count, last_hash


@dataclass(frozen=True, slots=True)
class _PairVerdict:
    """What one log/anchor snapshot amounts to, and the head a writer may extend from."""

    verification: ChainVerification
    count: int
    last_hash: str


def _broken(detail: str) -> _PairVerdict:
    return _PairVerdict(ChainVerification(ChainState.BROKEN, detail), 0, _GENESIS)


def _verify_pair(log_text: str | None, anchor: bytes | None) -> _PairVerdict:
    """Judge a log (``None`` = absent) beside its anchor (``None`` = absent): the D2 §2 matrix.

    The chain is validated first so its precise first-line failure wins; then the anchor
    must exist, decode, match the record count, and name the last hash. Both absent is
    the only ABSENT. A log with no anchor is BROKEN — an existing file must never be
    inferred fresh from its contents — until the owner bootstraps it.
    """

    if log_text is None:
        if anchor is None:
            return _PairVerdict(
                ChainVerification(ChainState.ABSENT, "no audit log or head anchor yet"),
                0,
                _GENESIS,
            )
        try:
            _decode_anchor(anchor)
        except ValueError as error:
            return _broken(f"head anchor unreadable and audit log absent: {error}")
        return _broken("audit log truncation/deletion: head anchor exists but audit log is absent")
    try:
        count, last_hash = _walk_chain(io.StringIO(log_text))
    except _ChainBreak as error:
        return _broken(str(error))
    if anchor is None:
        return _broken("head anchor missing for existing audit log; owner bootstrap required")
    try:
        expected_count, expected_hash = _decode_anchor(anchor)
    except ValueError as error:
        return _broken(f"head anchor unreadable: {error}")
    if expected_count > count:
        return _broken(
            f"audit log truncation/rollback: {count} records but anchor expects {expected_count}"
        )
    if expected_count < count:
        return _broken(
            "uncommitted audit append/crash window: "
            f"{count} records but anchor expects {expected_count}"
        )
    if expected_hash != last_hash:
        return _broken("head hash mismatch (rollback or replacement)")
    return _PairVerdict(
        ChainVerification(ChainState.VALID, f"chain + anchor intact ({count} records)"),
        count,
        last_hash,
    )


# -------------------------------------------------------------------- descriptor helpers


def _thread_lock_for(key: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = _THREAD_LOCKS[key] = threading.Lock()
        return lock


def _held_keys() -> set[str]:
    keys = getattr(_HELD_BY_THIS_THREAD, "keys", None)
    if keys is None:
        keys = set()
        _HELD_BY_THIS_THREAD.keys = keys
    return keys


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _refuse(subject: str, problem: str, cause: BaseException | None = None) -> NoReturn:
    raise AuditLogCorruptionError(
        f"{subject} {problem}; refusing to touch the audit log"
    ) from cause


def _require_owned_regular(descriptor: int, subject: str) -> os.stat_result:
    """A regular file owned by this effective user, exactly one link, mode exactly 0600.

    Mode is a capability condition (D2 §1), not something to repair on the way past. An
    existing log, lock or anchor found looser than 0600 has been exposed, and silently
    re-tightening it — what ``chronos.utils.secure_files.secure_owner_only`` does for the
    halt and ledger files, whose contract that remains — would hide the exposure event
    from the owner. The audit capability refuses and reports the mode as found. Entries
    this transaction CREATES are ``fchmod``-ed to 0600 before this check runs, so the
    process umask cannot fail them.
    """

    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        _refuse(subject, "is not a regular file")
    if opened.st_uid != os.geteuid():
        _refuse(subject, f"is owned by uid {opened.st_uid}, not this process's effective user")
    if opened.st_nlink != 1:
        _refuse(subject, f"has {opened.st_nlink} links; it must have exactly one")
    mode = stat.S_IMODE(opened.st_mode)
    if mode != 0o600:
        _refuse(
            subject,
            f"has mode {oct(mode)}, not 0o600; a looser mode is an exposure for the owner to "
            "review, not something to tighten silently",
        )
    return opened


def _read_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("audit head anchor write made no progress")
        view = view[written:]


@contextmanager
def _publication_step(step: str, subject: str, clause: str) -> Iterator[None]:
    """Turn an ``OSError`` from one publication step into the one catchable error.

    The detail names the step, the entry and the cause (strerror and errno), and carries
    the durability clause when a record is already fsynced. Callers that catch only
    ``AuditLogCorruptionError`` — the CLI, the service — then refuse instead of crashing.
    """

    try:
        yield
    except OSError as error:
        cause = error.strerror or str(error)
        if error.errno is not None:
            cause += f" (errno {error.errno})"
        _refuse(subject, f"failed while {step}: {cause}" + clause, error)


def _open_entry(parent_fd: int, name: str, subject: str) -> int | None:
    """Open ``name`` read-only against the held parent, no-follow; ``None`` if absent.

    The descriptor is checked before it is used — a regular file owned by this effective
    user with exactly one link — so a symlink, FIFO, hard link or foreign file refuses
    without reading through to its target. An entry whose inode shows zero links was
    replaced between the name lookup and the check (an anchor being published by a
    writer this lock-free reader is racing): the name is re-resolved a bounded number of
    times, and refused if it stays unlinked.
    """

    for _attempt in range(_ZERO_LINK_REOPEN_ATTEMPTS):
        try:
            descriptor = os.open(name, _ENTRY_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.EMLINK):
                # O_NOFOLLOW reports ELOOP for a symlink at the final component.
                _refuse(subject, "is a symlink", error)
            _refuse(subject, f"could not be opened safely: {error}", error)
        if os.fstat(descriptor).st_nlink == 0 and stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            continue
        try:
            _require_owned_regular(descriptor, subject)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    _refuse(subject, "remained unlinked while being read; it is being replaced or was removed")


def _read_entry(parent_fd: int, name: str, subject: str) -> bytes | None:
    descriptor = _open_entry(parent_fd, name, subject)
    if descriptor is None:
        return None
    try:
        return _read_all(descriptor)
    finally:
        os.close(descriptor)


def _open_existing_parent(parent: Path) -> int | None:
    """An ``O_NOFOLLOW`` component walk from the root that creates nothing.

    ``None`` when a component is missing (there is nothing there to judge); a component
    that exists but is a symlink or not a real directory refuses.
    """

    components = parent.parts[1:]
    descriptor = os.open(os.sep, _DIR_FLAGS)
    transferred = False
    try:
        for depth, component in enumerate(components, start=1):
            try:
                child = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                return None
            except OSError as error:
                display = Path(os.sep, *components[:depth])
                _refuse(
                    f"audit log parent {display}", "is a symlink or not a real directory", error
                )
            os.close(descriptor)
            descriptor = child
        transferred = True
        return descriptor
    finally:
        if not transferred:
            os.close(descriptor)


class AuditLog:
    """Append-only writer over one JSONL file, serialized on ``<path>.lock``.

    Every append also publishes the sibling head anchor ``<stem>.head.json``.
    """

    def __init__(self, path: Path) -> None:
        self._bind(path)
        with self._exclusive() as (parent_fd, _lock_fd):
            self._sequence, self._last_hash = self._recover_at(parent_fd)

    def _bind(self, path: Path) -> None:
        """The path capability: names, keys and pins, before anything is opened."""

        self._path = path
        absolute = Path(os.path.abspath(os.fspath(path)))
        if absolute.name in {"", ".", ".."}:
            raise ValueError(f"audit log path must name a file: {path}")
        self._parent = absolute.parent
        self._name = absolute.name
        self._lock_name = absolute.name + _LOCK_SUFFIX
        self._anchor_name = _anchor_name_for(absolute.name)
        # Keyed lexically (no symlink resolution) so two spellings of one path
        # share a lock in-process; the flock covers the cross-process case.
        self._lock_key = os.fspath(absolute)
        self._thread_lock = _thread_lock_for(self._lock_key)
        #: Test seam. When set, called inside the transaction after fresh recovery and
        #: before the write, with both locks held, so a test can park one writer inside
        #: the critical section and prove another cannot cross. No effect when None.
        self._after_recovery: Callable[[], None] | None = None
        # Identities this writer has seen; a later transaction that finds a different
        # inode under the same name refuses rather than following the swap.
        self._pinned_parent: tuple[int, int] | None = None
        self._pinned_log: tuple[int, int] | None = None

    @classmethod
    def _capability_only(cls, path: Path) -> AuditLog:
        """The path capability with no recovery — for ``bootstrap_anchor``, which judges the
        bare chain itself because construction would (correctly) refuse it."""

        instance = cls.__new__(cls)
        instance._bind(path)
        return instance

    # ------------------------------------------------------------------ the transaction

    @contextmanager
    def _exclusive(self) -> Iterator[tuple[int, int]]:
        """Hold the per-path thread lock and an exclusive ``flock``; yield (parent fd, lock fd).

        Both locks, because neither substitutes for the other: ``flock`` binds an
        open file description, which a second thread in this process does not
        share, and a ``threading.Lock`` says nothing about another process
        (the registry reasons the same way, ``registry/ledger.py``). Neither is
        reentrant, and nothing legitimate re-enters: an append reached from inside an
        append on the same thread is refused, because the alternative is a deadlock.
        """

        held = _held_keys()
        if self._lock_key in held:
            raise AuditLogCorruptionError(
                f"re-entrant audit log transaction on {self._path}: an append cannot run "
                "inside another append on the same thread; the lock is not reentrant, so "
                "this refuses rather than deadlocking"
            )
        with self._thread_lock:
            held.add(self._lock_key)
            try:
                parent_fd = self._open_parent()
                try:
                    lock_fd = self._open_lock(parent_fd)
                    try:
                        self._lock_exclusive(lock_fd)
                        self._assert_transaction_bound(
                            parent_fd, lock_fd, None, when="after taking the lock"
                        )
                        try:
                            yield parent_fd, lock_fd
                        finally:
                            # Closing the descriptor releases the lock anyway; a failing
                            # LOCK_UN must not mask the exception that is unwinding.
                            with suppress(OSError):
                                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)
                finally:
                    os.close(parent_fd)
            finally:
                held.discard(self._lock_key)

    def _lock_exclusive(self, lock_fd: int) -> None:
        """Take the OS lock; a filesystem that cannot is an audit refusal, not a crash."""

        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except OSError as error:
            _refuse(
                f"audit log lock at {self._parent / self._lock_name}",
                f"could not be locked (flock failed: {error.strerror or error}, errno "
                f"{error.errno}); the filesystem may not support advisory locking",
                error,
            )

    def _open_parent(self) -> int:
        """Open the parent directory by an ``O_NOFOLLOW`` walk from the root; pin its identity.

        Missing components are created, as ``Path.mkdir(parents=True)`` used to do
        for the first append, but through the directory descriptor rather than the
        path, so a symlink planted at any component refuses instead of redirecting
        the creation. A component that is a symlink or not a real directory refuses.
        """

        components = self._parent.parts[1:]
        descriptor = os.open(os.sep, _DIR_FLAGS)
        try:
            for depth, component in enumerate(components, start=1):
                display = Path(os.sep, *components[:depth])
                subject = f"audit log parent {display}"
                try:
                    child = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
                except FileNotFoundError:
                    try:
                        os.mkdir(component, dir_fd=descriptor)
                    except FileExistsError:
                        pass  # a racing creator; the no-follow open below still judges it
                    except OSError as error:
                        _refuse(subject, f"could not be created: {error}", error)
                    try:
                        child = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
                    except OSError as error:
                        _refuse(subject, "is a symlink or not a real directory", error)
                except OSError as error:
                    _refuse(subject, "is a symlink or not a real directory", error)
                os.close(descriptor)
                descriptor = child
            identity = _identity(os.fstat(descriptor))
            if self._pinned_parent is None:
                self._pinned_parent = identity
            elif self._pinned_parent != identity:
                _refuse(
                    f"audit log parent {self._parent}", "was replaced after this writer pinned it"
                )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _open_lock(self, parent_fd: int) -> int:
        """Open (creating if absent) the sibling lock as an owned, owner-only regular file.

        Every check runs on the descriptor that is then locked, so nothing can be swapped
        between check and use. When the name is observed absent the lock is created with
        ``O_CREAT`` (deliberately not ``O_EXCL``: two fresh processes constructing at once
        both legitimately create it, and the loser must open the winner's lock, not
        refuse) and ``fchmod``-ed to 0600 so the process umask cannot widen it — a file that
        appeared in that window was created moments ago by this program, not exposed. An
        existing lock is opened as found and must already be exact: a looser mode is
        refused, not repaired (see ``_require_owned_regular``).
        """

        subject = f"audit log lock at {self._parent / self._lock_name}"
        try:
            os.stat(self._lock_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            creating = True
        else:
            creating = False
        flags = _LOCK_FLAGS | (os.O_CREAT if creating else 0)
        try:
            descriptor = os.open(self._lock_name, flags, 0o600, dir_fd=parent_fd)
        except FileNotFoundError as error:
            _refuse(subject, "was removed while this transaction was opening it", error)
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.EMLINK):
                # O_NOFOLLOW reports ELOOP for a symlink at the final component.
                _refuse(subject, "is a symlink", error)
            _refuse(subject, f"could not be opened safely: {error}", error)
        try:
            if creating:
                os.fchmod(descriptor, 0o600)
            self._require_owned_regular(descriptor, subject)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    # ------------------------------------------------------------ the binding assertion

    def _assert_transaction_bound(
        self,
        parent_fd: int,
        lock_fd: int,
        log_fd: int | None,
        *,
        when: str,
        record_written: bool = False,
    ) -> None:
        """Re-establish that the canonical names still designate the held descriptors.

        Descriptors bind inodes, not names. A lock unlinked and recreated after
        our check lets a second writer lock a different inode; a parent renamed
        away and replaced lets a second writer operate at the canonical path
        while we write into the displaced directory; a log swapped after open
        leaves our record on an inode nothing names. Each is a fork nothing
        downstream would attribute to a single writer, so the names are checked
        at every boundary: after the lock is taken, immediately before the write,
        after the log fsync immediately before the anchor is touched, and after
        the anchor and its directory are durable, before success is reported.
        Called once per boundary.

        ``record_written`` selects the after-fsync wording: the record may then
        be durable in a displaced file, and success is still not reported.
        """

        self._assert_parent_designates(parent_fd, when=when, record_written=record_written)
        self._assert_name_designates(
            parent_fd,
            self._lock_name,
            lock_fd,
            subject=f"audit log lock at {self._parent / self._lock_name}",
            when=when,
            record_written=record_written,
        )
        if log_fd is not None:
            self._assert_name_designates(
                parent_fd,
                self._name,
                log_fd,
                subject=f"audit log at {self._parent / self._name}",
                when=when,
                record_written=record_written,
            )

    def _assert_parent_designates(self, parent_fd: int, *, when: str, record_written: bool) -> None:
        """A fresh no-follow walk from the root must reach the directory we hold."""

        components = self._parent.parts[1:]
        subject = f"audit log parent {self._parent}"
        descriptor = os.open(os.sep, _DIR_FLAGS)
        try:
            for depth, component in enumerate(components, start=1):
                try:
                    child = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
                except OSError as error:
                    display = Path(os.sep, *components[:depth])
                    _refuse(
                        f"audit log parent {display}",
                        f"no longer designates a real directory ({when})"
                        + _durability_clause(record_written),
                        error,
                    )
                os.close(descriptor)
                descriptor = child
            if _identity(os.fstat(descriptor)) != _identity(os.fstat(parent_fd)):
                _refuse(
                    subject,
                    f"was displaced or replaced under this transaction ({when})"
                    + _durability_clause(record_written),
                )
        finally:
            os.close(descriptor)

    def _assert_name_designates(
        self,
        parent_fd: int,
        name: str,
        held_fd: int,
        *,
        subject: str,
        when: str,
        record_written: bool,
    ) -> None:
        """The name, resolved against the held parent, must be the inode we hold."""

        held = os.fstat(held_fd)
        try:
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as error:
            _refuse(
                subject,
                f"was removed under this transaction ({when})" + _durability_clause(record_written),
                error,
            )
        if _identity(named) != _identity(held):
            _refuse(
                subject,
                f"was replaced under this transaction ({when})"
                + _durability_clause(record_written),
            )

    def _open_log(self, parent_fd: int, *, create: bool) -> int | None:
        """Open the log by name against the parent, no-follow; ``None`` if absent and not creating.

        The descriptor is checked before use — a regular file, owned by this
        effective user, with exactly one link, mode exactly 0600 — and its identity is
        pinned the first time this writer sees the file, so a later transaction that
        finds a different inode under the same name (a swap, a restore over a running
        writer) refuses instead of extending whatever is there now. ``create`` is used
        only after an absent name was observed under the lock: the log is then created
        ``O_CREAT`` and ``fchmod``-ed to 0600 (the umask cannot widen it; a name that
        appeared meanwhile can only belong to a writer holding a different lock inode, and
        the binding checks refuse that transaction before it writes). An existing log is
        opened as found and a looser mode is refused, never tightened.
        """

        subject = f"audit log at {self._parent / self._name}"
        flags = _LOG_FLAGS | (os.O_CREAT if create else 0)
        try:
            descriptor = os.open(self._name, flags, 0o600, dir_fd=parent_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.EMLINK):
                _refuse(subject, "is a symlink", error)
            _refuse(subject, f"could not be opened safely: {error}", error)
        try:
            if create:
                os.fchmod(descriptor, 0o600)
            opened = self._require_owned_regular(descriptor, subject)
            identity = _identity(opened)
            if self._pinned_log is None:
                self._pinned_log = identity
            elif self._pinned_log != identity:
                _refuse(subject, "was replaced after this writer pinned it")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _require_owned_regular(descriptor: int, subject: str) -> os.stat_result:
        return _require_owned_regular(descriptor, subject)

    # ------------------------------------------------------------------------ the anchor

    def _read_anchor(self, parent_fd: int) -> tuple[bytes | None, tuple[int, int] | None]:
        """The anchor's bytes and inode identity by name, or ``(None, None)``.

        Read-only: a symlink, FIFO, hard link or foreign file at the anchor's name refuses
        before anything is read through it, and nothing is created or repaired here.
        """

        subject = f"audit head anchor at {self._parent / self._anchor_name}"
        descriptor = _open_entry(parent_fd, self._anchor_name, subject)
        if descriptor is None:
            return None, None
        try:
            return _read_all(descriptor), _identity(os.fstat(descriptor))
        finally:
            os.close(descriptor)

    def _publish_anchor(
        self,
        parent_fd: int,
        lock_fd: int,
        log_fd: int,
        *,
        count: int,
        last_hash: str,
        prior: tuple[int, int] | None,
        record_written: bool,
    ) -> None:
        """Publish ``{count, last_hash}`` atomically beside the log, inside the held transaction.

        Binds the names immediately before the anchor is touched; creates a unique
        same-directory temp ``O_EXCL|O_NOFOLLOW`` at 0600 and checks it on its descriptor;
        writes and fsyncs the bytes; proves the temp name still designates that descriptor
        and the destination is still the entry validated at the start of the transaction
        (``prior``, or still absent); renames through the held parent descriptor; fsyncs the
        parent; re-opens the published entry by name and requires the inode that was
        written, byte for byte, with the temp name gone; then binds the names again.

        Every step's ``OSError`` becomes the one catchable ``AuditLogCorruptionError``
        naming the step and the cause (HOLD F1); the durable pair is then exactly what the
        disk holds — a stale anchor beside the new record (a crash window) for a failure
        before the rename, a complete pair for a failure of the parent fsync after it. The
        unwind removes the unique temp name only while it still designates this
        transaction's inode (HOLD F2): a name is not ours to delete just because we chose
        it. Mirrors the registry's ``publish_anchor``. The caller has reported nothing yet.
        """

        self._assert_transaction_bound(
            parent_fd, lock_fd, log_fd, when="before the anchor", record_written=record_written
        )
        clause = _durability_clause(record_written)
        anchor_subject = f"audit head anchor at {self._parent / self._anchor_name}"
        temporary = f".{self._anchor_name}.{uuid.uuid4().hex}.tmp"
        temp_subject = f"audit head anchor temporary at {self._parent / temporary}"
        content = _anchor_bytes(count, last_hash)
        try:
            descriptor = os.open(temporary, _TEMP_FLAGS, 0o600, dir_fd=parent_fd)
        except OSError as error:
            _refuse(temp_subject, f"could not be created exclusively: {error}" + clause, error)
        written_identity = _identity(os.fstat(descriptor))
        published = False
        try:
            with _publication_step("making the temporary private", temp_subject, clause):
                os.fchmod(descriptor, 0o600)
            _require_owned_regular(descriptor, temp_subject)
            with _publication_step("writing the temporary", temp_subject, clause):
                _write_all(descriptor, content)
            with _publication_step("fsyncing the temporary", temp_subject, clause):
                os.fsync(descriptor)
            self._assert_name_designates(
                parent_fd,
                temporary,
                descriptor,
                subject=temp_subject,
                when="before the anchor replace",
                record_written=record_written,
            )
            self._assert_anchor_destination(
                parent_fd, prior, subject=anchor_subject, record_written=record_written
            )
            with _publication_step(
                "renaming the temporary over the anchor", anchor_subject, clause
            ):
                os.replace(temporary, self._anchor_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            published = True
            with _publication_step("fsyncing the parent directory", anchor_subject, clause):
                os.fsync(parent_fd)
        except BaseException as error:
            os.close(descriptor)
            descriptor = -1
            note = ""
            if not published:
                note = self._unlink_only_own_temp(parent_fd, temporary, written_identity)
            if note and isinstance(error, AuditLogCorruptionError):
                raise AuditLogCorruptionError(f"{error}{note}") from error
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self._assert_anchor_published(
            parent_fd,
            temporary,
            written_identity,
            content,
            subject=anchor_subject,
            temp_subject=temp_subject,
            record_written=record_written,
        )
        self._assert_transaction_bound(
            parent_fd, lock_fd, log_fd, when="after the anchor", record_written=record_written
        )

    def _unlink_only_own_temp(
        self, parent_fd: int, temporary: str, written_identity: tuple[int, int]
    ) -> str:
        """Remove the unique temp name only while it still designates this transaction's inode.

        After the fsync an actor can rename our inode away and plant its own entry under
        the unique name; deleting that entry would destroy something this transaction did
        not create and leave our inode stranded under the actor's name. So the name is
        unlinked only when it still names the inode we wrote; otherwise it is left in place
        and the returned note says so, for the raised detail. Returns "" when nothing needs
        saying (removed, or already gone).
        """

        try:
            named = os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return ""
        if _identity(named) == written_identity:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent_fd)
            return ""
        return (
            f"; the temporary name {temporary} no longer designates this transaction's inode "
            f"(dev {written_identity[0]}, inode {written_identity[1]}) and was left in place "
            "because this transaction did not create what is there now; the inode this "
            "transaction wrote may be stranded under another name"
        )

    def _assert_anchor_destination(
        self,
        parent_fd: int,
        prior: tuple[int, int] | None,
        *,
        subject: str,
        record_written: bool,
    ) -> None:
        """The anchor's name must still be the entry validated at the start, or still absent."""

        clause = _durability_clause(record_written)
        try:
            named = os.stat(self._anchor_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as error:
            if prior is None:
                return
            _refuse(
                subject,
                "was removed under this transaction (before the anchor replace)" + clause,
                error,
            )
        if prior is None:
            _refuse(subject, "appeared under this transaction (before the anchor replace)" + clause)
        if _identity(named) != prior:
            _refuse(
                subject, "was replaced under this transaction (before the anchor replace)" + clause
            )

    def _assert_anchor_published(
        self,
        parent_fd: int,
        temporary: str,
        written_identity: tuple[int, int],
        content: bytes,
        *,
        subject: str,
        temp_subject: str,
        record_written: bool,
    ) -> None:
        """By name, the anchor is now the inode this transaction wrote, and the temp is gone."""

        clause = _durability_clause(record_written)
        descriptor = _open_entry(parent_fd, self._anchor_name, subject)
        if descriptor is None:
            _refuse(subject, "is absent after publication" + clause)
        try:
            if _identity(os.fstat(descriptor)) != written_identity:
                _refuse(subject, "is not the inode this transaction published" + clause)
            with _publication_step("re-reading the published anchor", subject, clause):
                published = _read_all(descriptor)
            if published != content:
                _refuse(subject, "does not hold the bytes this transaction published" + clause)
        finally:
            os.close(descriptor)
        try:
            os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        _refuse(temp_subject, "still exists after publication" + clause)

    # ------------------------------------------------------------------------ recovery

    def _recover(self, handle: IO[str] | None, anchor: bytes | None) -> tuple[int, str]:
        """Judge the pair under the lock; return ``(next_sequence, last_hash)`` or refuse.

        ``handle`` is the open log (``None`` when there is no log yet) and ``anchor`` the
        sibling's bytes (``None`` when absent). The whole chain, not the last row: a
        writer that trusted the last row alone extended a chain whose middle was
        already broken, and the break surfaced only when a separate verifier ran
        (D1 §6). And the anchor beside it (D2 §2): anything but VALID, or the fresh
        both-absent ABSENT, refuses — a legacy log with no anchor included, because
        certifying it here would certify a rollback. Caller holds the lock.
        """

        text = None if handle is None else handle.read()
        verdict = _verify_pair(text, anchor)
        if verdict.verification.state is ChainState.BROKEN:
            self._refuse_broken(verdict.verification.detail)
        return verdict.count, verdict.last_hash

    def _refuse_broken(self, detail: str) -> NoReturn:
        raise AuditLogCorruptionError(
            f"audit log at {self._path} is BROKEN: {detail}; refusing to append past it"
        )

    def _recover_at(self, parent_fd: int) -> tuple[int, str]:
        anchor, _prior = self._read_anchor(parent_fd)
        descriptor = self._open_log(parent_fd, create=False)
        if descriptor is None:
            return self._recover(None, anchor)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            return self._recover(handle, anchor)

    # -------------------------------------------------------------------------- append

    def append(self, kind: str, payload: dict[str, object]) -> AuditRecord:
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._exclusive() as (parent_fd, lock_fd):
            anchor, prior_anchor = self._read_anchor(parent_fd)
            existing = self._open_log(parent_fd, create=False)
            if existing is None:
                # Both absent is the only fresh state (D2 §1): an anchor with no log is
                # deletion or truncation of the log and refuses here, before anything is
                # created. The log itself is created only after recovery and the seam,
                # right before the write, so a transaction refused at the binding check
                # leaves no bare log behind for the owner to bootstrap.
                sequence, previous_hash = self._recover(None, anchor)
                if self._after_recovery is not None:
                    self._after_recovery()
                created = self._open_log(parent_fd, create=True)
                if created is None:  # pragma: no cover - O_CREAT never yields "absent"
                    _refuse(f"audit log at {self._parent / self._name}", "could not be created")
                handle = os.fdopen(created, "r+", encoding="utf-8")
            else:
                # One descriptor for the whole transaction: read from the start for the
                # fresh head, then O_APPEND puts the write at the end of that same inode.
                handle = os.fdopen(existing, "r+", encoding="utf-8")
            with handle:
                if existing is not None:
                    sequence, previous_hash = self._recover(handle, anchor)
                    if self._after_recovery is not None:
                        self._after_recovery()
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
                # The names must still designate what we hold, right before the mutation...
                self._assert_transaction_bound(
                    parent_fd, lock_fd, handle.fileno(), when="before the write"
                )
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                # ...the record is durable. The anchor is published inside the same
                # transaction: the names are bound again immediately before it is touched
                # and once more after it and the directory are durable — only then is the
                # cache updated and the lock released. Between the log fsync and the anchor
                # replace the durable pair reads as a crash window; every failure raises.
                self._publish_anchor(
                    parent_fd,
                    lock_fd,
                    handle.fileno(),
                    count=sequence + 1,
                    last_hash=record_hash,
                    prior=prior_anchor,
                    record_written=True,
                )
            self._sequence = sequence + 1
            self._last_hash = record_hash
        return record


def _durability_clause(record_written: bool) -> str:
    if not record_written:
        return ""
    return (
        "; the record was written and fsynced to the inode this writer held, so it may be "
        "durable in a displaced file, and success is NOT reported"
    )


def bootstrap_anchor(path: Path) -> Path | None:
    """Publish the first head anchor for a legacy log that has none — an owner's act (D2 §3).

    Runs under the same thread lock, ``flock`` and path capability as an append. Refuses
    if anything at all exists at the anchor's name — a matching anchor, a malformed one, a
    symlink, a FIFO, a hard link; none is touched — verifies the ENTIRE bare chain, then
    publishes ``{count, last_hash}`` through the same temp/fsync/rename/parent-fsync
    sequence as an append, with the names bound before and after. No audit record is
    appended: the durable act is the anchor itself, and a bootstrap row would make the
    chain attest its own trust transition. Returns the anchor path, or ``None`` when there
    is no log to anchor. An empty but readable legacy log may be bootstrapped explicitly
    (count 0, genesis hash).
    """

    writer = AuditLog._capability_only(path)
    with writer._exclusive() as (parent_fd, lock_fd):
        anchor_subject = f"audit head anchor at {writer._parent / writer._anchor_name}"
        try:
            os.stat(writer._anchor_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _refuse(
                anchor_subject,
                "already exists; bootstrap publishes only a first anchor and never overwrites",
            )
        descriptor = writer._open_log(parent_fd, create=False)
        if descriptor is None:
            return None
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            try:
                count, last_hash = _walk_chain(io.StringIO(handle.read()))
            except _ChainBreak as error:
                raise AuditLogCorruptionError(
                    f"audit log at {path} is BROKEN: {error}; refusing to anchor a broken chain"
                ) from error
            writer._publish_anchor(
                parent_fd,
                lock_fd,
                handle.fileno(),
                count=count,
                last_hash=last_hash,
                prior=None,
                record_written=False,
            )
    return writer._parent / writer._anchor_name


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


def verify_chain_text(text: str) -> ChainVerification:
    """Verify a chain from text a caller already holds; VALID or BROKEN, never ABSENT.

    A consumer that verifies a PATH and then re-reads the path to derive from it
    has two reads, and a file replaced between them lets a VALID verdict
    authorise BROKEN rows (the monitoring snapshot did exactly this). Verifying
    the captured text lets verification and derivation run over one read. Line
    numbers and detail strings are the same as ``verify_chain``'s over the same
    bytes. ABSENT is a statement about a path, so only ``verify_chain`` says it.
    """

    try:
        count, _last_hash = _walk_chain(io.StringIO(text))
    except _ChainBreak as error:
        return ChainVerification(ChainState.BROKEN, str(error))
    return ChainVerification(ChainState.VALID, f"chain intact ({count} records)")


def verify_chain(path: Path) -> ChainVerification:
    """Verify the chain AND its head anchor, distinguishing absent from valid from broken.

    Read-only and lock-free: it creates no lock file, because its consumers
    (monitoring, campaign status, the CLI verifier) are read-only by contract.
    The parent is reached by a no-follow component walk that creates nothing, and
    both leaves are read by name against it, no-follow: a symlink, FIFO, hard link
    or foreign file at either name is BROKEN without its target being read. The
    anchor is read before the log so that a reader racing an append (which fsyncs
    the log first, then publishes the anchor) can only observe the in-flight
    window as the honestly named "crash window", never as a truncation.
    """

    absolute = Path(os.path.abspath(os.fspath(path)))
    parent, name = absolute.parent, absolute.name
    anchor_name = _anchor_name_for(name)
    try:
        parent_fd = _open_existing_parent(parent)
        if parent_fd is None:
            return _verify_pair(None, None).verification
        try:
            anchor = _read_entry(
                parent_fd, anchor_name, f"audit head anchor at {parent / anchor_name}"
            )
            log = _read_entry(parent_fd, name, f"audit log at {parent / name}")
        finally:
            os.close(parent_fd)
    except AuditLogCorruptionError as error:
        return ChainVerification(ChainState.BROKEN, str(error))
    text: str | None
    if log is None:
        text = None
    else:
        try:
            text = log.decode("utf-8")
        except UnicodeDecodeError as error:
            return ChainVerification(ChainState.BROKEN, f"audit log is not UTF-8: {error}")
    return _verify_pair(text, anchor).verification
