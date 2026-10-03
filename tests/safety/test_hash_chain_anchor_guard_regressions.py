"""Supporting regression coverage for the unwired ``AnchorGuard`` (FU1-GUARD-1T).

Written by the coverage seat, not by the guard's author. Every test here exists to
kill a mutant the reader found SURVIVING the author's file
(``PREMERGE-FU1-GUARD-1-reader.md`` section 2: m03, m09, m10, m16, m19, m20, m27),
plus one labelled limitation test (P3-4) that documents the spec's L-same-uid
residual as current behaviour. Nothing here touches anchor I/O, the basename
mapping, listeners, sessions, call sites, R11 values or R9: those are OUT.

Harness: the few helpers this file needs are copied from the sibling file rather
than imported, so the two files stay independent.
"""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from chronos.persistence import anchor_guard as guard_module
from chronos.persistence.anchor_guard import AnchorGuard, AnchorGuardRefused, AnchorGuardTimeout

SRC_ROOT = Path(guard_module.__file__).resolve().parents[2]
JOIN_S = 5.0
CHILD_S = 10.0
LOCK_EX_NB = fcntl.LOCK_EX | fcntl.LOCK_NB

# ------------------------------------------------------------------ helpers


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _bounded(fn: Callable[[], object], timeout: float = JOIN_S) -> object:
    """Run ``fn`` in a worker thread; a hang fails the test instead of hanging the suite."""

    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as error:
            box["error"] = error

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    assert not worker.is_alive(), "bounded operation did not finish (deadlock or hang)"
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("value")


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(SRC_ROOT),
        PYTHONDONTWRITEBYTECODE="1",
        BROKER_MODE="demo",
        ALLOW_ORDER_TRANSMIT="false",
        ALLOW_LIVE_TRADING="false",
    )
    return env


def _spawn(code: str, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", code, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_env(),
    )


def _read_line(proc: subprocess.Popen[str], timeout: float = CHILD_S) -> str:
    assert proc.stdout is not None
    stream = proc.stdout
    line: list[str] = []
    reader = threading.Thread(target=lambda: line.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    assert not reader.is_alive() and line, "child did not report within the bound"
    return line[0].strip()


def _finish(proc: subprocess.Popen[str]) -> tuple[int, str]:
    if proc.stdin is not None and proc.stdin.closed:
        proc.stdin = None
    try:
        _, err = proc.communicate(timeout=CHILD_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate(timeout=CHILD_S)
        raise AssertionError("child process did not exit within the bound") from None
    return proc.returncode, err


def _run_child(code: str, *args: str) -> str:
    """Run a one-shot child to completion within the bound; return its first stdout line."""

    proc = _spawn(code, *args)
    try:
        line = _read_line(proc)
    finally:
        code_, err = _finish(proc)
    assert code_ == 0, f"child failed ({code_}): {err}"
    return line


def _spy_flock(monkeypatch: pytest.MonkeyPatch, probe: Callable[[], object] | None = None) -> list:
    """Wrap the real ``fcntl.flock``; record (op, probe()) per call and still take the lock."""

    real = fcntl.flock
    calls: list[tuple[int, object]] = []

    def spy(fd: int, operation: int) -> None:
        calls.append((operation, probe() if probe is not None else None))
        real(fd, operation)

    monkeypatch.setattr(fcntl, "flock", spy)
    return calls


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    return directory


@pytest.fixture
def lock_path(lock_dir: Path) -> Path:
    return lock_dir / "chronos.db.anchor-6175746f6e6f6d79.lock"


@pytest.fixture
def restore_umask() -> Iterator[None]:
    before = os.umask(0o022)
    os.umask(before)
    yield
    os.umask(before)


CHILD_TIMED_HOLDER = """
import sys, time
from pathlib import Path
from chronos.persistence.anchor_guard import AnchorGuard
guard = AnchorGuard(Path(sys.argv[1]), wait_s=float(sys.argv[2]))
guard.acquire()
print("HELD", flush=True)
time.sleep(float(sys.argv[3]))
guard.release()
print("RELEASED", flush=True)
"""

CHILD_CONTENDER = """
import os, sys
from pathlib import Path
from chronos.persistence.anchor_guard import AnchorGuard, AnchorGuardTimeout
path = Path(sys.argv[1])
guard = AnchorGuard(path, wait_s=float(sys.argv[2]))
try:
    guard.acquire()
except AnchorGuardTimeout:
    print("TIMEOUT", flush=True)
else:
    print(f"ACQUIRED {os.stat(path).st_ino}", flush=True)
    guard.release()
"""


# ------------------------------------------------------------------ P3-2a (m03): no clamp


def test_a_one_second_contender_acquires_from_a_holder_that_releases_at_point_six_seconds(
    lock_path: Path,
) -> None:
    """Kills m03 (``self._wait_s = min(normalized_wait_s, 0.25)``).

    A contender that may wait 1.0 s must ACQUIRE from a holder that releases at about
    0.6 s, on BOTH legs of the one deadline (spec section 5): the thread lock (step 5,
    an in-process holder) and the flock (step 8, a child-process holder). A 0.2 s
    contender in each leg is the positive control that times out typed.
    """

    # --- the thread-lock leg: the holder is another thread of this process.
    held = threading.Event()
    release_at = threading.Event()

    def holder() -> None:
        guard = AnchorGuard(lock_path, wait_s=1.0).acquire()
        try:
            held.set()
            release_at.wait(JOIN_S)
        finally:
            guard.release()

    worker = threading.Thread(target=holder, daemon=True)
    release_worker: threading.Thread | None = None
    worker.start()
    try:
        assert held.wait(JOIN_S)
        started = time.monotonic()
        control = AnchorGuard(lock_path, wait_s=0.2)
        try:
            with pytest.raises(AnchorGuardTimeout, match="another thread"):
                _bounded(control.acquire)
        finally:
            control.release()
        assert time.monotonic() - started < 0.6, "the control contender waited past its bound"

        def release_later() -> None:
            time.sleep(0.6)
            release_at.set()

        release_worker = threading.Thread(target=release_later, daemon=True)
        release_worker.start()
        started = time.monotonic()
        contender = _bounded(lambda: AnchorGuard(lock_path, wait_s=1.0).acquire())
        try:
            elapsed = time.monotonic() - started
            assert isinstance(contender, AnchorGuard) and contender.state == "HELD"
            assert 0.3 <= elapsed < 1.0, f"acquired after {elapsed:.3f}s; expected about 0.6s"
        finally:
            assert isinstance(contender, AnchorGuard)
            contender.release()
    finally:
        release_at.set()
        worker.join(JOIN_S)
        if release_worker is not None:
            release_worker.join(JOIN_S)
        assert not worker.is_alive()
        assert release_worker is None or not release_worker.is_alive()

    # --- the flock leg: the holder is a child process that releases after 0.6 s.
    proc = _spawn(CHILD_TIMED_HOLDER, str(lock_path), "1.0", "0.6")
    try:
        assert _read_line(proc) == "HELD"
        started = time.monotonic()
        contender = _bounded(lambda: AnchorGuard(lock_path, wait_s=1.0).acquire())
        try:
            elapsed = time.monotonic() - started
            assert isinstance(contender, AnchorGuard) and contender.state == "HELD"
            assert 0.3 <= elapsed < 1.0, f"acquired after {elapsed:.3f}s; expected about 0.6s"
        finally:
            assert isinstance(contender, AnchorGuard)
            contender.release()
        assert _read_line(proc) == "RELEASED"
    finally:
        code, err = _finish(proc)
    assert code == 0, err

    # --- the flock control: a 0.2 s contender against a 0.6 s holder times out typed.
    proc = _spawn(CHILD_TIMED_HOLDER, str(lock_path), "1.0", "0.6")
    try:
        assert _read_line(proc) == "HELD"
        started = time.monotonic()
        control = AnchorGuard(lock_path, wait_s=0.2)
        try:
            with pytest.raises(AnchorGuardTimeout, match="another process"):
                _bounded(control.acquire)
        finally:
            control.release()
        assert time.monotonic() - started < 0.6
        assert _read_line(proc) == "RELEASED"
    finally:
        code, err = _finish(proc)
    assert code == 0, err


# ------------------------------------------------------------------ P3-3 (m09, m10): O_NOFOLLOW


def test_a_symlink_to_an_existing_0600_lock_file_is_refused_before_any_flock(
    lock_dir: Path, lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kills m09 (the lock-file open without ``O_NOFOLLOW``).

    The author's symlink case points at a MISSING target, which both the real code and
    the mutant refuse. Here the target EXISTS and would pass every as-found check
    (owned, regular, 0600, one link): only ``O_NOFOLLOW`` refuses it, and it must do so
    with ZERO ``flock`` calls. The mutant follows the link, takes the flock, and is only
    caught later by the identity re-check with a different message.
    """

    target = lock_dir / "target.lock"
    os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    os.chmod(target, 0o600)
    lock_path.symlink_to(target)
    target_before = os.stat(target)
    calls = _spy_flock(monkeypatch)
    fds = _fd_count()
    guard = AnchorGuard(lock_path, wait_s=0.2)
    try:
        with pytest.raises(AnchorGuardRefused, match=r"lock file .* without following links"):
            guard.acquire()
    finally:
        guard.release()
    assert calls == [], f"flock was called before the refusal: {calls}"
    assert _fd_count() == fds, "the refusal leaked a descriptor"
    assert stat.S_ISLNK(os.lstat(lock_path).st_mode), "the symlink was replaced"
    target_after = os.stat(target)
    assert (target_after.st_ino, stat.S_IMODE(target_after.st_mode)) == (
        target_before.st_ino,
        0o600,
    ), "the target was touched"


def test_a_symlinked_lock_directory_is_refused_before_any_flock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kills m10 (the directory open without ``O_NOFOLLOW``).

    The author's symlinked-directory case matches only the refusal TYPE, which the
    post-flock identity re-check also raises. This pins the ORDER: the refusal names the
    link, no ``flock`` has happened, no descriptor leaked, and no lock file was created
    behind the link.
    """

    real_dir = tmp_path / "real"
    real_dir.mkdir(mode=0o700)
    os.chmod(real_dir, 0o700)
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(real_dir)
    calls = _spy_flock(monkeypatch)
    fds = _fd_count()
    guard = AnchorGuard(linked_dir / "x.lock", wait_s=0.2)
    try:
        with pytest.raises(AnchorGuardRefused, match=r"lock directory .* without following links"):
            guard.acquire()
    finally:
        guard.release()
    assert calls == [], f"flock was called before the refusal: {calls}"
    assert _fd_count() == fds, "the refusal leaked a descriptor"
    assert sorted(os.listdir(real_dir)) == [], "a lock file was created behind the link"


# ------------------------------------------------------------------ P3-5 (m16): fchmod under umask


@pytest.mark.parametrize("umask", [0o277, 0o377])
def test_a_created_lock_file_is_0600_under_a_restrictive_umask(
    lock_path: Path, umask: int, restore_umask: None
) -> None:
    """Kills m16 (the ``fchmod`` after creation removed).

    Under the default umask 022 the created mode is 0600 with or without ``fchmod``.
    Under 0277 or 0377 the ``O_CREAT`` mode is masked to 0400 or 0200; only ``fchmod``
    makes it 0600, and without it the guard's own exact-mode check refuses the file it
    just created. The umask is restored by the fixture even on failure.
    """

    os.umask(umask)
    guard = AnchorGuard(lock_path, wait_s=0.5).acquire()
    guard.release()
    assert guard.state == "RELEASED"
    assert stat.S_IMODE(os.stat(lock_path).st_mode) == 0o600
    # A second acquisition judges the file as found: it must still be acceptable.
    AnchorGuard(lock_path, wait_s=0.5).acquire().release()


# ------------------------------------------------------------------ P3-1 (m19, m20): ownership


def test_a_lock_directory_owned_by_another_uid_is_refused_as_found_without_root(
    lock_dir: Path, lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kills m20 (the directory ownership check removed) without root.

    The check is ``found.st_uid != os.geteuid()`` on a REAL ``fstat`` of the real
    directory. The filesystem is left untouched; only the guard's view of its own
    identity changes (``os.geteuid`` returns the real uid + 1). Not a tautology: the
    mutant acquires under the same patch, so the outcome depends on the production
    comparison. The refusal must name the DIRECTORY and both uids, happen before any
    ``flock``, leak nothing, and create no lock file; with the real ``geteuid`` the same
    setup acquires (positive control).
    """

    real_geteuid = os.geteuid
    real_uid = real_geteuid()
    foreign_uid = real_uid + 1
    monkeypatch.setattr(os, "geteuid", lambda: foreign_uid)
    calls = _spy_flock(monkeypatch)
    fds = _fd_count()
    guard = AnchorGuard(lock_path, wait_s=0.2)
    try:
        with pytest.raises(AnchorGuardRefused) as info:
            guard.acquire()
    finally:
        guard.release()
    message = str(info.value)
    assert "lock directory" in message and "lock file" not in message, message
    assert f"owned by uid {real_uid}" in message and f"effective user {foreign_uid}" in message
    assert calls == [], f"flock was called before the refusal: {calls}"
    assert _fd_count() == fds, "the refusal leaked a descriptor"
    assert not lock_path.exists(), "a lock file was created before the directory refusal"
    assert stat.S_IMODE(os.stat(lock_dir).st_mode) == 0o700, "the directory was repaired"
    monkeypatch.setattr(os, "geteuid", real_geteuid)
    AnchorGuard(lock_path, wait_s=0.2).acquire().release()


def test_a_lock_file_owned_by_another_uid_is_refused_as_found_without_root(
    lock_dir: Path, lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kills m19 (the lock-file ownership check removed) without root.

    The directory is validated first and would refuse under a foreign identity before
    the lock-file branch is reached, so the fake ``geteuid`` returns the real uid while
    ``_open_directory`` runs and the real uid + 1 afterwards. The lock file EXISTS
    (owned, regular, 0600, one link): only the ownership comparison can refuse it. The
    refusal must name the FILE and both uids, happen before any ``flock``, leak nothing,
    and leave the file as found; with the real ``geteuid`` the same file is acquirable.
    """

    os.close(os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    os.chmod(lock_path, 0o600)
    real_geteuid = os.geteuid
    real_uid = real_geteuid()
    foreign_uid = real_uid + 1
    phase = {"validating_directory": False}
    real_open_directory = AnchorGuard._open_directory

    def open_directory(self: AnchorGuard) -> int:
        phase["validating_directory"] = True
        try:
            return real_open_directory(self)
        finally:
            phase["validating_directory"] = False

    monkeypatch.setattr(AnchorGuard, "_open_directory", open_directory)
    monkeypatch.setattr(
        os, "geteuid", lambda: real_uid if phase["validating_directory"] else foreign_uid
    )
    calls = _spy_flock(monkeypatch)
    fds = _fd_count()
    before = os.stat(lock_path)
    guard = AnchorGuard(lock_path, wait_s=0.2)
    try:
        with pytest.raises(AnchorGuardRefused) as info:
            guard.acquire()
    finally:
        guard.release()
    message = str(info.value)
    assert "lock file" in message and "lock directory" not in message, message
    assert f"owned by uid {real_uid}" in message and f"effective user {foreign_uid}" in message
    assert calls == [], f"flock was called before the refusal: {calls}"
    assert _fd_count() == fds, "the refusal leaked a descriptor"
    after = os.stat(lock_path)
    assert (after.st_ino, stat.S_IMODE(after.st_mode), after.st_nlink) == (
        before.st_ino,
        0o600,
        1,
    ), "the lock file was repaired or replaced"
    monkeypatch.setattr(os, "geteuid", real_geteuid)
    AnchorGuard(lock_path, wait_s=0.2).acquire().release()


# ------------------------------------------------------------------ P3-8 (m27): an ORDER pin


def test_order_pin_the_thread_lock_is_held_before_the_first_flock_call(
    lock_dir: Path, lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ORDER PIN for spec section 2.2, steps 5 (thread lock) before 8 (flock); kills m27.

    Labelled as an order pin, not a safety property: exclusion holds in either order
    (the reader's P3-8). It pins the spec's stated order so a reordering is a visible
    change, not a silent one. At the FIRST ``flock`` call the identity-keyed thread
    lock for this directory must already be held by this guard.
    """

    directory = os.stat(lock_dir)
    key = (directory.st_dev, directory.st_ino, lock_path.name)
    guard = AnchorGuard(lock_path, wait_s=0.5)

    def probe() -> tuple[bool, bool]:
        thread_lock = guard_module._GUARD_THREAD_LOCKS.get(key)
        return (thread_lock is not None and thread_lock.locked(), guard._thread_lock_held)

    calls = _spy_flock(monkeypatch, probe)
    guard.acquire()
    try:
        assert calls and calls[0][0] == LOCK_EX_NB, calls
        assert calls[0][1] == (True, True), (
            "the first flock ran before the thread lock was held (spec 2.2 order 5 -> 8)"
        )
    finally:
        guard.release()
    assert guard.state == "RELEASED"
    assert calls[-1][0] == fcntl.LOCK_UN


# ------------------------------------------------------------------ P3-4: a DISCLOSED LIMITATION


def test_disclosed_limitation_same_uid_unlink_admits_a_child_on_a_new_inode_while_held(
    lock_path: Path,
) -> None:
    """DISCLOSED LIMITATION, asserted as CURRENT behaviour; never presented as protection.

    The spec's L-same-uid residual (``FIX-26-FU1g5.1-guard-spec.md`` sections 1 and 7):
    a same-uid actor can swap inodes between another writer's checks. The reader's
    P3-4 probe, pinned: holder A holds the guard; a child contender TIMES OUT (the
    control); a same-uid ``os.unlink`` of the lock file; the same child code then
    ACQUIRES on a NEW inode while A still reports ``HELD``. The guard also recreates the
    missing lock file at that acquisition. The pre-rename and post-fsync re-checks that
    would let A notice are publication (OUT); nothing here claims they exist.
    """

    holder = AnchorGuard(lock_path, wait_s=0.5).acquire()
    try:
        inode_held = os.stat(lock_path).st_ino
        assert _run_child(CHILD_CONTENDER, str(lock_path), "0.5") == "TIMEOUT"
        os.unlink(lock_path)
        report = _run_child(CHILD_CONTENDER, str(lock_path), "0.5")
        verb, _, inode_text = report.partition(" ")
        assert verb == "ACQUIRED", report
        assert int(inode_text) != inode_held, "the child reused the holder's inode"
        assert holder.state == "HELD", "the holder noticed the unlink (behaviour changed)"
        assert lock_path.exists(), "the child did not recreate the lock file"
        assert os.stat(lock_path).st_ino != inode_held
    finally:
        holder.release()
    assert holder.state == "RELEASED"
