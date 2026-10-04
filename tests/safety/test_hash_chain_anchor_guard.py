"""FU1-GUARD-1: the dedicated per-stream anchor guard, in isolation (spec FU1g5.1 §1-§5).

The guard is new code that nothing calls yet: it is NOT wired into the issuance route or
the drain (P1-NEW-22 stays open), it performs no anchor-file I/O and no URL derivation (the
lock path is injected), and R11 is open, so every test passes an explicit ``wait_s``.

Every wait is bounded and every join and child process has a hard timeout, so a deadlock
fails an assertion instead of hanging the suite.
"""

from __future__ import annotations

import ast
import contextlib
import errno
import fcntl
import inspect
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from chronos.persistence import anchor_guard as guard_module
from chronos.persistence.anchor_guard import (
    AnchorGuard,
    AnchorGuardCleanupFailed,
    AnchorGuardReentrant,
    AnchorGuardRefused,
    AnchorGuardStranded,
    AnchorGuardTimeout,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="the anchor guard is supported on Linux only (G6)"
)

SRC_ROOT = Path(guard_module.__file__).resolve().parents[2]
JOIN_S = 5.0
CHILD_S = 10.0


# ------------------------------------------------------------------ helpers


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _force_unstrand(guard: AnchorGuard) -> None:
    """Test-only cleanup of a deliberately stranded guard (production never does this)."""

    if guard._lock_fd is not None:
        with contextlib.suppress(OSError):
            os.close(guard._lock_fd)
        guard._lock_fd = None
        guard._flocked = False
    if guard._held_key and guard._held_set is not None:
        guard._held_set.discard(guard._key)
        guard._held_key = False
    if guard._thread_lock_held and guard._thread_lock is not None:
        guard._thread_lock.release()
        guard._thread_lock_held = False
    if guard._dir_fd is not None:
        with contextlib.suppress(OSError):
            os.close(guard._dir_fd)
        guard._dir_fd = None


@pytest.fixture(autouse=True)
def _no_stranded_leftovers() -> Iterator[None]:
    yield
    for guard in list(guard_module._STRANDED.values()) + list(guard_module._STRANDED_UNKEYED):
        _force_unstrand(guard)
    guard_module._STRANDED.clear()
    guard_module._STRANDED_UNKEYED.clear()


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    return directory


@pytest.fixture
def lock_path(lock_dir: Path) -> Path:
    return lock_dir / "chronos.db.anchor-6175746f6e6f6d79.lock"


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


def _install_hook(
    monkeypatch: pytest.MonkeyPatch, plan: dict[tuple[str, str], BaseException | Callable[[], None]]
) -> list[tuple[str, str]]:
    """Inject faults at release steps through the test-only seam; return the call log."""

    seen: list[tuple[str, str]] = []

    def hook(step: str, phase: str) -> None:
        seen.append((step, phase))
        action = plan.get((step, phase))
        if action is None:
            return
        if isinstance(action, BaseException):
            raise action
        action()

    monkeypatch.setattr(guard_module, "_ANCHOR_GUARD_STEP_HOOK", hook)
    return seen


def _attempted(seen: list[tuple[str, str]]) -> list[str]:
    return [step for step, phase in seen if phase == "pre"]


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


CHILD_HOLDER = """
import sys
from pathlib import Path
from chronos.persistence.anchor_guard import AnchorGuard
guard = AnchorGuard(Path(sys.argv[1]), wait_s=float(sys.argv[2]))
guard.acquire()
print("HELD", flush=True)
sys.stdin.readline()
guard.release()
print("RELEASED", flush=True)
"""

CHILD_CONTENDER = """
import sys
from pathlib import Path
from chronos.persistence.anchor_guard import AnchorGuard, AnchorGuardTimeout
guard = AnchorGuard(Path(sys.argv[1]), wait_s=float(sys.argv[2]))
try:
    guard.acquire()
except AnchorGuardTimeout:
    print("TIMEOUT", flush=True)
else:
    print("ACQUIRED", flush=True)
    guard.release()
"""

CHILD_JOURNAL_LOOP = """
import os, sys, time
from pathlib import Path
from chronos.persistence.anchor_guard import AnchorGuard
lock, journal, rounds = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
fd = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
for _ in range(rounds):
    with AnchorGuard(lock, wait_s=5.0):
        os.write(fd, f"enter {os.getpid()}\\n".encode())
        time.sleep(0.05)
        os.write(fd, f"exit {os.getpid()}\\n".encode())
os.close(fd)
print("DONE", flush=True)
"""


def _assert_journal_never_overlaps(lines: list[str]) -> None:
    holder: str | None = None
    for line in lines:
        event, who = line.split()
        if event == "enter":
            assert holder is None, f"two concurrent holders: {holder} and {who}"
            holder = who
        else:
            assert holder == who, f"exit by {who} while {holder} held"
            holder = None
    assert holder is None


# ------------------------------------------------------------------ Muse (a): T1


def test_two_successive_writes_in_one_process_do_not_retain_the_first_guard(
    lock_path: Path,
) -> None:
    baseline = _fd_count()

    def section() -> str:
        with AnchorGuard(lock_path, wait_s=1.0) as guard:
            assert guard.state == "HELD"
        return guard.state

    assert _bounded(section) == "RELEASED"
    assert _fd_count() == baseline
    assert _bounded(section) == "RELEASED", "the second section deadlocked or was refused"
    assert _fd_count() == baseline
    assert lock_path.is_file()


# ------------------------------------------------------------------ Muse (b): T2, T2b, T3, T32


def test_competing_threads_cannot_write_concurrently(lock_path: Path) -> None:
    held, go = threading.Event(), threading.Event()

    def holder() -> None:
        with AnchorGuard(lock_path, wait_s=1.0):
            held.set()
            assert go.wait(JOIN_S)

    worker = threading.Thread(target=holder, daemon=True)
    worker.start()
    assert held.wait(JOIN_S)
    started = time.monotonic()
    with pytest.raises(AnchorGuardTimeout):
        _bounded(lambda: AnchorGuard(lock_path, wait_s=0.3).acquire())
    elapsed = time.monotonic() - started
    assert 0.25 <= elapsed < 0.3 + 2.0
    go.set()
    worker.join(JOIN_S)
    assert not worker.is_alive()
    _bounded(lambda: AnchorGuard(lock_path, wait_s=2.0).acquire().release())


def _thread_journal(lock_path: Path, rounds: int) -> list[str]:
    journal: list[str] = []
    barrier = threading.Barrier(2, timeout=JOIN_S)

    def loop(name: str) -> None:
        barrier.wait()
        for _ in range(rounds):
            with AnchorGuard(lock_path, wait_s=5.0):
                journal.append(f"enter {name}")
                time.sleep(0.005)
                journal.append(f"exit {name}")

    workers = [threading.Thread(target=loop, args=(n,), daemon=True) for n in ("a", "b")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(JOIN_S * 2)
        assert not worker.is_alive()
    return journal


def test_threads_exclude_each_other_over_many_rounds(lock_path: Path) -> None:
    journal = _thread_journal(lock_path, rounds=25)
    assert len(journal) == 100
    _assert_journal_never_overlaps(journal)


def test_the_thread_lock_alone_serializes_threads(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2b: with flock neutralised (test only), the identity-keyed thread lock still excludes."""

    real_flock = fcntl.flock

    def no_op_exclusive(fd: int, operation: int) -> None:
        if operation & fcntl.LOCK_EX:
            return None
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", no_op_exclusive)
    journal = _thread_journal(lock_path, rounds=15)
    _assert_journal_never_overlaps(journal)


def test_separate_processes_cannot_write_concurrently(lock_path: Path) -> None:
    child = _spawn(CHILD_HOLDER, str(lock_path), "2.0")
    try:
        assert _read_line(child) == "HELD"
        started = time.monotonic()
        with pytest.raises(AnchorGuardTimeout):
            _bounded(lambda: AnchorGuard(lock_path, wait_s=0.3).acquire())
        assert time.monotonic() - started < 0.3 + 2.0
        assert child.stdin is not None
        child.stdin.write("go\n")
        child.stdin.flush()
        assert _read_line(child) == "RELEASED"
    finally:
        code, err = _finish(child)
    assert code == 0, err
    _bounded(lambda: AnchorGuard(lock_path, wait_s=2.0).acquire().release())


def test_exclusion_holds_between_threads_and_processes_while_the_anchor_is_replaced(
    lock_path: Path, lock_dir: Path
) -> None:
    """T32 / Muse (b): a sibling anchor-path file is replaced throughout the contention.

    Two child processes and one in-process thread contend on the lock; every holder
    journals entry and exit with a 50 ms critical section, so overlapping holders would
    interleave. Meanwhile the sibling file is replaced by rename 50+ times. The lock's
    identity (st_dev, st_ino) must not change, and the journal must never overlap.
    """

    AnchorGuard(lock_path, wait_s=1.0).acquire().release()
    identity = (os.stat(lock_path).st_dev, os.stat(lock_path).st_ino)
    journal = lock_dir.parent / "journal.log"
    sibling = lock_dir / "chronos.db.anchor-6175746f6e6f6d79.head.json"
    stop = threading.Event()
    replaced = [0]

    def replace_sibling() -> None:
        while not stop.is_set():
            temp = lock_dir / f"chronos.db.anchor-6175746f6e6f6d79.tmp-{replaced[0]:016x}"
            temp.write_text(f'{{"n": {replaced[0]}}}')
            os.rename(temp, sibling)
            replaced[0] += 1
            time.sleep(0.002)

    def thread_contender() -> None:
        fd = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            for _ in range(4):
                with AnchorGuard(lock_path, wait_s=5.0):
                    os.write(fd, f"enter thread-{os.getpid()}\n".encode())
                    time.sleep(0.05)
                    os.write(fd, f"exit thread-{os.getpid()}\n".encode())
        finally:
            os.close(fd)

    replacer = threading.Thread(target=replace_sibling, daemon=True)
    replacer.start()
    children = [_spawn(CHILD_JOURNAL_LOOP, str(lock_path), str(journal), "4") for _ in range(2)]
    contender = threading.Thread(target=thread_contender, daemon=True)
    contender.start()
    try:
        for child in children:
            assert _read_line(child, timeout=20.0) == "DONE"
        contender.join(20.0)
        assert not contender.is_alive()
    finally:
        stop.set()
        replacer.join(JOIN_S)
        for child in children:
            code, err = _finish(child)
            assert code == 0, err
    assert replaced[0] >= 50, f"the sibling file was replaced only {replaced[0]} times"
    assert (os.stat(lock_path).st_dev, os.stat(lock_path).st_ino) == identity
    lines = journal.read_text().splitlines()
    assert len(lines) == 2 * 4 * 3
    _assert_journal_never_overlaps(lines)


# ------------------------------------------------------------------ Muse (c): T4, T5, T8


def test_a_failed_acquisition_leaves_a_later_acquisition_possible(
    lock_path: Path, lock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _fd_count()
    # (i) a timeout
    held, go = threading.Event(), threading.Event()

    def holder() -> None:
        with AnchorGuard(lock_path, wait_s=1.0):
            held.set()
            assert go.wait(JOIN_S)

    worker = threading.Thread(target=holder, daemon=True)
    worker.start()
    assert held.wait(JOIN_S)
    timed_out = AnchorGuard(lock_path, wait_s=0.2)
    with pytest.raises(AnchorGuardTimeout):
        _bounded(timed_out.acquire)
    assert timed_out.state == "RELEASED"
    assert timed_out.held_resources() == ()
    go.set()
    worker.join(JOIN_S)
    assert _fd_count() == baseline
    # (ii) a validation refusal (loose directory mode), then the fixed directory acquires
    os.chmod(lock_dir, 0o755)
    with pytest.raises(AnchorGuardRefused, match="0755"):
        AnchorGuard(lock_path, wait_s=0.2).acquire()
    os.chmod(lock_dir, 0o700)
    # (iii) the platform predicate refuses, then a later acquisition is possible
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(AnchorGuardRefused, match="Linux only"):
        AnchorGuard(lock_path, wait_s=0.2).acquire()
    monkeypatch.setattr(sys, "platform", "linux")
    assert _fd_count() == baseline
    assert lock_path not in [g._lock_path for g in guard_module._STRANDED.values()]
    _bounded(lambda: AnchorGuard(lock_path, wait_s=0.2).acquire().release())
    assert _fd_count() == baseline


def test_a_failed_write_leaves_a_later_acquisition_possible(lock_path: Path) -> None:
    baseline = _fd_count()
    failure = ValueError("the guarded write failed")
    with pytest.raises(ValueError) as caught, AnchorGuard(lock_path, wait_s=1.0):
        raise failure
    assert caught.value is failure, "the original failure must propagate unchanged"
    assert _fd_count() == baseline
    _bounded(lambda: AnchorGuard(lock_path, wait_s=0.5).acquire().release())


@pytest.mark.parametrize("fault", ["lock_path_is_a_directory", "flock_unsupported", "timeout"])
def test_partial_acquisition_unwinds_at_every_failure_point(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    baseline = _fd_count()
    if fault == "lock_path_is_a_directory":
        lock_path.mkdir(mode=0o700)
        expected: type[BaseException] = AnchorGuardRefused
    elif fault == "flock_unsupported":
        real_flock = fcntl.flock

        def unsupported(fd: int, operation: int) -> None:
            if operation & fcntl.LOCK_EX:
                raise OSError(errno.ENOLCK, "No locks available")
            return real_flock(fd, operation)

        monkeypatch.setattr(fcntl, "flock", unsupported)
        expected = AnchorGuardRefused
    else:
        real_flock = fcntl.flock

        def busy(fd: int, operation: int) -> None:
            if operation & fcntl.LOCK_EX:
                raise BlockingIOError(errno.EWOULDBLOCK, "busy")
            return real_flock(fd, operation)

        monkeypatch.setattr(fcntl, "flock", busy)
        expected = AnchorGuardTimeout
    guard = AnchorGuard(lock_path, wait_s=0.2)
    with pytest.raises(expected):
        _bounded(guard.acquire)
    assert guard.state == "RELEASED"
    assert guard.held_resources() == ()
    assert _fd_count() == baseline
    assert not guard_module._STRANDED
    monkeypatch.undo()
    if fault != "lock_path_is_a_directory":
        _bounded(lambda: AnchorGuard(lock_path, wait_s=0.5).acquire().release())


# ------------------------------------------------------------------ Muse (d): T6, interruption


def test_process_termination_releases_exclusion(lock_path: Path) -> None:
    child = _spawn(CHILD_HOLDER, str(lock_path), "2.0")
    try:
        assert _read_line(child) == "HELD"
        with pytest.raises(AnchorGuardTimeout):
            _bounded(lambda: AnchorGuard(lock_path, wait_s=0.3).acquire())
        os.kill(child.pid, signal.SIGKILL)
    finally:
        code, _ = _finish(child)
    assert code == -signal.SIGKILL
    _bounded(lambda: AnchorGuard(lock_path, wait_s=2.0).acquire().release())


def test_an_interrupted_guarded_section_propagates_the_interrupt_and_releases(
    lock_path: Path,
) -> None:
    baseline = _fd_count()
    interrupt = KeyboardInterrupt("interrupt inside the guarded write")
    with pytest.raises(KeyboardInterrupt) as caught, AnchorGuard(lock_path, wait_s=1.0) as guard:
        raise interrupt
    assert caught.value is interrupt
    assert guard.state == "RELEASED"
    assert _fd_count() == baseline
    _bounded(lambda: AnchorGuard(lock_path, wait_s=0.5).acquire().release())


def test_an_interrupted_release_leaves_a_refused_state_never_a_clean_one(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_hook(monkeypatch, {("release_thread_lock", "pre"): KeyboardInterrupt("stop")})
    with pytest.raises(KeyboardInterrupt), AnchorGuard(lock_path, wait_s=1.0) as guard:
        pass
    assert guard.state == "RELEASE_INCOMPLETE"
    monkeypatch.setattr(guard_module, "_ANCHOR_GUARD_STEP_HOOK", None)
    with pytest.raises(AnchorGuardStranded, match="thread lock"):
        AnchorGuard(lock_path, wait_s=1.0).acquire()


# ------------------------------------------------------------------ Muse (e): bounded waits, T13


def test_contention_returns_the_typed_failure_within_a_bound_and_never_admits_a_second_holder(
    lock_path: Path,
) -> None:
    inside = [0]
    overlap = [False]
    held, go = threading.Event(), threading.Event()

    def holder() -> None:
        with AnchorGuard(lock_path, wait_s=1.0):
            inside[0] += 1
            held.set()
            assert go.wait(JOIN_S)
            inside[0] -= 1

    worker = threading.Thread(target=holder, daemon=True)
    worker.start()
    assert held.wait(JOIN_S)
    for wait_s in (0.0, 0.05, 0.2):
        started = time.monotonic()
        with pytest.raises(AnchorGuardTimeout):
            guard = AnchorGuard(lock_path, wait_s=wait_s)
            _bounded(guard.acquire)
            overlap[0] = inside[0] > 0  # pragma: no cover - only reached if exclusion failed
        elapsed = time.monotonic() - started
        assert elapsed >= wait_s * 0.9
        assert elapsed < wait_s + 1.5
    assert not overlap[0]
    go.set()
    worker.join(JOIN_S)
    assert not worker.is_alive()


def test_a_bounded_wait_deadlock_fails_instead_of_hanging(lock_dir: Path) -> None:
    lock_x = lock_dir / "chronos.db.anchor-78.lock"
    lock_y = lock_dir / "chronos.db.anchor-79.lock"
    barrier = threading.Barrier(2, timeout=JOIN_S)
    outcomes: list[str] = []

    def inverted(first: Path, second: Path) -> None:
        with AnchorGuard(first, wait_s=0.5):
            barrier.wait()
            try:
                AnchorGuard(second, wait_s=0.5).acquire().release()
                outcomes.append("acquired")
            except AnchorGuardTimeout:
                outcomes.append("timeout")
            barrier.wait()

    workers = [
        threading.Thread(target=inverted, args=(lock_x, lock_y), daemon=True),
        threading.Thread(target=inverted, args=(lock_y, lock_x), daemon=True),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(JOIN_S)
        assert not worker.is_alive(), "the inverted order hung instead of refusing"
    assert outcomes.count("timeout") == 2


def test_two_spellings_of_one_directory_share_one_thread_lock_key(
    lock_path: Path, lock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_flock = fcntl.flock

    def no_op_exclusive(fd: int, operation: int) -> None:
        if operation & fcntl.LOCK_EX:
            return None
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", no_op_exclusive)
    alias = lock_dir / ".." / lock_dir.name / lock_path.name
    held, go = threading.Event(), threading.Event()

    def holder() -> None:
        with AnchorGuard(lock_path, wait_s=1.0):
            held.set()
            assert go.wait(JOIN_S)

    worker = threading.Thread(target=holder, daemon=True)
    worker.start()
    assert held.wait(JOIN_S)
    with pytest.raises(AnchorGuardTimeout):
        _bounded(lambda: AnchorGuard(alias, wait_s=0.2).acquire())
    go.set()
    worker.join(JOIN_S)
    assert not worker.is_alive()


def test_a_second_acquisition_on_the_same_thread_refuses_instead_of_deadlocking(
    lock_path: Path,
) -> None:
    def nested() -> None:
        with AnchorGuard(lock_path, wait_s=0.5):
            AnchorGuard(lock_path, wait_s=0.5).acquire()

    with pytest.raises(AnchorGuardReentrant):
        _bounded(nested)


# ------------------------------------------------------------------ identity: T10, T11


@pytest.mark.parametrize("replaced", ["lock_file", "directory"])
def test_a_replaced_lock_file_or_directory_is_refused_by_identity(
    lock_path: Path, lock_dir: Path, monkeypatch: pytest.MonkeyPatch, replaced: str
) -> None:
    AnchorGuard(lock_path, wait_s=0.5).acquire().release()
    real_flock = fcntl.flock
    done = [False]

    def replace_then_lock(fd: int, operation: int) -> None:
        if operation & fcntl.LOCK_EX and not done[0]:
            done[0] = True
            if replaced == "lock_file":
                os.rename(lock_path, lock_dir / "displaced.lock")
                fresh = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fresh)
            else:
                os.rename(lock_dir, lock_dir.parent / "displaced-dir")
                lock_dir.mkdir(mode=0o700)
                os.chmod(lock_dir, 0o700)
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", replace_then_lock)
    baseline = _fd_count()
    guard = AnchorGuard(lock_path, wait_s=0.5)
    with pytest.raises(AnchorGuardRefused, match="replaced"):
        guard.acquire()
    assert guard.state == "RELEASED"
    assert _fd_count() == baseline
    monkeypatch.undo()
    _bounded(lambda: AnchorGuard(lock_path, wait_s=0.5).acquire().release())


def _guard_block() -> str:
    source = Path(guard_module.__file__).read_text()
    start = source.index("# >>> anchor guard (FU1-GUARD-1)")
    end = source.index("# <<< anchor guard (FU1-GUARD-1)")
    return source[start:end]


def test_the_lock_file_is_never_moved_truncated_or_deleted(lock_path: Path, lock_dir: Path) -> None:
    block = _guard_block()
    tree = ast.parse(block)
    forbidden = {"unlink", "remove", "rename", "renames", "replace", "truncate", "ftruncate"}
    forbidden |= {"rmdir", "chmod", "removedirs"}
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (calls & forbidden), calls & forbidden
    AnchorGuard(lock_path, wait_s=0.5).acquire().release()
    identity = os.stat(lock_path).st_ino
    for index in range(20):
        sibling = lock_dir / f"sibling-{index}.tmp"
        sibling.write_text("x")
        os.rename(sibling, lock_dir / "sibling.head.json")
        AnchorGuard(lock_path, wait_s=0.5).acquire().release()
        assert os.stat(lock_path).st_ino == identity


# ------------------------------------------------------------------ release & precedence: T9, T16


def test_release_is_idempotent_and_survives_failing_unlock_and_close(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _fd_count()
    guard = AnchorGuard(lock_path, wait_s=0.5).acquire()
    guard.release()
    guard.release()
    assert guard.state == "RELEASED"
    # an ordinary OSError at LOCK_UN: the close supersedes it; cleanup failure is primary
    unlock_error = OSError(errno.EIO, "LOCK_UN failed")
    _install_hook(monkeypatch, {("unlock", "pre"): unlock_error})
    guard = AnchorGuard(lock_path, wait_s=0.5).acquire()
    with pytest.raises(AnchorGuardCleanupFailed) as caught:
        guard.release()
    assert caught.value.__cause__ is unlock_error
    assert guard.state == "RELEASED"
    assert _fd_count() == baseline
    # a close that reports an error: Linux freed the descriptor; it is never retried
    holder: dict[str, AnchorGuard] = {}
    _install_hook(monkeypatch, {("close_lock_fd", "pre"): lambda: os.close(holder["g"]._lock_fd)})
    holder["g"] = AnchorGuard(lock_path, wait_s=0.5).acquire()
    with pytest.raises(AnchorGuardCleanupFailed) as caught:
        holder["g"].release()
    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.__cause__.errno == errno.EBADF
    assert holder["g"].state == "RELEASED"
    assert _fd_count() == baseline
    # an interrupt at the held-key step leaves it stranded; there is no retry
    monkeypatch.setattr(guard_module, "_ANCHOR_GUARD_STEP_HOOK", None)
    _install_hook(monkeypatch, {("discard_held_key", "pre"): KeyboardInterrupt("held-key")})
    stranded = AnchorGuard(lock_path, wait_s=0.5).acquire()
    with pytest.raises(KeyboardInterrupt):
        stranded.release()
    assert stranded.state == "RELEASE_INCOMPLETE"
    with pytest.raises(AnchorGuardStranded):
        stranded.release()
    with pytest.raises(AnchorGuardStranded, match="held-key"):
        AnchorGuard(lock_path, wait_s=0.5).acquire()


STEPS = ["unlock", "close_lock_fd", "discard_held_key", "release_thread_lock", "close_dir_fd"]


def test_operation_failure_with_successful_cleanup_propagates_unchanged(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T16a."""

    seen = _install_hook(monkeypatch, {})
    failure = ValueError("operation failed")
    with pytest.raises(ValueError) as caught, AnchorGuard(lock_path, wait_s=0.5):
        raise failure
    assert caught.value is failure
    assert _attempted(seen) == STEPS


def test_successful_operation_with_failed_cleanup_raises_the_cleanup_failure(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T16b: no ordinary success after a cleanup failure; later steps still run."""

    error = OSError(errno.EIO, "unlock failed")
    seen = _install_hook(monkeypatch, {("unlock", "pre"): error})
    with (
        pytest.raises(AnchorGuardCleanupFailed) as caught,
        AnchorGuard(lock_path, wait_s=0.5) as guard,
    ):
        pass
    assert caught.value.__cause__ is error
    assert _attempted(seen) == STEPS
    assert guard.state == "RELEASED"
    record = caught.value.anchor_cleanup_record
    assert [f.name for f in record.failures] == ["unlock"]
    assert record.original is None


def test_a_cleanup_oserror_becomes_primary_with_the_original_as_context(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T16c (R12 inverts r2's 'stays primary')."""

    error = OSError(errno.EIO, "unlock failed")
    _install_hook(monkeypatch, {("unlock", "pre"): error})
    failure = ValueError("operation failed")
    with pytest.raises(AnchorGuardCleanupFailed) as caught, AnchorGuard(lock_path, wait_s=0.5):
        raise failure
    assert caught.value.__cause__ is error
    assert caught.value.__context__ is failure
    assert caught.value.anchor_cleanup_record.original is failure


def test_multiple_cleanup_failures_keep_the_first_in_release_order_primary(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T16d: the first ordinary failure in release order is primary; later ones retained."""

    first = OSError(errno.EIO, "unlock failed")
    later = RuntimeError("thread lock release failed")
    seen = _install_hook(
        monkeypatch, {("unlock", "pre"): first, ("release_thread_lock", "pre"): later}
    )
    with (
        pytest.raises(AnchorGuardCleanupFailed) as caught,
        AnchorGuard(lock_path, wait_s=0.5) as guard,
    ):
        pass
    assert caught.value.__cause__ is first
    record = caught.value.anchor_cleanup_record
    assert [f.error for f in record.failures] == [first, later]
    assert _attempted(seen) == STEPS
    assert guard.state == "RELEASE_INCOMPLETE"
    assert guard.held_resources() == ("thread lock",)


# pre-effect: what is still held after an interrupt at each step (the §3.2 oracle)
PRE_EFFECT_HELD = {
    "unlock": (),
    "close_lock_fd": ("lock_fd",),
    "discard_held_key": ("held-key",),
    "release_thread_lock": ("thread lock",),
    "close_dir_fd": ("dir_fd",),
}


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("phase", ["pre", "post"])
@pytest.mark.parametrize("step", STEPS)
def test_an_async_interrupt_at_each_release_step_is_primary_and_state_is_what_is_held(
    lock_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    phase: str,
    interrupt: type[BaseException],
) -> None:
    """T16e/T17/T18: no clean-state claim after an interrupt; every later step still runs."""

    signal_object = interrupt("injected")
    seen = _install_hook(monkeypatch, {(step, phase): signal_object})
    guard = AnchorGuard(lock_path, wait_s=0.5).acquire()
    with pytest.raises(interrupt) as caught:
        guard.release()
    assert caught.value is signal_object
    assert caught.value.anchor_cleanup_record.state == guard.state
    later = STEPS[STEPS.index(step) + 1 :]
    assert all(name in _attempted(seen) for name in later), "a later step was skipped"
    expected = PRE_EFFECT_HELD[step] if phase == "pre" else ()
    assert guard.held_resources() == expected
    monkeypatch.setattr(guard_module, "_ANCHOR_GUARD_STEP_HOOK", None)
    if expected:
        assert guard.state == "RELEASE_INCOMPLETE"
        started = time.monotonic()
        with pytest.raises(AnchorGuardStranded):
            AnchorGuard(lock_path, wait_s=2.0).acquire()
        assert time.monotonic() - started < 1.0, "the stranded refusal waited"
    else:
        assert guard.state == "RELEASED"
        _bounded(lambda: AnchorGuard(lock_path, wait_s=0.5).acquire().release())


def test_an_ordinary_failure_then_an_interrupt_makes_the_interrupt_primary(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T16f (R13 over R12)."""

    ordinary = OSError(errno.EIO, "unlock failed")
    interrupt = KeyboardInterrupt("later interrupt")
    _install_hook(
        monkeypatch, {("unlock", "pre"): ordinary, ("release_thread_lock", "pre"): interrupt}
    )
    guard = AnchorGuard(lock_path, wait_s=0.5).acquire()
    with pytest.raises(KeyboardInterrupt) as caught:
        guard.release()
    assert caught.value is interrupt
    assert ordinary in [f.error for f in caught.value.anchor_cleanup_record.failures]


def test_two_async_interruptions_in_one_release_make_the_second_primary(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T36a (P1-NEW-17): the most recently delivered asynchronous exception is primary."""

    first = KeyboardInterrupt("first async")
    second = SystemExit("second async")
    seen = _install_hook(
        monkeypatch,
        {("close_lock_fd", "pre"): first, ("release_thread_lock", "pre"): second},
    )
    guard = AnchorGuard(lock_path, wait_s=0.5).acquire()
    with pytest.raises(SystemExit) as caught:
        guard.release()
    assert caught.value is second
    assert type(caught.value) is SystemExit
    assert first in [f.error for f in caught.value.anchor_cleanup_record.failures]
    assert "discard_held_key" in _attempted(seen)
    assert "close_dir_fd" in _attempted(seen)
    assert guard.held_resources() == ("lock_fd", "thread lock")
    monkeypatch.setattr(guard_module, "_ANCHOR_GUARD_STEP_HOOK", None)
    with pytest.raises(AnchorGuardStranded):
        AnchorGuard(lock_path, wait_s=0.5).acquire()


def test_an_unwinding_interrupt_stays_primary_over_an_ordinary_cleanup_failure(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R13: an asynchronous interruption remains primary over ordinary failures."""

    ordinary = OSError(errno.EIO, "unlock failed")
    _install_hook(monkeypatch, {("unlock", "pre"): ordinary})
    interrupt = KeyboardInterrupt("inside the section")
    with pytest.raises(KeyboardInterrupt) as caught, AnchorGuard(lock_path, wait_s=0.5):
        raise interrupt
    assert caught.value is interrupt
    assert ordinary in [f.error for f in caught.value.anchor_cleanup_record.failures]


# ------------------------------------------------------------------ stranded: T26-T29


def test_a_stranded_guard_refuses_same_process_reuse_immediately(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T26: another thread refuses without waiting for wait_s; nothing is acquired."""

    _install_hook(monkeypatch, {("release_thread_lock", "pre"): SystemExit("stranding")})
    guard = AnchorGuard(lock_path, wait_s=0.5).acquire()
    with pytest.raises(SystemExit):
        guard.release()
    monkeypatch.setattr(guard_module, "_ANCHOR_GUARD_STEP_HOOK", None)
    baseline = _fd_count()
    started = time.monotonic()
    with pytest.raises(AnchorGuardStranded, match="thread lock"):
        _bounded(lambda: AnchorGuard(lock_path, wait_s=2.0).acquire())
    assert time.monotonic() - started < 1.0
    assert _fd_count() == baseline


def test_a_stranded_held_key_refuses_the_same_thread_immediately(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T27 (r4): the stranded lookup at step 3 precedes the re-entrancy check."""

    _install_hook(monkeypatch, {("discard_held_key", "pre"): KeyboardInterrupt("held-key")})
    guard = AnchorGuard(lock_path, wait_s=0.5).acquire()
    with pytest.raises(KeyboardInterrupt):
        guard.release()
    monkeypatch.setattr(guard_module, "_ANCHOR_GUARD_STEP_HOOK", None)
    started = time.monotonic()
    with pytest.raises(AnchorGuardStranded):
        AnchorGuard(lock_path, wait_s=2.0).acquire()
    assert time.monotonic() - started < 1.0


CHILD_STRAND_HELD_FLOCK = """
import sys
from pathlib import Path
from chronos.persistence import anchor_guard as guard_module
from chronos.persistence.anchor_guard import AnchorGuard
def hook(step, phase):
    if phase == "pre" and step in ("unlock", "close_lock_fd"):
        raise KeyboardInterrupt(step)
guard_module._ANCHOR_GUARD_STEP_HOOK = hook
guard = AnchorGuard(Path(sys.argv[1]), wait_s=1.0).acquire()
try:
    guard.release()
except KeyboardInterrupt:
    pass
assert guard.held_resources()[0] == "lock_fd (flock HELD)", guard.held_resources()
print("STRANDED", flush=True)
sys.stdin.readline()
"""


def test_a_stranded_flock_bounds_other_processes_until_the_owner_exits(lock_path: Path) -> None:
    """T28: two failures strand a HELD flock; other processes refuse until the owner exits."""

    child = _spawn(CHILD_STRAND_HELD_FLOCK, str(lock_path))
    try:
        assert _read_line(child) == "STRANDED"
        with pytest.raises(AnchorGuardTimeout):
            _bounded(lambda: AnchorGuard(lock_path, wait_s=0.3).acquire())
        assert child.stdin is not None
        child.stdin.close()
    finally:
        code, err = _finish(child)
    assert code == 0, err
    _bounded(lambda: AnchorGuard(lock_path, wait_s=2.0).acquire().release())


@pytest.mark.parametrize("step", ["close_lock_fd", "close_dir_fd"])
def test_a_stranded_descriptor_without_a_lock_is_a_leak_and_an_in_process_refusal(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """T29a/T29b (r4): other processes acquire; in-process admissions refuse; fd +1."""

    baseline = _fd_count()
    _install_hook(monkeypatch, {(step, "pre"): KeyboardInterrupt(step)})
    guard = AnchorGuard(lock_path, wait_s=0.5).acquire()
    with pytest.raises(KeyboardInterrupt):
        guard.release()
    monkeypatch.setattr(guard_module, "_ANCHOR_GUARD_STEP_HOOK", None)
    assert _fd_count() == baseline + 1
    with pytest.raises(AnchorGuardStranded):
        AnchorGuard(lock_path, wait_s=0.5).acquire()
    contender = _spawn(CHILD_CONTENDER, str(lock_path), "0.3")
    try:
        assert _read_line(contender) == "ACQUIRED"
    finally:
        code, err = _finish(contender)
    assert code == 0, err


# ---------------------------------------------- acquisition interrupts: T33, T20


@pytest.mark.parametrize("when", ["before", "after"])
def test_an_interrupt_at_the_flock_boundary_is_primary_and_leaves_nothing_held(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    """T33 (acquisition boundary, guard-only half)."""

    baseline = _fd_count()
    real_flock = fcntl.flock
    interrupt = KeyboardInterrupt(f"{when} flock")

    def interrupted(fd: int, operation: int) -> None:
        if operation & fcntl.LOCK_EX:
            if when == "before":
                raise interrupt
            real_flock(fd, operation)
            raise interrupt
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", interrupted)
    guard = AnchorGuard(lock_path, wait_s=0.5)
    with pytest.raises(KeyboardInterrupt) as caught:
        guard.acquire()
    assert caught.value is interrupt
    assert guard.state == "RELEASED"
    assert _fd_count() == baseline
    monkeypatch.undo()
    _bounded(lambda: AnchorGuard(lock_path, wait_s=0.5).acquire().release())


def test_async_interrupt_in_the_return_to_record_window_is_contained_to_availability(
    lock_path: Path, lock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T20 (a DOCUMENTED LIMITATION, §4): an interrupt after os.open returns the directory
    descriptor and before it is recorded leaks that descriptor (+1); nothing is locked."""

    baseline = _fd_count()
    real_open = os.open
    leaked: list[int] = []

    def open_then_interrupt(path: object, flags: int, *args: object, **kwargs: object) -> int:
        fd = real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        if flags & os.O_DIRECTORY and Path(str(path)) == lock_dir:
            leaked.append(fd)
            raise KeyboardInterrupt("after os.open returned")
        return fd

    monkeypatch.setattr(os, "open", open_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        AnchorGuard(lock_path, wait_s=0.5).acquire()
    monkeypatch.undo()
    assert _fd_count() == baseline + 1, "the documented leak did not occur as described"
    os.close(leaked[0])
    _bounded(lambda: AnchorGuard(lock_path, wait_s=0.5).acquire().release())


# ------------------------------------------------------------------ fork: T19


CHILD_FORK = """
import os, sys
from pathlib import Path
from chronos.persistence import anchor_guard as guard_module
from chronos.persistence.anchor_guard import AnchorGuard, AnchorGuardTimeout
guard = AnchorGuard(Path(sys.argv[1]), wait_s=1.0).acquire()
lock_fd, dir_fd = guard._lock_fd, guard._dir_fd
pid = os.fork()
if pid == 0:
    code = 0
    for fd in (lock_fd, dir_fd):
        try:
            os.fstat(fd)
            code = 11
        except OSError:
            pass
    if guard.state != "INVALID_AFTER_FORK" or guard.held_resources():
        code = 12
    if guard_module._STRANDED or guard_module._GUARD_THREAD_LOCKS:
        code = 13
    try:
        AnchorGuard(Path(sys.argv[1]), wait_s=0.2).acquire()
        code = 14
    except AnchorGuardTimeout:
        pass
    os._exit(code)
_, status = os.waitpid(pid, 0)
print(f"CHILD {os.waitstatus_to_exitcode(status)}", flush=True)
print("STILL", "HELD" if guard.state == "HELD" else guard.state, flush=True)
sys.stdin.readline()
guard.release()
"""


def test_a_forked_child_does_not_inherit_a_usable_guard(lock_path: Path) -> None:
    proc = _spawn(CHILD_FORK, str(lock_path))
    try:
        assert _read_line(proc) == "CHILD 0"
        assert _read_line(proc) == "STILL HELD"
        with pytest.raises(AnchorGuardTimeout):
            _bounded(lambda: AnchorGuard(lock_path, wait_s=0.3).acquire())
        assert proc.stdin is not None
        proc.stdin.close()
    finally:
        code, err = _finish(proc)
    assert code == 0, err
    _bounded(lambda: AnchorGuard(lock_path, wait_s=2.0).acquire().release())


# ------------------------------------------------------------------ validation: T25 (§2.1)


def test_a_loose_or_linked_lock_location_is_refused_as_found(
    lock_path: Path, lock_dir: Path, tmp_path: Path
) -> None:
    os.chmod(lock_dir, 0o750)
    with pytest.raises(AnchorGuardRefused, match="0750"):
        AnchorGuard(lock_path, wait_s=0.2).acquire()
    assert oct(os.stat(lock_dir).st_mode & 0o777) == oct(0o750), "the guard must not repair"
    os.chmod(lock_dir, 0o700)
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(lock_dir)
    with pytest.raises(AnchorGuardRefused):
        AnchorGuard(linked_dir / lock_path.name, wait_s=0.2).acquire()
    loose = lock_dir / "loose.lock"
    loose.touch(mode=0o644)
    os.chmod(loose, 0o644)
    with pytest.raises(AnchorGuardRefused, match="0644"):
        AnchorGuard(loose, wait_s=0.2).acquire()
    linked = lock_dir / "linked.lock"
    os.link(lock_dir / "loose.lock", linked)
    os.chmod(linked, 0o600)
    with pytest.raises(AnchorGuardRefused, match="link"):
        AnchorGuard(linked, wait_s=0.2).acquire()
    symlinked = lock_dir / "symlinked.lock"
    symlinked.symlink_to(lock_dir / "target.lock")
    with pytest.raises(AnchorGuardRefused):
        AnchorGuard(symlinked, wait_s=0.2).acquire()
    assert not (lock_dir / "target.lock").exists(), "a symlink was followed"


# M1-GAPS gap 6: this case needs a GENUINELY foreign-owned inode, which takes real root
# (chown). An unprivileged user namespace does not help where AppArmor restricts them
# (kernel.apparmor_restrict_unprivileged_userns=1: the "unprivileged_userns" profile
# denies CAP_CHOWN). Without root the property is carried by
# test_hash_chain_anchor_guard_regressions.py::
# test_a_lock_directory_owned_by_another_uid_is_refused_as_found_without_root (a real fstat
# of the real directory with geteuid patched), which fails if the owner check is removed.
@pytest.mark.skipif(
    os.geteuid() != 0,
    reason=(
        "needs root to chown a directory to another uid; without root the directory-owner "
        "check is pinned by test_hash_chain_anchor_guard_regressions.py::"
        "test_a_lock_directory_owned_by_another_uid_is_refused_as_found_without_root"
    ),
)
def test_a_foreign_owned_exact_0700_directory_is_refused_as_found(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o700)
    os.chmod(foreign, 0o700)
    os.chown(foreign, 65534, 65534)
    with pytest.raises(AnchorGuardRefused, match="65534"):
        AnchorGuard(foreign / "x.lock", wait_s=0.2).acquire()


def test_a_created_lock_file_is_owned_0600_with_one_link(lock_path: Path) -> None:
    AnchorGuard(lock_path, wait_s=0.5).acquire().release()
    status = os.stat(lock_path)
    assert status.st_uid == os.geteuid()
    assert status.st_mode & 0o777 == 0o600
    assert status.st_nlink == 1


@pytest.mark.parametrize("name", ["", ".", "..", "x" * 256])
def test_an_invalid_lock_name_is_refused(lock_dir: Path, name: str) -> None:
    with pytest.raises(AnchorGuardRefused, match="lock name"):
        AnchorGuard(f"{lock_dir}/{name}", wait_s=0.2)


def test_a_relative_lock_path_is_refused_at_acquisition(
    lock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-NEW-24's process-independence rule, applied to the injected path: a relative
    path resolves per working directory, so it refuses (author's reading, reported)."""

    monkeypatch.chdir(lock_dir)
    with pytest.raises(AnchorGuardRefused, match="absolute"):
        AnchorGuard("chronos.db.anchor-61.lock", wait_s=0.2).acquire()
    assert not (lock_dir / "chronos.db.anchor-61.lock").exists()


# ---------------------------------------------- C3/C4 pins: unwired, R11, no anchor I/O


def test_wait_s_is_required_with_no_default_and_no_deadline_constant() -> None:
    parameter = inspect.signature(AnchorGuard).parameters["wait_s"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError):
        AnchorGuard("x.lock")  # type: ignore[call-arg]
    constants = {
        node.value
        for node in ast.walk(ast.parse(_guard_block()))
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
    }
    assert not constants & {5, 30}, "an R11 deadline constant appeared"


@pytest.mark.parametrize(
    "wait_s",
    [-0.1, float("nan"), float("inf"), True, False],
    # K-c: a bool is not a number of seconds; True must not become a 1.0 s deadline.
    ids=["negative", "nan", "inf", "bool_true", "bool_false"],
)
def test_a_non_finite_or_negative_wait_is_refused_never_clamped(wait_s: float) -> None:
    with pytest.raises(AnchorGuardRefused, match="wait_s"):
        AnchorGuard("x.lock", wait_s=wait_s)


@pytest.mark.parametrize(
    "wait_s",
    [threading.TIMEOUT_MAX * 2, 10**10000],
    ids=["above_timeout_max", "int_10_pow_10000"],
)
def test_an_oversized_wait_is_a_typed_refusal_never_a_runtime_overflow(wait_s: int | float) -> None:
    with pytest.raises(AnchorGuardRefused, match="wait_s"):
        AnchorGuard("/tmp/unused.lock", wait_s=wait_s)


def _references_the_guard_module(source: str) -> bool:
    """True when the source REALLY references persistence.anchor_guard.

    Real references: any import form (absolute, ``from chronos.persistence import
    anchor_guard``, relative ``from . import anchor_guard`` / ``from .anchor_guard import``),
    an ``importlib.import_module`` / ``__import__`` call with a literal naming it, an
    attribute access ``.anchor_guard``, a bare name ``anchor_guard``, or any non-docstring
    string constant naming it (an indirect import by variable). Comments are not in the
    syntax tree, and docstrings are excluded, so a mention in documentation passes.
    """

    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any("anchor_guard" in alias.name.split(".") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module_parts = (node.module or "").split(".")
            if "anchor_guard" in module_parts:
                return True
            if any(alias.name == "anchor_guard" for alias in node.names):
                return True
        elif (
            (isinstance(node, ast.Attribute) and node.attr == "anchor_guard")
            or (isinstance(node, ast.Name) and node.id == "anchor_guard")
            or (
                isinstance(node, ast.Constant)
                and (
                    (isinstance(node.value, str) and "anchor_guard" in node.value)
                    or (isinstance(node.value, bytes) and b"anchor_guard" in node.value)
                )
                and id(node) not in docstrings
            )
        ):
            return True
    return False


def test_the_guard_is_unwired_and_does_no_anchor_io() -> None:
    package = SRC_ROOT / "chronos"
    guard_source = package / "persistence" / "anchor_guard.py"
    users = sorted(
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if path != guard_source and _references_the_guard_module(path.read_text())
    )
    assert users == [], f"the guard is referenced outside persistence/anchor_guard.py: {users}"
    assert not (package / "persistence" / "hash_chain_anchor.py").exists()
    block = _guard_block()
    for marker in ("head.json", "hash_chain", "anchors", "authoriz", "database_url", "_sqlite"):
        assert marker not in block, marker
