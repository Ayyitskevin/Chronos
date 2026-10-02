"""The dedicated per-stream anchor guard (FU1-GUARD-1, relocated by FU1-GUARD-2).

Kevin approved this module on 2026-10-02 for the GUARD ONLY (spec FU1g5.3, section 0.12.1
item 1). NOT WIRED: no module under ``src/chronos/`` imports it; P1-NEW-22 stays open.
"""

from __future__ import annotations

import contextlib
import fcntl
import math
import os
import stat
import sys
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass

# >>> anchor guard (FU1-GUARD-1)
# The dedicated per-stream anchor guard of spec FU1g5.1 sections 1-5, IN ISOLATION.
#
# NOT WIRED: nothing in Chronos calls this yet. It is not used by the issuance route or the
# drain, and must not be until the drain re-presentation blocker (P1-NEW-22) is closed. It
# performs no anchor-file I/O and derives no path: the caller INJECTS the lock path, and
# the wait deadline is a required argument (the default/maximum, R11, is undecided).
#
# It lives in this module only because the approved production set is three files; the
# cost (a larger module that mixes filesystem locking with engine configuration) is
# disclosed in the FU1-GUARD-1 handoff. Linux only (advisory flock on a local filesystem).

#: Release steps, in the documented release order (the reverse of acquisition).
_ANCHOR_RELEASE_STEPS = (
    "unlock",
    "close_lock_fd",
    "discard_held_key",
    "release_thread_lock",
    "close_dir_fd",
)
_ANCHOR_NAME_MAX = 255
_ANCHOR_FLOCK_POLL_S = 0.01
_ANCHOR_DIR_MODE = 0o700
_ANCHOR_LOCK_MODE = 0o600

_GUARD_REGISTRY_LOCK = threading.Lock()
#: One identity-keyed thread lock per (directory st_dev, st_ino, lock name).
_GUARD_THREAD_LOCKS: dict[tuple[int, int, str], threading.Lock] = {}
_GUARD_HELD = threading.local()
#: Guards whose release could not be established as safe. Strongly referenced, never
#: removed in-process: every later in-process acquisition of the key refuses until exit.
_STRANDED: dict[tuple[int, int, str], AnchorGuard] = {}
_STRANDED_UNKEYED: list[AnchorGuard] = []
_LIVE_GUARDS: weakref.WeakSet[AnchorGuard] = weakref.WeakSet()
#: Test-only seam: called as hook(step, "pre"|"post") immediately before and after each
#: release step's system or library call. A no-op while unset.
_ANCHOR_GUARD_STEP_HOOK: Callable[[str, str], None] | None = None


class AnchorGuardError(RuntimeError):
    """Base class for every typed anchor-guard failure."""


class AnchorGuardRefused(AnchorGuardError):
    """The guard refuses as found: platform, path, ownership, mode, identity or lock support."""


class AnchorGuardTimeout(AnchorGuardError):
    """The bounded wait expired; nothing is held and nothing was mutated."""


class AnchorGuardReentrant(AnchorGuardError):
    """The same thread already holds this guard; it refuses rather than deadlocking."""


class AnchorGuardStranded(AnchorGuardError):
    """A previous release of this identity could not be established as safe (no retry)."""


class AnchorGuardCleanupFailed(AnchorGuardError):
    """An ordinary cleanup failure; primary per R12, the raw error is ``__cause__``."""


@dataclass(frozen=True, slots=True)
class AnchorCleanupFailure:
    step: int
    name: str
    error: BaseException
    asynchronous: bool


@dataclass(frozen=True, slots=True)
class AnchorCleanupRecord:
    """Everything a release collected; attached to the primary exception it raises."""

    lock_path: str
    failures: tuple[AnchorCleanupFailure, ...]
    original: BaseException | None
    still_held: tuple[str, ...]
    state: str


def _anchor_held_keys() -> set[tuple[int, int, str]]:
    held = getattr(_GUARD_HELD, "keys", None)
    if held is None:
        held = set()
        _GUARD_HELD.keys = held
    return held


def _anchor_step_hook(step: str, phase: str) -> None:
    hook = _ANCHOR_GUARD_STEP_HOOK
    if hook is not None:
        hook(step, phase)


class AnchorGuard:
    """Exclusive, bounded, per-stream guard on a stable dedicated lock file.

    Acquisition order (spec section 2.2 as it applies to the guard alone): 0 platform
    predicate, 2 directory descriptor plus validation, 3 key plus the stranded lookup,
    4 same-thread re-entrancy, 5 bounded thread lock, 6 held-key, 7 lock descriptor,
    8 bounded non-blocking flock, 9 identity re-check. Release is the exact reverse and
    runs every step (section 3.1); the state afterwards is derived from what is still
    held. A guard is single-use.
    """

    def __init__(self, lock_path: str | os.PathLike[str], *, wait_s: float) -> None:
        if isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)):
            raise AnchorGuardRefused(
                "wait_s must be a finite, non-negative number of seconds within the "
                "platform lock-timeout bound; "
                "the guard has no default deadline and never clamps one"
            )
        try:
            normalized_wait_s = float(wait_s)
        except (OverflowError, ValueError):
            raise AnchorGuardRefused(
                "wait_s must be a finite, non-negative number of seconds within the "
                "platform lock-timeout bound; the guard has no default deadline and "
                "never clamps one"
            ) from None
        if (
            not math.isfinite(normalized_wait_s)
            or normalized_wait_s < 0
            or normalized_wait_s > threading.TIMEOUT_MAX
        ):
            raise AnchorGuardRefused(
                "wait_s must be a finite, non-negative number of seconds within the "
                "platform lock-timeout bound; the guard has no default deadline and "
                "never clamps one"
            )
        raw = os.fspath(lock_path)
        directory, name = os.path.split(raw)
        if (
            not name
            or name in (".", "..")
            or "\x00" in raw
            or len(os.fsencode(name)) > _ANCHOR_NAME_MAX
        ):
            raise AnchorGuardRefused(f"invalid lock name in {raw!r}")
        self._lock_path = raw
        self._dir_path = directory or "."
        self._lock_name = name
        self._wait_s = normalized_wait_s
        self.state = "NEW"
        self.cleanup_record: AnchorCleanupRecord | None = None
        self._key: tuple[int, int, str] | None = None
        self._dir_fd: int | None = None
        self._lock_fd: int | None = None
        self._flocked = False
        self._thread_lock: threading.Lock | None = None
        self._thread_lock_held = False
        self._held_set: set[tuple[int, int, str]] | None = None
        self._held_key = False
        _LIVE_GUARDS.add(self)

    def __enter__(self) -> AnchorGuard:
        return self.acquire()

    def __exit__(self, *_exc_info: object) -> None:
        self.release()

    def held_resources(self) -> tuple[str, ...]:
        held: list[str] = []
        if self._lock_fd is not None:
            held.append("lock_fd (flock HELD)" if self._flocked else "lock_fd")
        if self._held_key:
            held.append("held-key")
        if self._thread_lock_held:
            held.append("thread lock")
        if self._dir_fd is not None:
            held.append("dir_fd")
        return tuple(held)

    # ------------------------------------------------------------------ acquisition

    def acquire(self) -> AnchorGuard:
        if self.state != "NEW":
            raise AnchorGuardRefused(f"an AnchorGuard is single-use; this one is {self.state}")
        self.state = "ACQUIRING"
        try:
            self._acquire_steps()
        except BaseException:
            self.release()
            raise
        self.state = "HELD"
        return self

    def _acquire_steps(self) -> None:
        if sys.platform != "linux":
            raise AnchorGuardRefused(
                f"the anchor guard is supported on Linux only (sys.platform={sys.platform})"
            )
        if not os.path.isabs(self._lock_path):
            raise AnchorGuardRefused(
                f"the lock path must be absolute, got {self._lock_path!r}: a relative path "
                "resolves per working directory"
            )
        deadline = time.monotonic() + self._wait_s
        self._dir_fd = self._open_directory()
        directory = os.fstat(self._dir_fd)
        key = (directory.st_dev, directory.st_ino, self._lock_name)
        self._key = key
        with _GUARD_REGISTRY_LOCK:
            stranded = _STRANDED.get(key)
            thread_lock = _GUARD_THREAD_LOCKS.setdefault(key, threading.Lock())
        if stranded is not None:
            raise AnchorGuardStranded(
                f"the anchor guard for {self._lock_path} is stranded holding "
                f"{', '.join(stranded.held_resources())}; every in-process acquisition "
                "refuses until the process exits (no retry exists)"
            )
        held = _anchor_held_keys()
        if key in held:
            raise AnchorGuardReentrant(
                f"this thread already holds the anchor guard for {self._lock_path}; "
                "it is not reentrant, so this refuses rather than deadlocking"
            )
        self._thread_lock = thread_lock
        self._thread_lock_held = thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if not self._thread_lock_held:
            raise AnchorGuardTimeout(
                f"another thread in this process holds the anchor guard for "
                f"{self._lock_path}; waited {self._wait_s}s"
            )
        self._held_set = held
        held.add(key)
        self._held_key = True
        self._lock_fd = self._open_lock_file(self._dir_fd)
        self._take_flock(deadline)
        self._recheck_identity()

    def _open_directory(self) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(self._dir_path, flags)
        except OSError as error:
            raise AnchorGuardRefused(
                f"cannot open the lock directory {self._dir_path} without following links: {error}"
            ) from error
        try:
            found = os.fstat(fd)
            mode = stat.S_IMODE(found.st_mode)
            if not stat.S_ISDIR(found.st_mode):
                raise AnchorGuardRefused(f"{self._dir_path} is not a directory")
            if found.st_uid != os.geteuid():
                raise AnchorGuardRefused(
                    f"the lock directory {self._dir_path} is owned by uid {found.st_uid}, "
                    f"not this process's effective user {os.geteuid()}; refused as found"
                )
            if mode != _ANCHOR_DIR_MODE:
                raise AnchorGuardRefused(
                    f"the lock directory {self._dir_path} has mode {mode:04o}; it must be "
                    "exactly 0700 (refused as found, never repaired)"
                )
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _open_lock_file(self, dir_fd: int) -> int:
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        created = False
        try:
            fd = os.open(
                self._lock_name, flags | os.O_CREAT | os.O_EXCL, _ANCHOR_LOCK_MODE, dir_fd=dir_fd
            )
            created = True
        except FileExistsError:
            try:
                fd = os.open(self._lock_name, flags, dir_fd=dir_fd)
            except OSError as error:
                raise AnchorGuardRefused(
                    f"cannot open the lock file {self._lock_path} without following links: {error}"
                ) from error
        except OSError as error:
            raise AnchorGuardRefused(
                f"cannot create the lock file {self._lock_path}: {error}"
            ) from error
        try:
            if created:
                os.fchmod(fd, _ANCHOR_LOCK_MODE)
            found = os.fstat(fd)
            mode = stat.S_IMODE(found.st_mode)
            if not stat.S_ISREG(found.st_mode):
                raise AnchorGuardRefused(f"the lock file {self._lock_path} is not a regular file")
            if found.st_uid != os.geteuid():
                raise AnchorGuardRefused(
                    f"the lock file {self._lock_path} is owned by uid {found.st_uid}, not "
                    f"this process's effective user {os.geteuid()}; refused as found"
                )
            if mode != _ANCHOR_LOCK_MODE:
                raise AnchorGuardRefused(
                    f"the lock file {self._lock_path} has mode {mode:04o}; it must be exactly "
                    "0600 (refused as found, never repaired)"
                )
            if found.st_nlink != 1:
                raise AnchorGuardRefused(
                    f"the lock file {self._lock_path} has {found.st_nlink} links; it must "
                    "have exactly one link"
                )
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _take_flock(self, deadline: float) -> None:
        assert self._lock_fd is not None
        while True:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AnchorGuardTimeout(
                        f"another process holds the anchor guard for {self._lock_path}; "
                        f"waited {self._wait_s}s"
                    ) from None
                time.sleep(min(_ANCHOR_FLOCK_POLL_S, remaining))
                continue
            except OSError as error:
                raise AnchorGuardRefused(
                    f"cannot take an advisory lock on {self._lock_path} ({error}); exclusion "
                    "cannot be verified on this filesystem"
                ) from error
            self._flocked = True
            return

    def _recheck_identity(self) -> None:
        assert self._lock_fd is not None and self._dir_fd is not None
        opened = os.fstat(self._lock_fd)
        try:
            named = os.stat(self._lock_name, dir_fd=self._dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            named = None
        if named is None or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise AnchorGuardRefused(
                f"the lock file {self._lock_path} was replaced after it was opened; "
                "refused by identity"
            )
        directory = os.fstat(self._dir_fd)
        try:
            named_dir = os.stat(self._dir_path, follow_symlinks=False)
        except OSError:
            named_dir = None
        if named_dir is None or (named_dir.st_dev, named_dir.st_ino) != (
            directory.st_dev,
            directory.st_ino,
        ):
            raise AnchorGuardRefused(
                f"the lock directory {self._dir_path} was replaced after it was opened; "
                "refused by identity"
            )

    # ------------------------------------------------------------------ release

    def release(self) -> None:
        """Run every release step; derive the state from what is still held.

        Precedence (R12, R13): the most recently delivered asynchronous exception is
        primary; else an unwinding asynchronous exception stays primary; else the first
        ordinary failure in release order is raised as ``AnchorGuardCleanupFailed``; else
        the caller's original failure, if any, propagates unchanged.
        """

        if self.state in ("RELEASED", "INVALID_AFTER_FORK"):
            return
        if self.state == "NEW":
            self.state = "RELEASED"
            return
        if self.state == "RELEASE_INCOMPLETE":
            raise AnchorGuardStranded(
                f"the anchor guard for {self._lock_path} is stranded holding "
                f"{', '.join(self.held_resources())}; release is not retried"
            )
        if self.state == "RELEASING":
            raise AnchorGuardRefused("release() re-entered during its own release")
        self.state = "RELEASING"
        original = sys.exception()
        failures: list[AnchorCleanupFailure] = []

        def failed(name: str, error: BaseException) -> None:
            failures.append(
                AnchorCleanupFailure(
                    _ANCHOR_RELEASE_STEPS.index(name) + 1,
                    name,
                    error,
                    not isinstance(error, Exception),
                )
            )

        if self._flocked and self._lock_fd is not None:
            try:
                _anchor_step_hook("unlock", "pre")
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._flocked = False
                _anchor_step_hook("unlock", "post")
            except BaseException as error:
                failed("unlock", error)
        if self._lock_fd is not None:
            try:
                _anchor_step_hook("close_lock_fd", "pre")
                fd, self._lock_fd = self._lock_fd, None
                try:
                    os.close(fd)
                except OSError:
                    # Linux frees the descriptor even when close reports an error, so the
                    # field stays cleared and the close is never retried.
                    self._flocked = False
                    raise
                except BaseException:
                    self._lock_fd = fd
                    raise
                self._flocked = False
                _anchor_step_hook("close_lock_fd", "post")
            except BaseException as error:
                failed("close_lock_fd", error)
        if self._held_key and self._held_set is not None:
            try:
                _anchor_step_hook("discard_held_key", "pre")
                self._held_set.discard(self._key)
                self._held_key = False
                _anchor_step_hook("discard_held_key", "post")
            except BaseException as error:
                failed("discard_held_key", error)
        if self._thread_lock_held and self._thread_lock is not None:
            try:
                _anchor_step_hook("release_thread_lock", "pre")
                self._thread_lock.release()
                self._thread_lock_held = False
                _anchor_step_hook("release_thread_lock", "post")
            except BaseException as error:
                failed("release_thread_lock", error)
        if self._dir_fd is not None:
            try:
                _anchor_step_hook("close_dir_fd", "pre")
                fd, self._dir_fd = self._dir_fd, None
                try:
                    os.close(fd)
                except OSError:
                    raise
                except BaseException:
                    self._dir_fd = fd
                    raise
                _anchor_step_hook("close_dir_fd", "post")
            except BaseException as error:
                failed("close_dir_fd", error)

        still_held = self.held_resources()
        if still_held:
            self.state = "RELEASE_INCOMPLETE"
            with _GUARD_REGISTRY_LOCK:
                if self._key is not None:
                    _STRANDED[self._key] = self
                else:
                    _STRANDED_UNKEYED.append(self)
        else:
            self.state = "RELEASED"
        record = AnchorCleanupRecord(
            self._lock_path, tuple(failures), original, still_held, self.state
        )
        self.cleanup_record = record
        asynchronous = [failure.error for failure in failures if failure.asynchronous]
        if asynchronous:
            primary = asynchronous[-1]
            _attach_cleanup_record(primary, record)
            raise primary
        if original is not None and not isinstance(original, Exception):
            _attach_cleanup_record(original, record)
            return
        ordinary = [failure.error for failure in failures if not failure.asynchronous]
        if ordinary:
            cleanup_failed = AnchorGuardCleanupFailed(
                f"releasing the anchor guard for {self._lock_path} failed at "
                f"{', '.join(f.name for f in failures)}; cleanup status {self.state}"
                + (f" (still held: {', '.join(still_held)})" if still_held else "")
            )
            _attach_cleanup_record(cleanup_failed, record)
            raise cleanup_failed from ordinary[0]

    def _invalidate_after_fork(self) -> None:
        for fd in (self._lock_fd, self._dir_fd):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self._lock_fd = None
        self._dir_fd = None
        self._flocked = False
        self._thread_lock = None
        self._thread_lock_held = False
        self._held_set = None
        self._held_key = False
        self.state = "INVALID_AFTER_FORK"


def _attach_cleanup_record(error: BaseException, record: AnchorCleanupRecord) -> None:
    # Diagnostic recording must never prevent propagation (R13). Only an ordinary failure
    # of the attribute write is swallowed; a new asynchronous interruption propagates.
    with contextlib.suppress(Exception):
        error.anchor_cleanup_record = record  # type: ignore[attr-defined]


def _reset_anchor_guards_in_forked_child() -> None:
    """A forked child inherits no usable guard: close its descriptor copies, clear state.

    Closing the child's copy of a lock descriptor does not release the parent's flock,
    which the parent's own descriptor still holds.
    """

    global _GUARD_REGISTRY_LOCK, _GUARD_HELD
    for guard in list(_LIVE_GUARDS):
        guard._invalidate_after_fork()
    _GUARD_REGISTRY_LOCK = threading.Lock()
    _GUARD_HELD = threading.local()
    _GUARD_THREAD_LOCKS.clear()
    _STRANDED.clear()
    _STRANDED_UNKEYED.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_anchor_guards_in_forked_child)
# <<< anchor guard (FU1-GUARD-1)
