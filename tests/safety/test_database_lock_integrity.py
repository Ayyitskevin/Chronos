"""DBLOCK: ``Database`` must never drop its own SQLite WAL locks (P1-NEW-23).

POSIX record locks belong to the (process, file) pair, and ``close()`` of ANY
descriptor for a file releases every lock the process holds on it. Before this
fix, ``Database.__init__`` (and ``initialize()``) re-opened and closed the
database and its sidecars by path AFTER the first pooled connection, silently
dropping that connection's SQLite locks; with several processes on one file
that lost acknowledged commits (DRAIN-2r2, DRAIN-2r2-verify-reader).

The fix (DBLOCK-1r3, Kevin's "repair only before first connect"): one
process-wide, one-way repair window. During the first file-backed ``Database``
construction in a process the files may be repaired; the window then closes
permanently, before any engine exists, and every later check is ``lstat``-only.

R-21 is a startup check only: these tests pin the lock-integrity property and
the repair window. Nothing here claims protection against a concurrent change
of the paths by any actor.

Order independence (reader/Daybreak P2): every in-process REFUSE assertion first
builds a sacrificial file-backed ``Database`` in the test body, so the window is
CLOSED whatever ran before; every REPAIR assertion runs in a fresh child process.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from chronos.persistence import database as database_module
from chronos.persistence.database import Database

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="POSIX lock rows via /proc/locks")

SRC_ROOT = Path(database_module.__file__).resolve().parents[2]
CHILD_S = 60.0
SIDE = ("-wal", "-shm", "-journal")
_RESTART = "restart the process to repair"


def _url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def _lock_key(path: Path) -> str:
    """The file's identity as /proc/locks prints it: MAJ:MIN:INODE (hex major/minor)."""

    st = os.stat(path)
    return f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"


def _proc_locks_rows(keys: dict[str, str]) -> list[str]:
    """One full snapshot of /proc/locks (read to EOF), filtered to this pid and the keys.

    /proc/locks is a seq_file: one read() returns only one internal page (about 4 KiB), so a
    single os.read silently TRUNCATES it once a few dozen locks exist on the host. Reading to
    EOF takes several read() calls, between which other processes' lock churn can repeat or
    skip rows (DBLOCK-2r2); _lock_rows therefore demands two agreeing snapshots.
    """

    with open("/proc/locks", "rb") as handle:
        data = handle.read()
    rows = []
    for line in data.decode().splitlines():
        fields = line.split()
        if len(fields) < 8 or fields[1] != "POSIX" or fields[4] != str(os.getpid()):
            continue
        if fields[5] in keys:
            rows.append(f"{keys[fields[5]]}:{fields[6]}-{fields[7]}")
    return sorted(rows)


def _lock_rows(*paths: Path) -> list[str]:
    """This pid's POSIX lock rows on the given files, as 'name:start-end' (by device+inode).

    A full read is several read() calls and can tear under other processes' lock churn, so
    two consecutive snapshots of THIS pid's rows must agree before they are returned.
    """

    keys: dict[str, str] = {}
    for path in paths:
        try:
            keys[_lock_key(path)] = path.name
        except FileNotFoundError:
            continue
    previous = _proc_locks_rows(keys)
    for _ in range(50):
        current = _proc_locks_rows(keys)
        if current == previous:
            return current
        previous = current
    raise AssertionError("/proc/locks never gave two agreeing snapshots of this pid's rows")


def _db_rows(path: Path) -> list[str]:
    return _lock_rows(path, Path(f"{path}-shm"))


def _existing_database(path: Path) -> None:
    """An initialized database, as a deployment has: a fresh EMPTY file has no WAL state, so
    no -wal/-shm and no locks until its first write (a lock pin on it would be vacuous)."""

    setup = Database(_url(path))
    try:
        setup.initialize()
    finally:
        setup.dispose()


def _close_window(tmp_path: Path) -> None:
    """A sacrificial file-backed Database: afterwards the window is CLOSED, whatever ran before."""

    sacrificial = Database(_url(tmp_path / "sacrificial" / "s.db"))
    sacrificial.dispose()
    assert database_module._REPAIR_WINDOW_OPEN is False


class _OpenSpy:
    """Counts os.open calls whose path names one of the watched files (by name or basename)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *paths: Path) -> None:
        self.names = {str(path) for path in paths} | {path.name for path in paths}
        self.hits: list[str] = []
        real = os.open

        def spy(path: Any, *args: Any, **kwargs: Any) -> int:
            if str(path) in self.names or os.path.basename(str(path)) in self.names:
                self.hits.append(str(path))
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", spy)


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


# TEARDOWN-1: every child this module starts is reaped on EVERY path. It runs in its own
# process group (start_new_session) and the _children() context kills that group and waits
# in a finally (success, assertion failure, timeout, KeyboardInterrupt). Each child also
# carries a watchdog prelude, so a SIGKILLed pytest (no finally runs) cannot strand it: it
# exits when its parent changes or a bounded total deadline passes. The marker comment
# lets a leftover census find this module's children by command line.
_CHILD_MARK = "# chronos-test-child:test_database_lock_integrity"
_CHILD_DEADLINE_S = 300.0


def _child_code(code: str) -> str:
    return (
        f"{_CHILD_MARK}\n"
        "import os as _os, threading as _threading, time as _time\n"
        # The spawner's pid, not getppid(): a parent that died before this line runs
        # would make getppid() already the reaper's pid and blind the watchdog.
        "_PARENT = int(_os.environ.get('CHRONOS_TEST_PARENT_PID', _os.getppid()))\n"
        f"_DEADLINE = _time.monotonic() + {_CHILD_DEADLINE_S}\n"
        "def _orphan_watchdog():\n"
        "    while _os.getppid() == _PARENT and _time.monotonic() < _DEADLINE:\n"
        "        _time.sleep(0.1)\n"
        "    _os._exit(97)\n"
        # DBLOCK-2r3: a sentinel that keeps this process group non-empty (so its id is never
        # recycled) until the spawner closes the pipe's write end, or dies. It is the pipe's
        # ONLY reader (the leader closes its copy), forked before any thread or user code.
        "_GROUP_FD = _os.environ.pop('CHRONOS_TEST_GROUP_FD', None)\n"
        "if _GROUP_FD is not None:\n"
        "    if _os.fork() == 0:\n"
        "        try:\n"
        "            _null = _os.open(_os.devnull, _os.O_RDWR)\n"
        "            for _std in (0, 1, 2):\n"
        "                _os.dup2(_null, _std)\n"
        "            while _os.read(int(_GROUP_FD), 4096):\n"
        "                pass\n"
        "        finally:\n"
        "            _os._exit(0)\n"
        "    _os.close(int(_GROUP_FD))\n"
        "_threading.Thread(target=_orphan_watchdog, daemon=True).start()\n" + textwrap.dedent(code)
    )


class _Children:
    """Children started in their own process groups; reap() kills and waits for them all.

    Each group holds a sentinel (see _child_code) that is the only reader of a pipe whose write
    end only this process holds. While a 1-byte write to that pipe succeeds, the sentinel is a
    live member of the group, so the group exists and Linux cannot hand its id to anyone else:
    killpg on the recorded id reaches only our group, even after its leader has been reaped.
    """

    def __init__(self) -> None:
        self._procs: list[tuple[subprocess.Popen[str], int]] = []

    def popen(self, code: str, *args: str) -> subprocess.Popen[str]:
        env = _child_env()
        env["CHRONOS_TEST_PARENT_PID"] = str(os.getpid())
        read_fd, write_fd = os.pipe()
        env["CHRONOS_TEST_GROUP_FD"] = str(read_fd)
        try:
            proc = subprocess.Popen(
                [sys.executable, "-c", _child_code(code), *args],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                pass_fds=(read_fd,),
            )
        except BaseException:
            os.close(write_fd)
            raise
        finally:
            os.close(read_fd)
        os.set_blocking(write_fd, False)
        self._procs.append((proc, write_fd))
        return proc

    def run(self, code: str, *args: str) -> subprocess.CompletedProcess[str]:
        proc = self.popen(code, *args)
        out, err = proc.communicate(timeout=CHILD_S)
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    def reap(self) -> None:
        for proc, write_fd in self._procs:
            try:
                os.write(write_fd, b"\0")
                pinned = True
            except BrokenPipeError:  # no reader: the sentinel is gone, the id may be recycled
                pinned = False
            except BlockingIOError:  # a full pipe still has its reader
                pinned = True
            # An unreaped leader also pins its pid (and so the group id) until it is waited for.
            if pinned or proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                proc.communicate(timeout=CHILD_S)
            os.close(write_fd)
            if pinned:  # the killed members are reaped by init; signal 0 only probes
                deadline = time.monotonic() + CHILD_S
                while _group_alive(proc.pid) and time.monotonic() < deadline:
                    time.sleep(0.01)


@contextlib.contextmanager
def _children() -> Iterator[_Children]:
    children = _Children()
    try:
        yield children
    finally:
        children.reap()


def _run_child(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    with _children() as children:
        return children.run(code, *args)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _pid_alive(pid: int) -> bool:
    """Alive and not a zombie (an orphan's zombie is reaped by init, not by us)."""

    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def _construct(path: Path) -> BaseException | None:
    """Construct (and dispose) a Database; return the exception instead of raising it."""

    try:
        Database(_url(path)).dispose()
    except BaseException as error:
        return error
    return None


def _assert_refused(
    outcome: BaseException | None, match: str = "restart the process to repair"
) -> None:
    assert isinstance(outcome, RuntimeError), f"expected a refusal, got {outcome!r}"
    assert match in str(outcome), str(outcome)


@pytest.fixture
def window_closed(tmp_path: Path) -> Iterator[None]:
    _close_window(tmp_path)
    yield


# ------------------------------------------------------------------ T-0: the oracle itself


def test_the_proc_locks_oracle_sees_a_known_lock(tmp_path: Path) -> None:
    """Positive control (reader P3-2): if the parser cannot see a lock we took, FAIL."""

    target = tmp_path / "control.lock"
    with open(target, "w") as handle:
        fcntl.lockf(handle, fcntl.LOCK_EX)
        rows = _lock_rows(target)
    assert rows, "the /proc/locks oracle cannot see this process's own lock; the pins are blind"


# ------------------------------------------------------------------ T-2: lock rows survive


def test_construction_keeps_the_pooled_connections_wal_locks(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    _existing_database(path)
    database = Database(_url(path))
    try:
        rows = _db_rows(path)
        assert any(row.startswith("a.db-shm:128-128") for row in rows), rows
        assert any(row.startswith("a.db:") for row in rows), rows
        database.initialize()
        assert _db_rows(path) == rows, "initialize() dropped the pooled connection's locks"
    finally:
        database.dispose()


def test_a_second_in_process_database_keeps_the_first_engines_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "a.db"
    _existing_database(path)
    first = Database(_url(path))
    try:
        rows = _db_rows(path)
        assert rows
        spy = _OpenSpy(monkeypatch, path, *(Path(f"{path}{suffix}") for suffix in SIDE))
        second = Database(_url(path))
        second.dispose()
        assert spy.hits == [], spy.hits
        assert _db_rows(path) == rows
    finally:
        first.dispose()


def test_dispose_and_reuse_of_the_same_engine_never_reopens_a_repair_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "a.db"
    _existing_database(path)
    first = Database(_url(path))
    try:
        first.dispose()
        with first.engine.connect() as connection:  # the SAME engine, reused
            connection.execute(text("SELECT 1"))
            rows = _db_rows(path)
            assert rows
            shm = Path(f"{path}-shm")
            shm.chmod(0o644)
            spy = _OpenSpy(monkeypatch, path, *(Path(f"{path}{suffix}") for suffix in SIDE))
            outcome = _construct(path)
            assert _db_rows(path) == rows, "the constructor dropped the live connection's locks"
            assert spy.hits == [], spy.hits
            assert (shm.stat().st_mode & 0o777) == 0o644, "the drift was repaired"
            _assert_refused(outcome)
    finally:
        first.dispose()


@pytest.mark.parametrize("spelling", ["hard_link", "symlinked_directory"])
def test_an_alias_with_a_drifted_mode_is_refused_and_never_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    path = tmp_path / "live" / "a.db"
    _existing_database(path)
    first = Database(_url(path))
    try:
        rows = _db_rows(path)
        assert rows
        if spelling == "hard_link":
            alias_dir = tmp_path / "alias"
            alias_dir.mkdir()
            alias = alias_dir / "a.db"
            os.link(path, alias)
            for suffix in ("-wal", "-shm"):
                os.link(f"{path}{suffix}", f"{alias}{suffix}")
        else:
            (tmp_path / "dirlink").symlink_to(path.parent, target_is_directory=True)
            alias = tmp_path / "dirlink" / "a.db"
        Path(f"{alias}-shm").chmod(0o644)  # same inode as the live -shm
        watched = [path, alias, *(Path(f"{p}{s}") for p in (path, alias) for s in SIDE)]
        spy = _OpenSpy(monkeypatch, *watched)
        outcome = _construct(alias)
        assert _db_rows(path) == rows, "the alias constructor dropped the live connection's locks"
        assert spy.hits == [], spy.hits
        # DBLOCK-2r1: a hard-linked file is refused by its link count before any mode check
        _assert_refused(outcome, "hard link" if spelling == "hard_link" else _RESTART)
    finally:
        first.dispose()


# ------------------------------------------------------------------ T-1: Daybreak's barrier pin


def test_no_repair_can_race_a_dbapi_open_after_the_window_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daybreak P1-NEW-1 / r3 P2-5: the alias helper decides, THEN a physical connection on the
    retained engine positively locks the replacement -shm inode, THEN the helper resumes.

    GREEN: refusal, zero opens on every alias and canonical name, the lock rows unchanged.
    The base has no decision-point seam; there the helper runs after the lock is observed
    (the same ordering an fd-scan design loses), and it repairs, dropping the row.
    """

    path = tmp_path / "live" / "a.db"
    _existing_database(path)
    engine_owner = Database(_url(path))
    engine_owner.dispose()  # fully disposed: no physical connection, no sidecar fd
    shm = Path(f"{path}-shm")
    for suffix in SIDE:  # SQLite removed them at the last close
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    # Non-empty on purpose: SQLite's robust_open fchmods a ZERO-size file to the database's
    # mode when it opens it, which would silently erase the drift this pin plants.
    fd = os.open(shm, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, b"\0" * 32768)
    finally:
        os.close(fd)
    alias_dir = tmp_path / "alias"
    alias_dir.mkdir()
    alias = alias_dir / "a.db"
    os.link(path, alias)
    os.link(shm, f"{alias}-shm")
    Path(f"{alias}-shm").chmod(0o644)  # the drifted mode, on the replacement inode
    replacement_inode = os.stat(shm).st_ino

    decided = threading.Event()
    release = threading.Event()
    has_seam = hasattr(database_module, "_ADMISSION_STEP_HOOK")
    if has_seam:

        def hook(step: str) -> None:
            if step == "decided":
                decided.set()
                assert release.wait(10), "the test never released the helper"

        monkeypatch.setattr(database_module, "_ADMISSION_STEP_HOOK", hook)

    outcome: dict[str, BaseException | None] = {"error": None}

    def construct_alias() -> None:
        try:
            Database(_url(alias)).dispose()
        except BaseException as error:
            outcome["error"] = error

    watched = [path, alias, *(Path(f"{p}{s}") for p in (path, alias) for s in SIDE)]
    spy = _OpenSpy(monkeypatch, *watched)
    helper = threading.Thread(target=construct_alias)
    try:
        if has_seam:
            helper.start()
            assert decided.wait(10), "the alias helper never reached its decision point"
        with engine_owner.engine.connect() as connection:  # a NEW physical connection
            connection.execute(text("SELECT 1"))
            rows = _db_rows(path)
            assert any(row.startswith("a.db-shm:") for row in rows), rows
            assert os.stat(shm).st_ino == replacement_inode
            assert (os.stat(shm).st_mode & 0o777) == 0o644, "the planted drift is gone"
            if has_seam:
                release.set()
            else:
                helper.start()
            helper.join(10)
            assert not helper.is_alive()
            assert _db_rows(path) == rows, "the helper dropped the live connection's locks"
            assert spy.hits == [], spy.hits
            _assert_refused(outcome["error"], "hard link")  # DBLOCK-2r1: link count first
    finally:
        release.set()
        helper.join(10)
        engine_owner.dispose()


# ------------------------------------------------------------------ T-3: the reader's WAL-loss test

_WALLOSS_B = """
import os, sys, time
from pathlib import Path
from sqlalchemy import text
from chronos.persistence.database import Database
url, d = sys.argv[1], Path(sys.argv[2])
db = Database(url)
acks = open(d / "acks", "a")
def commit(i):
    with db.engine.begin() as c:
        c.execute(text("INSERT INTO walloss(id) VALUES (:i)"), {"i": i})
    acks.write(f"{i}\\n"); acks.flush(); os.fsync(acks.fileno())
for i in range(1, 51):
    commit(i)
(d / "b.ready").touch()
deadline = time.monotonic() + 30
while not (d / "a.disposed").exists():
    assert time.monotonic() < deadline, "A never disposed"
    time.sleep(0.01)
for i in range(51, 81):
    commit(i)
(d / "b.done").touch()
time.sleep(120)
"""

_WALLOSS_A = """
import json, os, sys, time
from pathlib import Path
from sqlalchemy import text
from chronos.persistence.database import Database
url, d, wal = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
db = Database(url)
with db.engine.connect() as c:
    c.execute(text("SELECT count(*) FROM walloss")).scalar()
deadline = time.monotonic() + 30
while not (d / "b.ready").exists():
    assert time.monotonic() < deadline, "B never became ready"
    time.sleep(0.01)
def ident():
    try:
        st = os.stat(wal)
        return [st.st_ino, st.st_size]
    except FileNotFoundError:
        return None
before = ident()
db.dispose()
after = ident()
(d / "a.json").write_text(json.dumps({"before": before, "after": after}))
(d / "a.disposed").touch()
"""


def test_a_second_process_closing_does_not_delete_the_wal_under_a_live_committer(
    tmp_path: Path,
) -> None:
    """The reader's walloss probe (DRAIN-2r2-verify-reader §4), barrier-driven, deterministic.

    B commits and acknowledges rows one by one; A, another process on the same file, disposes
    mid-run; B commits more, then dies (SIGKILL). Every acknowledged row must survive and A's
    dispose must not delete the WAL B is still writing.
    """

    path = tmp_path / "w.db"
    setup = Database(_url(path))
    try:
        setup.initialize()
        with setup.engine.begin() as connection:
            connection.execute(text("CREATE TABLE walloss (id INTEGER PRIMARY KEY)"))
    finally:
        setup.dispose()
    with _children() as children:
        b = children.popen(_WALLOSS_B, _url(path), str(tmp_path))
        a = children.run(_WALLOSS_A, _url(path), str(tmp_path), f"{path}-wal")
        if a.returncode != 0:
            os.killpg(b.pid, signal.SIGKILL)
            _out, b_err = b.communicate(timeout=CHILD_S)
            pytest.fail(f"A failed: {a.stderr}\nB stderr: {b_err}")
        deadline = time.monotonic() + CHILD_S
        while not (tmp_path / "b.done").exists():
            assert b.poll() is None, b.communicate()[1]
            assert time.monotonic() < deadline, "B never finished its post-dispose commits"
            time.sleep(0.01)
        os.killpg(b.pid, signal.SIGKILL)  # B "dies" here (the crash under test)
        b.communicate(timeout=CHILD_S)
    wal = json.loads((tmp_path / "a.json").read_text())
    acked = [int(line) for line in (tmp_path / "acks").read_text().split()]
    count = _run_child(
        """
        import sqlite3, sys
        c = sqlite3.connect(sys.argv[1])
        print(c.execute("SELECT count(*) FROM walloss").fetchone()[0])
        """,
        str(path),
    )
    assert count.returncode == 0, count.stderr
    present = int(count.stdout.strip())
    assert wal["before"] is not None
    assert wal["after"] is not None, f"A's dispose deleted the live WAL: {wal}"
    assert wal["after"][0] == wal["before"][0], wal
    assert len(acked) == 80
    assert present == len(acked), f"{len(acked) - present} acknowledged commits lost"


# ------------------------------------------------------------------ T-4: fork


def test_a_fork_while_a_sibling_holds_the_window_lock_does_not_strand_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daybreak P2-NEW-3: the child must not inherit a lock held by a thread it does not have."""

    _close_window(tmp_path)
    live = tmp_path / "live.db"
    _existing_database(live)
    holder = Database(_url(live))
    admitted = tmp_path / "admitted.db"
    Database(_url(admitted)).dispose()
    rows = _db_rows(live)
    assert rows

    inside = threading.Event()
    release = threading.Event()

    def hook(step: str) -> None:
        if step == "decided" and threading.current_thread().name == "sibling":
            inside.set()
            release.wait(10)

    monkeypatch.setattr(database_module, "_ADMISSION_STEP_HOOK", hook, raising=False)
    sibling = threading.Thread(
        target=lambda: Database(_url(tmp_path / "sib.db")).dispose(), name="sibling"
    )
    sibling.start()
    pid = 0
    child_reaped = False
    try:
        assert inside.wait(10), "the sibling never entered the window's critical section"
        pid = os.fork()
        if pid == 0:  # the child: its own process group, a bounded time, then exit
            os.setpgid(0, 0)
            signal.alarm(10)
            try:
                Database(_url(admitted)).dispose()
            except BaseException:
                os._exit(3)
            os._exit(0)
        deadline = time.monotonic() + 20
        status = None
        while time.monotonic() < deadline:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                child_reaped = True
                break
            time.sleep(0.05)
        else:
            pytest.fail("the forked child deadlocked on the inherited window lock")
        assert os.waitstatus_to_exitcode(status) == 0, os.waitstatus_to_exitcode(status)
        assert _db_rows(live) == rows
    finally:
        if pid and not child_reaped:  # TEARDOWN-1: never leave the forked child behind
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
        release.set()
        sibling.join(10)
        holder.dispose()


# ------------------------------------------------------------------ T-5: the window is one-way

_ONE_WAY = """
import json, os, sys
from pathlib import Path
from chronos.persistence import database as m
from chronos.persistence.database import Database
d = Path(sys.argv[1]); report = {}
def url(p): return f"sqlite+pysqlite:///{p}"
p1 = d / "p1.db"
p1.touch(); p1.chmod(0o666)
for s in ("-wal", "-shm", "-journal"):
    q = Path(f"{p1}{s}"); q.touch(); q.chmod(0o666)
assert m._REPAIR_WINDOW_OPEN is True
first = Database(url(p1))  # the window is OPEN: repaired
def mode(p): return p.stat().st_mode & 0o777
existing = [Path(f"{p1}{s}") for s in ("", "-wal", "-shm", "-journal")]
report["repaired"] = all(mode(q) == 0o600 for q in existing if q.exists())
assert m._REPAIR_WINDOW_OPEN is False
first.dispose()
j = Path(f"{p1}-journal"); j.touch(); j.chmod(0o666)
RESTART = "restart the process to repair"
try:
    Database(url(p1)); report["same_path_after_dispose"] = "constructed"
except RuntimeError as e:
    report["same_path_after_dispose"] = "refused" if RESTART in str(e) else str(e)
p2 = d / "p2.db"; p2.touch(); p2.chmod(0o644)
try:
    Database(url(p2)); report["new_path_drifted"] = "constructed"
except RuntimeError as e:
    report["new_path_drifted"] = "refused" if RESTART in str(e) else str(e)
opened = []
real = os.open
def spy(path, *a, **k):
    opened.append(os.path.basename(str(path)))
    return real(path, *a, **k)
os.open = spy
p3 = d / "p3.db"
Database(url(p3)).dispose()
os.open = real
report["p3_mode"] = oct(p3.stat().st_mode & 0o777)
report["p3_nlink"] = p3.stat().st_nlink
report["p3_opened_by_name"] = [n for n in opened if n == "p3.db" or n.startswith("p3.db-")]
report["p3_temp_opened"] = any(n.startswith(".p3.db.create-") for n in opened)
report["strays"] = sorted(n.name for n in d.iterdir() if ".create-" in n.name)
print(json.dumps(report))
"""

_FAILED_FIRST = """
import sys
from pathlib import Path
import sqlalchemy
from chronos.persistence import database as m
from chronos.persistence.database import Database
d = Path(sys.argv[1])
def url(p): return f"sqlite+pysqlite:///{p}"
real = m.create_engine
def boom(*a, **k): raise RuntimeError("forced first engine failure")
m.create_engine = boom
try:
    Database(url(d / "a.db"))
except RuntimeError as e:
    assert "forced" in str(e)
m.create_engine = real
b = d / "b.db"; b.touch(); b.chmod(0o666)
try:
    Database(url(b)); print("constructed")
except RuntimeError as e:
    print("refused" if "restart the process to repair" in str(e) else str(e))
"""


def test_the_repair_window_is_one_way(tmp_path: Path) -> None:
    result = _run_child(_ONE_WAY, str(tmp_path))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip())
    assert report["repaired"] is True
    assert report["same_path_after_dispose"] == "refused"
    assert report["new_path_drifted"] == "refused"
    assert report["p3_mode"] == "0o600"
    assert report["p3_nlink"] == 1
    assert report["p3_temp_opened"] is True
    assert report["p3_opened_by_name"] == [], report
    assert report["strays"] == []


def test_a_failed_first_construction_still_closes_the_window(tmp_path: Path) -> None:
    result = _run_child(_FAILED_FIRST, str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "refused"


# ------------------------------------------------------------------ T-6: creation crash safety

_CREATE_CRASH = """
import os, sys
from pathlib import Path
from chronos.persistence import database as m
from chronos.persistence.database import Database
d = Path(sys.argv[1])
def hook(step):
    if step == "linked":
        os._exit(7)
m._ADMISSION_STEP_HOOK = hook
Database(f"sqlite+pysqlite:///{d / 'c.db'}")
os._exit(0)
"""


def test_a_crash_between_link_and_unlink_is_refused_naming_the_exact_stray(
    tmp_path: Path,
) -> None:
    """DBLOCK-2r1 (Daybreak P1): the crash residue gives the database a SECOND name, which
    would give SQLite two WAL namespaces for one file. It is refused fail-closed, naming the
    exact private temp; nothing deletes it automatically (a name pattern is not proof the
    stray is ours). After the operator removes that exact file, the database starts."""

    crashed = _run_child(_CREATE_CRASH, str(tmp_path))
    assert crashed.returncode == 7, (crashed.returncode, crashed.stderr)
    strays = [p for p in tmp_path.iterdir() if ".create-" in p.name]
    assert len(strays) == 1
    assert (tmp_path / "c.db").stat().st_nlink == 2
    start = """
        import sys
        from chronos.persistence.database import Database
        try:
            d = Database(f"sqlite+pysqlite:///{sys.argv[1]}/c.db"); d.initialize(); d.dispose()
        except RuntimeError as error:
            print("refused:", error)
        else:
            print("ok")
        """
    refused = _run_child(start, str(tmp_path))
    assert refused.returncode == 0, refused.stderr
    assert refused.stdout.startswith("refused:"), refused.stdout
    assert str(strays[0]) in refused.stdout, refused.stdout  # the exact private temp
    assert strays[0].exists(), "the stray was removed automatically"
    strays[0].unlink()  # the operator's step
    restarted = _run_child(start, str(tmp_path))
    assert restarted.returncode == 0, restarted.stderr
    assert restarted.stdout.strip() == "ok"


# ------------------------------------------------------------------ DBLOCK-2r1: link counts

_HARDLINK_WORKER = """
import os, sys, time
from pathlib import Path
from sqlalchemy import text
from chronos.persistence.database import Database
path, root, rid = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
try:
    database = Database(f"sqlite+pysqlite:///{path}")
except RuntimeError as error:
    (root / f"{rid}.refused").write_text(str(error))
    sys.exit(0)
(root / f"{rid}.ready").touch()
deadline = time.monotonic() + 30
while not (root / "release").exists():
    assert time.monotonic() < deadline, "never released"
    time.sleep(0.005)
with database.engine.begin() as connection:
    connection.execute(text("INSERT INTO forked_wal(id) VALUES (:i)"), {"i": int(rid)})
fd = os.open(root / f"{rid}.ack", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, b"committed"); os.fsync(fd); os.close(fd)
time.sleep(120)
"""


def test_a_stable_hard_link_to_the_database_is_refused_before_any_write(
    tmp_path: Path,
) -> None:
    """Daybreak's DBLOCK-2 P1 probe as a pin: a stable owner-owned 0600 hard link, two path
    spellings, two processes. SQLite keys its WAL files by pathname, so two spellings of one
    inode get two WAL namespaces and acknowledged writes fork. Each construction must refuse
    before an engine exists; no write may be acknowledged."""

    canonical = tmp_path / "canonical.db"
    alias = tmp_path / "alias.db"
    setup = Database(_url(canonical))
    try:
        setup.initialize()
        with setup.engine.begin() as connection:
            connection.execute(text("CREATE TABLE forked_wal (id INTEGER PRIMARY KEY)"))
    finally:
        setup.dispose()
    os.link(canonical, alias)
    assert canonical.stat().st_mode & 0o777 == 0o600 == alias.stat().st_mode & 0o777
    with _children() as children:
        for path, rid in ((canonical, "1"), (alias, "2")):
            children.popen(_HARDLINK_WORKER, str(path), str(tmp_path), rid)
        deadline = time.monotonic() + CHILD_S
        for rid in ("1", "2"):
            while not any((tmp_path / f"{rid}.{kind}").exists() for kind in ("ready", "refused")):
                assert time.monotonic() < deadline, f"worker {rid} never decided"
                time.sleep(0.01)
        accepted = [rid for rid in ("1", "2") if (tmp_path / f"{rid}.ready").exists()]
        if accepted:
            (tmp_path / "release").touch()
            for rid in accepted:
                while not (tmp_path / f"{rid}.ack").exists():
                    assert time.monotonic() < deadline, f"worker {rid} never acknowledged"
                    time.sleep(0.01)
    rows = {}
    for name, path in (("canonical", canonical), ("alias", alias)):
        result = _run_child(
            """
            import sqlite3, sys
            c = sqlite3.connect(sys.argv[1])
            print([r[0] for r in c.execute("SELECT id FROM forked_wal ORDER BY id")])
            """,
            str(path),
        )
        rows[name] = result.stdout.strip()
    assert accepted == [], f"a hard-linked database was admitted; rows after the crash: {rows}"
    for rid in ("1", "2"):
        assert "hard link" in (tmp_path / f"{rid}.refused").read_text()
    assert not any(tmp_path.glob("*.ack"))


def test_a_hard_linked_sidecar_is_refused_in_the_open_and_the_closed_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """st_nlink != 1 on an existing sidecar refuses before any engine or open."""

    open_dir = tmp_path / "open"
    open_dir.mkdir()
    _existing_database(open_dir / "o.db")
    journal = Path(f"{open_dir / 'o.db'}-journal")
    journal.touch(mode=0o600)
    os.link(journal, open_dir / "second-name")
    fresh = _run_child(
        """
        import sys
        from chronos.persistence import database as m
        from chronos.persistence.database import Database
        assert m._REPAIR_WINDOW_OPEN is True
        try:
            Database(f"sqlite+pysqlite:///{sys.argv[1]}")
        except RuntimeError as error:
            print("refused:", error)
        else:
            print("constructed")
        """,
        str(open_dir / "o.db"),
    )
    assert fresh.returncode == 0, fresh.stderr
    assert fresh.stdout.startswith("refused:") and "hard link" in fresh.stdout, fresh.stdout

    _close_window(tmp_path)
    closed = tmp_path / "closed" / "c.db"
    _existing_database(closed)
    sidecar = Path(f"{closed}-journal")
    sidecar.touch(mode=0o600)
    os.link(sidecar, closed.parent / "second-name")
    spy = _OpenSpy(monkeypatch, closed, *(Path(f"{closed}{suffix}") for suffix in SIDE))
    _assert_refused(_construct(closed), "hard link")
    assert spy.hits == [], spy.hits


# ------------------------------------------------------------------ T-7 / T-8: non-regression pins


def test_the_documented_deployment_starts_under_umask_002(tmp_path: Path) -> None:
    """DEPLOYMENT.md: `mkdir -p data logs`; under umask 002 data/ is 0775. No parent check here."""

    result = _run_child(
        """
        import os, subprocess, sys
        from pathlib import Path
        from chronos.persistence.database import Database
        os.umask(0o002); os.chdir(sys.argv[1])
        subprocess.run(["mkdir", "-p", "data", "logs"], check=True)
        print(oct(Path("data").stat().st_mode & 0o777))
        d = Database("sqlite+pysqlite:///data/chronos.db"); d.initialize(); d.dispose()
        print(oct(Path("data/chronos.db").stat().st_mode & 0o777))
        """,
        str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["0o775", "0o600"]


@pytest.mark.parametrize(
    ("umask", "db_mode"), [(0o022, 0o600), (0o000, 0o600), (0o077, 0o600), (0o022, 0o640)]
)
def test_sqlite_creates_sidecars_with_the_database_mode(
    tmp_path: Path, umask: int, db_mode: int
) -> None:
    """The premise R-21's post-connect check relies on (DBLOCK-1 G4): inherited, not hardcoded."""

    result = _run_child(
        """
        import os, sqlite3, sys
        os.umask(int(sys.argv[2])); db = sys.argv[1]
        fd = os.open(db, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(fd, int(sys.argv[3])); os.close(fd)
        c = sqlite3.connect(db); c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE t(x)"); c.commit()
        def mode(p): return oct(os.stat(p).st_mode & 0o777)
        print(sqlite3.sqlite_version, mode(db + "-wal"), mode(db + "-shm"))
        c.close()
        """,
        str(tmp_path / "x.db"),
        str(umask),
        str(db_mode),
    )
    assert result.returncode == 0, result.stderr
    _version, wal, shm = result.stdout.split()
    assert wal == shm == oct(db_mode)


# ------------------------------------------------------------------ refusal cleanup


def test_a_refused_instance_is_dead_and_holds_no_lock(tmp_path: Path, window_closed: None) -> None:
    path = tmp_path / "r.db"
    database = Database(_url(path))
    database.initialize()
    Path(f"{path}-shm").chmod(0o644)
    with pytest.raises(RuntimeError, match="restart the process to repair"):
        database.initialize()
    assert _db_rows(path) == [], "the refused instance kept its lock rows"
    with pytest.raises(RuntimeError, match="refused its database files"):
        database.engine.connect()
    assert database.readable() is False


# ------------------------------------------------------------------ TEARDOWN-1: positive controls

_HANG = "import time\ntime.sleep(600)\n"
_INTERMEDIATE = """
import os, subprocess, sys, time
env = dict(os.environ, CHRONOS_TEST_PARENT_PID=str(os.getpid()))  # as any spawner does
child = subprocess.Popen(
    [sys.executable, "-c", sys.argv[1]], start_new_session=True, env=env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print(child.pid, flush=True)
time.sleep(600)
"""


def _sqlite_file_with_mode(path: Path, mode: int) -> None:
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE marker(id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    path.chmod(mode)


def test_c1_a_closed_window_refuses_a_file_without_owner_write_at_admission(
    tmp_path: Path, window_closed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DBLOCK-2r3 C1 (Daybreak P2-1): 0400 is refused up front, not left for SQLite to fail late."""

    candidate = tmp_path / "read-only.db"
    _sqlite_file_with_mode(candidate, 0o400)

    def unexpected_create_engine(*args: object, **kwargs: object) -> None:
        raise AssertionError("create_engine was reached before owner-mode refusal")

    monkeypatch.setattr(database_module, "create_engine", unexpected_create_engine)
    with pytest.raises(RuntimeError) as refused:
        Database(_url(candidate))
    assert "unsafe mode 0400" in str(refused.value)
    assert database_module._RESTART_TO_REPAIR in str(refused.value)


def test_c1_a_closed_window_refuses_a_file_without_owner_read_at_admission(
    tmp_path: Path, window_closed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DBLOCK-2r3 C1 (Daybreak P2-1): 0200 is refused up front, not left for SQLite to fail late."""

    candidate = tmp_path / "write-only.db"
    _sqlite_file_with_mode(candidate, 0o200)

    def unexpected_create_engine(*args: object, **kwargs: object) -> None:
        raise AssertionError("create_engine was reached before owner-mode refusal")

    monkeypatch.setattr(database_module, "create_engine", unexpected_create_engine)
    with pytest.raises(RuntimeError) as refused:
        Database(_url(candidate))
    assert "unsafe mode 0200" in str(refused.value)
    assert database_module._RESTART_TO_REPAIR in str(refused.value)


def test_teardown_control_a_failing_body_still_reaps_its_children() -> None:
    """A failure inside the context (here a forced assertion) leaves no child alive."""

    pgids: list[int] = []
    try:
        with pytest.raises(AssertionError, match="forced"), _children() as children:
            for _ in range(3):
                pgids.append(children.popen(_HANG).pid)
            raise AssertionError("forced failure inside the reaping context")
        assert [pgid for pgid in pgids if _group_alive(pgid)] == []
    finally:
        for pgid in pgids:  # never let a broken reaper leak the hung children
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)


def test_teardown_control_an_orphaned_child_exits_on_its_own() -> None:
    """A child whose parent is SIGKILLed (no finally runs there) exits via its watchdog."""

    grandchild = 0
    try:
        with _children() as children:
            parent = children.popen(_INTERMEDIATE, _child_code(_HANG))
            assert parent.stdout is not None
            grandchild = int(parent.stdout.readline())
            os.killpg(parent.pid, signal.SIGKILL)
            parent.wait(CHILD_S)
        deadline = time.monotonic() + 10
        while _pid_alive(grandchild):
            assert time.monotonic() < deadline, "the orphaned child outlived its parent"
            time.sleep(0.05)
    finally:
        if grandchild:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(grandchild, signal.SIGKILL)


_LEADER_WITH_DESCENDANT = """
import subprocess, sys
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(child.pid, flush=True)
"""


def test_c2_a_reaped_leader_does_not_exempt_its_same_group_descendant() -> None:
    """DBLOCK-2r3 C2 (Daybreak TEARDOWN-1 P1): the leader exits 0 after starting a same-group
    hung descendant and the caller reaps the leader; leaving the context leaves no survivor."""

    descendant = 0
    group = 0
    try:
        with _children() as children:
            leader = children.popen(_LEADER_WITH_DESCENDANT)
            group = leader.pid
            out, err = leader.communicate(timeout=CHILD_S)
            assert leader.returncode == 0, err
            descendant = int(out.strip())
            assert _pid_alive(descendant), "the descendant must reach the cleanup boundary"
            assert os.getpgid(descendant) == group
        assert not _pid_alive(descendant), "a reaped leader exempted its descendant"
        assert not _group_alive(group)
    finally:
        if descendant and _pid_alive(descendant):  # never leak it from a broken reaper
            with contextlib.suppress(ProcessLookupError):
                os.kill(descendant, signal.SIGKILL)


def test_c2_a_group_outlives_its_reaped_leader_inside_the_context() -> None:
    """The recorded pgid stays pinned to OUR group after its leader is reaped (the sentinel), so
    the closing killpg cannot reach a recycled id."""

    with _children() as children:
        leader = children.popen("pass")
        leader.communicate(timeout=CHILD_S)
        assert leader.returncode == 0
        assert _group_alive(leader.pid), "nothing pins the group once its leader is reaped"
    assert not _group_alive(leader.pid)


def test_c2_reap_never_signals_a_group_the_body_already_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-unrelated-process invariant: once the body has killed and reaped a group (its sentinel
    gone, so its id may be recycled), reap() sends that id no SIGKILL."""

    with _children() as children:
        leader = children.popen(_HANG)
        os.killpg(leader.pid, signal.SIGKILL)
        leader.communicate(timeout=CHILD_S)
        deadline = time.monotonic() + CHILD_S
        while _group_alive(leader.pid):
            assert time.monotonic() < deadline, "the killed group never went away"
            time.sleep(0.01)
        sent: list[tuple[int, int]] = []
        real_killpg = os.killpg

        def recording_killpg(pgid: int, sig: int) -> None:
            sent.append((pgid, sig))
            real_killpg(pgid, sig)

        monkeypatch.setattr(os, "killpg", recording_killpg)
    assert (leader.pid, signal.SIGKILL) not in sent, sent
