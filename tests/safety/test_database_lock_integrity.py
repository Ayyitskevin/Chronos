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


def _url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def _lock_rows(*paths: Path) -> list[str]:
    """This pid's POSIX lock rows on the given files, as 'name:start-end' (by inode)."""

    inodes: dict[int, str] = {}
    for path in paths:
        try:
            inodes[os.stat(path).st_ino] = path.name
        except FileNotFoundError:
            continue
    rows = []
    with open("/proc/locks") as handle:
        for line in handle:
            fields = line.split()
            if fields[1] != "POSIX" or int(fields[4]) != os.getpid():
                continue
            inode = int(fields[5].split(":")[2])
            if inode in inodes:
                rows.append(f"{inodes[inode]}:{fields[6]}-{fields[7]}")
    return sorted(rows)


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


def _run_child(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), *args],
        capture_output=True,
        text=True,
        env=_child_env(),
        timeout=CHILD_S,
        check=False,
    )


def _construct(path: Path) -> BaseException | None:
    """Construct (and dispose) a Database; return the exception instead of raising it."""

    try:
        Database(_url(path)).dispose()
    except BaseException as error:
        return error
    return None


def _assert_refused(outcome: BaseException | None) -> None:
    assert isinstance(outcome, RuntimeError), f"expected a refusal, got {outcome!r}"
    assert "restart the process to repair" in str(outcome), str(outcome)


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
        _assert_refused(outcome)
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
    fd = os.open(shm, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
            if has_seam:
                release.set()
            else:
                helper.start()
            helper.join(10)
            assert not helper.is_alive()
            assert _db_rows(path) == rows, "the helper dropped the live connection's locks"
            assert spy.hits == [], spy.hits
            _assert_refused(outcome["error"])
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
    env = _child_env()
    b = subprocess.Popen(
        [sys.executable, "-c", _WALLOSS_B, _url(path), str(tmp_path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        a = subprocess.run(
            [sys.executable, "-c", _WALLOSS_A, _url(path), str(tmp_path), f"{path}-wal"],
            env=env,
            capture_output=True,
            text=True,
            timeout=CHILD_S,
            check=False,
        )
        if a.returncode != 0:
            b.send_signal(signal.SIGKILL)
            _out, b_err = b.communicate(timeout=CHILD_S)
            pytest.fail(f"A failed: {a.stderr}\nB stderr: {b_err}")
        deadline = time.monotonic() + CHILD_S
        while not (tmp_path / "b.done").exists():
            assert b.poll() is None, b.communicate()[1]
            assert time.monotonic() < deadline, "B never finished its post-dispose commits"
            time.sleep(0.01)
    finally:
        b.send_signal(signal.SIGKILL)
        b.wait(CHILD_S)
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
    try:
        assert inside.wait(10), "the sibling never entered the window's critical section"
        pid = os.fork()
        if pid == 0:  # the child: construct within a bounded time, then exit
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
                break
            time.sleep(0.05)
        else:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail("the forked child deadlocked on the inherited window lock")
        assert os.waitstatus_to_exitcode(status) == 0, os.waitstatus_to_exitcode(status)
        assert _db_rows(live) == rows
    finally:
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


def test_a_crash_between_link_and_unlink_leaves_a_harmless_stray(tmp_path: Path) -> None:
    crashed = _run_child(_CREATE_CRASH, str(tmp_path))
    assert crashed.returncode == 7, (crashed.returncode, crashed.stderr)
    strays = [p for p in tmp_path.iterdir() if ".create-" in p.name]
    assert len(strays) == 1
    assert (tmp_path / "c.db").stat().st_nlink == 2
    restarted = _run_child(
        """
        import sys
        from chronos.persistence.database import Database
        d = Database(f"sqlite+pysqlite:///{sys.argv[1]}/c.db"); d.initialize(); d.dispose()
        print("ok")
        """,
        str(tmp_path),
    )
    assert restarted.returncode == 0, restarted.stderr
    assert restarted.stdout.strip() == "ok"


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
