"""LEDGER-2: ``SqliteLedger`` must never drop its own SQLite WAL locks (P1-NEW-24).

POSIX record locks belong to the (process, file) pair, and ``close()`` of ANY
descriptor for a file releases every lock the process holds on it. Before this
fix ``SqliteLedger.__init__`` re-opened and closed the ledger and its sidecars by
path AFTER its connection was open (``secure_owner_only`` x3), silently dropping
that connection's locks; a read-write client closing beside it (including the
documented ``sqlite3 data/platform_ledger.db ".backup ..."``) then deleted the
live WAL and a crash lost every acknowledged commit after that point
(LEDGER-LOCK-probe-reader: 6/10, 9/10, 5/8 trials; 0/25 in controls).

The fix (LEDGER-1r1, Kevin's DBLOCK rule applied to the ledger): secure the
files only BEFORE the first connection, inside a module-private one-way repair
window; create a missing ledger under an unpublished name and link it into
place only after its descriptor is closed; refuse symlinks, foreign owners and
``nlink != 1`` before SQLite sees the path; after the connection exists every
check is ``lstat``-only and refuses what it would have repaired (a restart
repairs); any constructor failure closes the connection before re-raising.

Order independence: every in-process REFUSE assertion first constructs a
sacrificial ledger so the window is CLOSED whatever ran before; every REPAIR
assertion runs in a fresh child process (``_children`` from the DBLOCK file,
which reaps every child by process group on every path).
"""

from __future__ import annotations

import contextlib
import fcntl
import gc
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import sys
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from chronos.domain.enums import OrderSide
from chronos.execution import sqlite_ledger as ledger_module
from chronos.execution.intents import IntentStatus, OrderIntent, TimeInForce
from chronos.execution.sqlite_ledger import SqliteLedger
from tests.safety.test_database_lock_integrity import (
    CHILD_S,
    _children,
    _db_rows,
    _lock_rows,
    _OpenSpy,
    _run_child,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="POSIX lock rows via /proc/locks")

SIDE = ("-wal", "-shm", "-journal")
_RESTART = "restart the process to repair"
_NOW = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)
_TEMP_NAME = re.compile(r"^\.(?P<db>.+)\.create-[0-9a-f]{16}$")


def _intent() -> OrderIntent:
    return OrderIntent(
        strategy_id="strat_a",
        strategy_version="1",
        symbol="SPY",
        side=OrderSide.BUY,
        quantity=5,
        limit_price=Decimal("500.10"),
        stop_price=Decimal("485.00"),
        time_in_force=TimeInForce.DAY,
        decision_timestamp_utc=_NOW,
        source_bar_sequence_id="test:SPY:1d:2024-06-03",
        proposal_reason="test",
    )


def _namespace(path: Path) -> tuple[Path, ...]:
    return (path, *(Path(f"{path}{suffix}") for suffix in SIDE))


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _ledger_file(path: Path) -> None:
    """An existing ledger file (schema written), no connection left open on it."""

    SqliteLedger(path).close()


def _close_window(tmp_path: Path) -> None:
    """A sacrificial ledger: afterwards the repair window is CLOSED, whatever ran before."""

    _ledger_file(tmp_path / "sacrificial" / "s.db")
    assert getattr(ledger_module, "_REPAIR_WINDOW_OPEN", False) is False


def _fds_on_inode(path: Path) -> int:
    """How many of THIS process's descriptors refer to the file's inode (any name)."""

    target = os.stat(path)
    count = 0
    for name in os.listdir("/proc/self/fd"):
        try:
            found = os.stat(f"/proc/self/fd/{name}")
        except OSError:
            continue
        if (found.st_dev, found.st_ino) == (target.st_dev, target.st_ino):
            count += 1
    return count


class _ConnectSpy:
    """Counts sqlite3.connect calls whose database argument names the watched path."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
        self.calls = 0
        watched = str(path)
        real = sqlite3.connect

        def spy(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
            if str(database) == watched:
                self.calls += 1
            return real(database, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", spy)


class _CreateSpy:
    """Records every os.open: (basename, flags, dir_fd). The temp is opened by a bare name."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, int, Any]] = []
        real = os.open

        def spy(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            self.calls.append((os.path.basename(str(path)), flags, kwargs.get("dir_fd")))
            return real(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", spy)

    def created(self) -> list[str]:
        return [name for name, flags, _ in self.calls if flags & os.O_CREAT]


def _hook(monkeypatch: pytest.MonkeyPatch, hook: Any) -> None:
    # raising=False: the seam does not exist at the pre-fix head. Every test that installs a
    # hook asserts the step FIRED, so at that head it fails on behaviour, not on a missing name.
    monkeypatch.setattr(ledger_module, "_ADMISSION_STEP_HOOK", hook, raising=False)


# ------------------------------------------------------------------ 1: the oracle itself


def test_c1_01_the_proc_locks_oracle_sees_a_known_ledger_lock(tmp_path: Path) -> None:
    """Positive control: if the parser cannot see a lock we took, FAIL (never a vacuous pin)."""

    target = tmp_path / "control.lock"
    with open(target, "w") as handle:
        fcntl.lockf(handle, fcntl.LOCK_EX)
        rows = _lock_rows(target)
    assert rows, "the /proc/locks oracle cannot see this process's own lock; the pins are blind"


# ------------------------------------------------------------------ 2, 7: lock rows survive


def test_c1_02_construction_keeps_the_ledgers_wal_locks(tmp_path: Path) -> None:
    """The constructor's own schema commit puts the WAL in use; its locks must still be there."""

    path = tmp_path / "ledger.db"
    ledger = SqliteLedger(path)
    try:
        rows = _db_rows(path)
        assert rows, "no lock rows after construction: the constructor dropped its own locks"
        ledger.record_intent(_intent(), IntentStatus.PENDING_SUBMISSION)
        assert _db_rows(path) == rows
    finally:
        ledger.close()


def test_c1_07_a_second_in_process_ledger_on_the_same_path_keeps_the_firsts_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sonnet r0 P3-1: the rows must be PRESENT first (at the pre-fix head they are already
    gone after the first's own construction), then a second instance opens nothing by path."""

    path = tmp_path / "ledger.db"
    first = SqliteLedger(path)
    try:
        first.record_intent(_intent(), IntentStatus.PENDING_SUBMISSION)
        rows = _db_rows(path)
        assert rows, "the first instance holds no lock rows: its own construction dropped them"
        spy = _OpenSpy(monkeypatch, *_namespace(path))
        second = SqliteLedger(path)
        second.close()
        assert spy.hits == [], spy.hits
        assert set(rows) <= set(_db_rows(path))
    finally:
        first.close()


# ------------------------------------------------------------------ 3, 4, 5: creation


def test_c1_03_a_new_ledger_is_created_unpublished_and_closed_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daybreak P1-1: no descriptor of the PUBLISHED name is ever opened or closed here. The file
    is created under `.<name>.create-<hex>`, closed, then linked into place (no-replace)."""

    path = tmp_path / "new.db"
    seen: dict[str, Any] = {}

    def hook(step: str) -> None:
        if step == "linked":
            seen["linked"] = {
                "exists": path.exists(),
                "nlink": path.lstat().st_nlink,
                "fds_on_inode": _fds_on_inode(path),
            }

    _hook(monkeypatch, hook)
    spy = _CreateSpy(monkeypatch)
    SqliteLedger(path).close()
    assert "linked" in seen, (
        "the constructor never published through the linked step: no unpublished-name "
        "creation exists at this head"
    )
    assert seen["linked"] == {"exists": True, "nlink": 2, "fds_on_inode": 0}, seen
    created = spy.created()
    assert path.name not in created, f"the final name was opened with O_CREAT: {spy.calls}"
    temps = [name for name in created if _TEMP_NAME.match(name)]
    match = _TEMP_NAME.match(temps[0]) if len(temps) == 1 else None
    assert match is not None and match.group("db") == path.name, created
    assert path.lstat().st_nlink == 1
    assert _mode(path) == 0o600
    assert [p.name for p in tmp_path.iterdir() if ".create-" in p.name] == []


def test_c1_04_publication_never_closes_a_descriptor_a_same_process_connection_has_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daybreak's required lock pin. At `linked` a direct same-process sqlite3 connection takes
    real locks on the just-published inode; the rest of the constructor (unlink of the temp,
    close of the directory fd, its own connect) must preserve every one of them.

    The window must be CLOSED here (sonnet r1 P2-1): with the window OPEN, step 5's repair
    through `secure_owner_only` opens and closes the names by path and DOES drop a direct
    connection's locks. That is the disclosed C1.6 residual, pinned separately below; this
    pin is about publication, not about the OPEN window."""

    _close_window(tmp_path)
    path = tmp_path / "new.db"
    seen: dict[str, Any] = {}
    direct: list[sqlite3.Connection] = []

    def hook(step: str) -> None:
        if step == "linked":
            connection = sqlite3.connect(str(path))
            direct.append(connection)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS probe(x)")
            connection.execute("INSERT INTO probe VALUES (1)")
            connection.commit()
            seen["rows"] = _db_rows(path)

    _hook(monkeypatch, hook)
    ledger = None
    try:
        ledger = SqliteLedger(path)
        assert seen.get("rows"), "the direct connection took no observable lock at publication"
        after = _db_rows(path)
        assert set(seen["rows"]) <= set(after), (seen["rows"], after)
        assert path.lstat().st_nlink == 1
    finally:
        if ledger is not None:
            ledger.close()
        for connection in direct:
            connection.close()


_OPEN_WINDOW_RESIDUAL = """
import sqlite3, sys
from pathlib import Path
from chronos.execution.sqlite_ledger import SqliteLedger
from tests.safety.test_database_lock_integrity import _db_rows
path = Path(sys.argv[1])
direct = sqlite3.connect(str(path))
direct.execute("PRAGMA journal_mode=WAL"); direct.execute("CREATE TABLE IF NOT EXISTS probe(x)")
direct.execute("INSERT INTO probe VALUES (1)"); direct.commit()
before = _db_rows(path)
SqliteLedger(path).close()  # the FIRST ledger in this process: the window is OPEN, it repairs
print(len(before), len(_db_rows(path)))
"""


def test_c1_04b_the_open_window_residual_is_as_disclosed(tmp_path: Path) -> None:
    """C1.6 (Daybreak P2-2): a direct same-process connection on the ledger's inode is OUTSIDE
    the module-private window: the first construction's repair drops ITS locks. Pinned so
    that a change of the window's scope is noticed, not so that anyone relies on it."""

    result = _run_child(_OPEN_WINDOW_RESIDUAL, str(tmp_path / "shared.db"))
    assert result.returncode == 0, result.stderr
    before, after = (int(n) for n in result.stdout.split())
    assert before > 0, "the direct connection took no lock; the residual pin is vacuous"
    assert after == 0, "the open-window repair kept the direct connection's locks: C1.6 changed"


_CREATE_CRASH = """
import os, sys
from pathlib import Path
from chronos.execution import sqlite_ledger as m
from chronos.execution.sqlite_ledger import SqliteLedger
def hook(step):
    if step == "linked":
        os._exit(7)
m._ADMISSION_STEP_HOOK = hook
SqliteLedger(Path(sys.argv[1]) / "c.db")
os._exit(0)
"""

_START = """
import sys
from pathlib import Path
from chronos.execution.sqlite_ledger import SqliteLedger
try:
    SqliteLedger(Path(sys.argv[1]) / "c.db").close()
except RuntimeError as error:
    print("refused:", error)
else:
    print("ok")
"""


def test_c1_05_a_crash_between_link_and_unlink_is_refused_naming_the_exact_stray(
    tmp_path: Path,
) -> None:
    """The crash residue gives the ledger a SECOND name (two WAL namespaces for one file). It is
    refused fail-closed, naming the exact private temp; nothing deletes it automatically."""

    crashed = _run_child(_CREATE_CRASH, str(tmp_path))
    assert crashed.returncode == 7, (
        "the child never reached the linked step (no unpublished-name creation at this head)",
        crashed.returncode,
        crashed.stderr,
    )
    strays = [p for p in tmp_path.iterdir() if ".create-" in p.name]
    assert len(strays) == 1
    assert (tmp_path / "c.db").stat().st_nlink == 2
    refused = _run_child(_START, str(tmp_path))
    assert refused.returncode == 0, refused.stderr
    assert refused.stdout.startswith("refused:"), refused.stdout
    assert str(strays[0]) in refused.stdout, refused.stdout  # the exact private temp
    assert strays[0].exists(), "the stray was removed automatically"
    strays[0].unlink()  # the operator's step
    restarted = _run_child(_START, str(tmp_path))
    assert restarted.returncode == 0, restarted.stderr
    assert restarted.stdout.strip() == "ok"


# ------------------------------------------------------------------ 6, 10: CLOSED admission


@pytest.mark.parametrize("mode", [0o400, 0o200])
def test_c1_06_a_closed_window_refuses_owner_read_only_and_write_only_files_at_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    """Daybreak P2-1: the owner bits are checked up front (DBLOCK 81b83f8's predicate), not
    left for SQLite to fail late with 'readonly database' / 'unable to open database file'."""

    _close_window(tmp_path)
    path = tmp_path / "ledger.db"
    _ledger_file(path)
    path.chmod(mode)
    spy = _ConnectSpy(monkeypatch, path)
    with pytest.raises(RuntimeError) as refused:
        SqliteLedger(path)
    assert f"unsafe mode {mode:04o}" in str(refused.value), str(refused.value)
    assert _RESTART in str(refused.value)
    assert spy.calls == 0, "sqlite3.connect was called before the refusal"
    assert _mode(path) == mode, "the mode was changed by a refusing constructor"


@pytest.mark.parametrize("mode", [0o640, 0o660, 0o604], ids=["0640", "0660", "0604"])
def test_m1_a_closed_window_refuses_group_or_other_bits_on_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    """M1-GAPS gap 3 (R-21, the ledger): once the window is closed ANY group or other bit is
    refused before sqlite3.connect, not only the world bits other fixtures set."""

    _close_window(tmp_path)
    path = tmp_path / "ledger.db"
    _ledger_file(path)
    path.chmod(mode)
    spy = _ConnectSpy(monkeypatch, path)
    with pytest.raises(RuntimeError) as refused:
        SqliteLedger(path)
    assert f"unsafe mode {mode:04o}" in str(refused.value), str(refused.value)
    assert _RESTART in str(refused.value)
    assert spy.calls == 0, "sqlite3.connect was called before the refusal"
    assert _mode(path) == mode, "a closed window repaired a file"


_OPEN_WINDOW_GROUP_BITS = """
import os, stat, sys
from pathlib import Path
from chronos.execution.sqlite_ledger import SqliteLedger
p = Path(sys.argv[1]) / "g.db"
SqliteLedger(p).close()  # the first construction: the window is open, then closes
"""


def test_m1_the_open_window_tightens_group_bits_on_the_ledger_to_owner_only(
    tmp_path: Path,
) -> None:
    """M1-GAPS gap 3, the OPEN half: a pre-existing 0640 ledger is repaired to 0600 by the first
    construction in a fresh process."""

    path = tmp_path / "g.db"
    _ledger_file(path)  # created (in THIS process, whose window is already closed) at 0600
    path.chmod(0o640)
    result = _run_child(_OPEN_WINDOW_GROUP_BITS, str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert _mode(path) == 0o600


def test_c1_06b_a_closed_window_accepts_an_owner_only_file(tmp_path: Path) -> None:
    _close_window(tmp_path)
    path = tmp_path / "ledger.db"
    _ledger_file(path)
    assert _mode(path) == 0o600
    SqliteLedger(path).close()


def test_c1_10_a_drifted_mode_after_the_window_closed_is_refused_and_never_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _close_window(tmp_path)
    path = tmp_path / "ledger.db"
    _ledger_file(path)
    path.chmod(0o644)
    spy = _OpenSpy(monkeypatch, *_namespace(path))
    with pytest.raises(RuntimeError) as refused:
        SqliteLedger(path)
    assert "unsafe mode 0644" in str(refused.value) and _RESTART in str(refused.value)
    assert spy.hits == [], spy.hits
    assert _mode(path) == 0o644, "a CLOSED window repaired the file"


# ------------------------------------------------------------------ 8, 9: the WAL-loss class

_WALLOSS_B = """
import os, sys, time
from datetime import UTC, datetime
from pathlib import Path
from chronos.execution.intents import IntentStatus
from chronos.execution.sqlite_ledger import SqliteLedger
path, d = Path(sys.argv[1]), Path(sys.argv[2])
ledger = SqliteLedger(path)
acks = open(d / "acks", "a")
def commit(i):
    ledger.record_transition(f"intent-{i}", IntentStatus.SUBMITTED, datetime.now(UTC), "sent")
    acks.write(f"{i}\\n"); acks.flush(); os.fsync(acks.fileno())
for i in range(1, 21):
    commit(i)
(d / "b.ready").touch()
deadline = time.monotonic() + 30
while not (d / "a.closed").exists():
    assert time.monotonic() < deadline, "A never closed"
    time.sleep(0.01)
for i in range(21, 41):
    commit(i)
(d / "b.done").touch()
time.sleep(120)
"""

_WALLOSS_A_STDLIB = """
import json, os, sqlite3, sys, time
from pathlib import Path
path, d = sys.argv[1], Path(sys.argv[2])
wal = Path(path + "-wal")
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
connection = sqlite3.connect(path)  # a stock read-write client, as the dashboard or a shell
connection.execute("SELECT count(*) FROM transitions").fetchone()
connection.close()
after = ident()
(d / "a.json").write_text(json.dumps({"before": before, "after": after}))
(d / "a.closed").touch()
"""

_WALLOSS_A_CLI = """
import json, os, subprocess, sys, time
from pathlib import Path
path, d = sys.argv[1], Path(sys.argv[2])
wal = Path(path + "-wal")
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
# docs/BACKUP_AND_RECOVERY.md: the documented "online, works while a process is running" backup
subprocess.run(["sqlite3", path, f".backup '{d / 'backup.db'}'"], check=True)
after = ident()
(d / "a.json").write_text(json.dumps({"before": before, "after": after}))
(d / "a.closed").touch()
"""


def _walloss(tmp_path: Path, a_code: str) -> None:
    path = tmp_path / "platform_ledger.db"
    with _children() as children:
        b = children.popen(_WALLOSS_B, str(path), str(tmp_path))
        a = children.run(a_code, str(path), str(tmp_path))
        if a.returncode != 0:
            os.killpg(b.pid, signal.SIGKILL)
            _out, b_err = b.communicate(timeout=CHILD_S)
            pytest.fail(f"A failed: {a.stderr}\nB stderr: {b_err}")
        deadline = time.monotonic() + CHILD_S
        while not (tmp_path / "b.done").exists():
            assert b.poll() is None, b.communicate()[1]
            assert time.monotonic() < deadline, "B never finished its post-close commits"
            time.sleep(0.01)
        os.killpg(b.pid, signal.SIGKILL)  # B "dies" here (the crash under test)
        b.communicate(timeout=CHILD_S)
    wal = json.loads((tmp_path / "a.json").read_text())
    acked = [int(line) for line in (tmp_path / "acks").read_text().split()]
    with contextlib.closing(sqlite3.connect(str(path))) as check:
        present = check.execute("SELECT count(*) FROM transitions").fetchone()[0]
    assert wal["before"] is not None
    assert wal["after"] is not None, f"A's close deleted the live WAL under the ledger: {wal}"
    assert wal["after"][0] == wal["before"][0], wal
    assert len(acked) == 40
    assert present == len(acked), f"{len(acked) - present} acknowledged ledger commits lost"


def test_c1_09_a_second_process_closing_does_not_delete_the_wal_under_a_live_ledger(
    tmp_path: Path,
) -> None:
    """The reader's ledger-ledloss.py, barrier-driven and deterministic: B (a real SqliteLedger)
    commits and acknowledges rows; A, a stock read-write sqlite3 client in another process,
    opens and closes mid-run; B commits more, then dies (SIGKILL). Every acknowledged row must
    survive and A's close must not delete the WAL B is still writing."""

    _walloss(tmp_path, _WALLOSS_A_STDLIB)


def test_c1_08_the_documented_backup_command_does_not_delete_the_wal_under_a_live_ledger(
    tmp_path: Path,
) -> None:
    """Same, with A = the documented online backup (docs/BACKUP_AND_RECOVERY.md:86). FAILS, never
    skips, when the CLI is absent: that command is the realistic trigger under test."""

    assert shutil.which("sqlite3"), (
        "the sqlite3 CLI is required for this pin (apt-get install sqlite3): the documented "
        "backup command is the trigger under test"
    )
    _walloss(tmp_path, _WALLOSS_A_CLI)


# ------------------------------------------------------------------ 11: the window is one-way

_ONE_WAY = """
import json, os, sqlite3, sys
from pathlib import Path
from chronos.execution import sqlite_ledger as m
from chronos.execution.sqlite_ledger import SqliteLedger
d = Path(sys.argv[1]); report = {}
RESTART = "restart the process to repair"
def mode(p): return p.stat().st_mode & 0o777
def plant(p, planted_mode):
    c = sqlite3.connect(str(p)); c.execute("PRAGMA journal_mode=WAL"); c.executescript(m._SCHEMA)
    c.execute("INSERT INTO schema_info (version) VALUES (?)", (m._SCHEMA_VERSION,)); c.commit()
    c.close()
    for s in ("-wal", "-shm", "-journal"):
        Path(f"{p}{s}").touch()
    for q in (p, *(Path(f"{p}{s}") for s in ("-wal", "-shm", "-journal"))):
        q.chmod(planted_mode)
p1 = d / "p1.db"; plant(p1, 0o666)
report["window_before"] = getattr(m, "_REPAIR_WINDOW_OPEN", "absent")
first = SqliteLedger(p1)  # the window is OPEN: repaired
names = [p1, *(Path(f"{p1}{s}") for s in ("-wal", "-shm", "-journal"))]
report["repaired"] = all(mode(q) == 0o600 for q in names if q.exists())
report["window_after"] = getattr(m, "_REPAIR_WINDOW_OPEN", "absent")
first.close()
j = Path(f"{p1}-journal"); j.touch(); j.chmod(0o666)
try:
    SqliteLedger(p1).close(); report["same_path_after_close"] = "constructed"
except RuntimeError as e:
    report["same_path_after_close"] = "refused" if RESTART in str(e) else str(e)
p2 = d / "p2.db"; plant(p2, 0o644)
try:
    SqliteLedger(p2).close(); report["new_path_drifted"] = "constructed"
except RuntimeError as e:
    report["new_path_drifted"] = "refused" if RESTART in str(e) else str(e)
opened = []
real = os.open
def spy(path, *a, **k):
    opened.append(os.path.basename(str(path)))
    return real(path, *a, **k)
os.open = spy
p3 = d / "p3.db"
SqliteLedger(p3).close()
os.open = real
report["p3_mode"] = oct(mode(p3))
report["p3_nlink"] = p3.stat().st_nlink
report["p3_opened_by_name"] = [n for n in opened if n == "p3.db" or n.startswith("p3.db-")]
report["p3_temp_opened"] = any(n.startswith(".p3.db.create-") for n in opened)
report["strays"] = sorted(n.name for n in d.iterdir() if ".create-" in n.name)
print(json.dumps(report))
"""

_FAILED_FIRST = """
import sqlite3, sys
from pathlib import Path
from chronos.execution import sqlite_ledger as m
from chronos.execution.sqlite_ledger import SqliteLedger
d = Path(sys.argv[1])
a = d / "a.db"
c = sqlite3.connect(str(a)); c.execute("PRAGMA journal_mode=WAL"); c.executescript(m._SCHEMA)
c.execute("INSERT INTO schema_info (version) VALUES (99)"); c.commit(); c.close()
a.chmod(0o600)
try:
    SqliteLedger(a)
except RuntimeError as e:
    assert "schema version 99" in str(e), e
print(getattr(m, "_REPAIR_WINDOW_OPEN", "absent"))
b = d / "b.db"; b.touch(); b.chmod(0o666)
try:
    SqliteLedger(b).close(); print("constructed")
except RuntimeError as e:
    print("refused" if "restart the process to repair" in str(e) else str(e))
"""


def test_c1_11_the_repair_window_is_one_way(tmp_path: Path) -> None:
    result = _run_child(_ONE_WAY, str(tmp_path))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip())
    assert report["window_before"] is True, report
    assert report["repaired"] is True, report
    assert report["window_after"] is False, report
    assert report["same_path_after_close"] == "refused", report
    assert report["new_path_drifted"] == "refused", report
    assert report["p3_mode"] == "0o600", report
    assert report["p3_nlink"] == 1, report
    assert report["p3_temp_opened"] is True, report
    assert report["p3_opened_by_name"] == [], report
    assert report["strays"] == [], report


def test_c1_11b_a_failed_first_construction_still_closes_the_window(tmp_path: Path) -> None:
    result = _run_child(_FAILED_FIRST, str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["False", "refused"], result.stdout


_REFUSED_IN_ADMISSION = """
import json, sqlite3, sys
from pathlib import Path
from chronos.execution import sqlite_ledger as m
from chronos.execution.sqlite_ledger import SqliteLedger
d = Path(sys.argv[1])
reached = []
real = sqlite3.connect
def spy(*a, **k):
    reached.append(1)
    return real(*a, **k)
sqlite3.connect = spy
(d / "target.db").touch()
(d / "a.db").symlink_to(d / "target.db")
first = ""
try:
    SqliteLedger(d / "a.db").close(); first = "constructed"
except RuntimeError as e:
    first = str(e)
report = {"first": first, "connect_reached": bool(reached), "window_open": m._REPAIR_WINDOW_OPEN}
b = d / "b.db"; b.touch(); b.chmod(0o666)
try:
    SqliteLedger(b).close(); report["second"] = "constructed"
except RuntimeError as e:
    report["second"] = "refused" if "restart the process to repair" in str(e) else str(e)
print(json.dumps(report))
"""


def test_m1_a_first_ledger_refused_inside_admission_still_closes_the_window(
    tmp_path: Path,
) -> None:
    """M1-GAPS gap 5: the window closes even when the FIRST construction is refused INSIDE
    admission (a symlink at the ledger path, before any sqlite3.connect)."""

    result = _run_child(_REFUSED_IN_ADMISSION, str(tmp_path))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip())
    assert "symbolic" in report["first"].lower() or "symlink" in report["first"].lower(), report
    assert report["connect_reached"] is False, report
    assert report["window_open"] is False, report
    assert report["second"] == "refused", report


# ------------------------------------------------------------------ 12, 13: identity before SQLite


@pytest.mark.parametrize("linked", ["db", "wal"])
def test_c1_12_a_hard_linked_ledger_or_sidecar_is_refused_before_any_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked: str
) -> None:
    """SQLite keys -wal/-shm by PATHNAME: a second name for one inode forks the database."""

    path = tmp_path / "ledger.db"
    _ledger_file(path)
    if linked == "db":
        target = tmp_path / "alias.db"
        os.link(path, target)
        subject, named = target, "alias.db"
    else:
        wal = Path(f"{path}-wal")
        wal.touch(mode=0o600)
        os.link(wal, tmp_path / "second-name")
        subject, named = path, "ledger.db-wal"
    spy = _ConnectSpy(monkeypatch, subject)
    with pytest.raises(RuntimeError) as refused:
        SqliteLedger(subject)
    assert "2 hard links" in str(refused.value) and named in str(refused.value), str(refused.value)
    assert spy.calls == 0, "sqlite3.connect ran before the identity refusal"
    assert not Path(f"{subject}-shm").exists()


def test_c1_13_a_symlinked_ledger_path_is_refused_before_sqlite_follows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "real.db"
    link = tmp_path / "link.db"
    link.symlink_to(target)
    spy = _ConnectSpy(monkeypatch, link)
    with pytest.raises(RuntimeError, match="symbolic-link"):
        SqliteLedger(link)
    assert spy.calls == 0, "sqlite3.connect ran before the identity refusal"
    assert not target.exists(), "SQLite followed the link and created the target"
    assert not Path(f"{target}-wal").exists()


# ------------------------------------------------------------------ 14, 15: post-connect boundary


@pytest.mark.parametrize("failure", ["schema_version", "not_a_database"])
def test_c1_14_a_failed_constructor_leaves_no_connection_and_no_lock(
    tmp_path: Path, failure: str
) -> None:
    """Daybreak P2-3 / sonnet P3-5: at the pre-fix head the schema-version refusal (and a
    non-SQLite file) left the connection and its locks alive while the exception lived."""

    path = tmp_path / "ledger.db"
    expected: type[BaseException]
    if failure == "schema_version":
        _ledger_file(path)
        with contextlib.closing(sqlite3.connect(str(path))) as connection:
            connection.execute("UPDATE schema_info SET version = 99")
            connection.commit()
        expected = RuntimeError
    else:
        path.write_bytes(b"not a database\n" * 64)
        path.chmod(0o600)
        expected = sqlite3.DatabaseError
    gc.disable()  # sonnet r1 P3-2: a collection could close a leaked connection vacuously
    try:
        with pytest.raises(expected) as excinfo:
            SqliteLedger(path)
        held = excinfo.value  # the traceback (and any leaked local) stays referenced
        assert _fds_on_inode(path) == 0, "a descriptor of the ledger survived the failure"
        assert _db_rows(path) == [], "lock rows survived the failed constructor"
        del held
    finally:
        gc.enable()
    assert getattr(ledger_module, "_REPAIR_WINDOW_OPEN", False) is False


def test_c1_15_a_post_connect_drift_is_refused_and_the_connection_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the connection exists the check is lstat-only and still refuses: a sidecar that
    appears with a wide mode between admission and verification closes the connection."""

    path = tmp_path / "ledger.db"
    seen: dict[str, bool] = {}

    def hook(step: str) -> None:
        if step == "connected":
            journal = Path(f"{path}-journal")
            journal.touch()
            journal.chmod(0o644)
            seen["connected"] = True

    _hook(monkeypatch, hook)
    with pytest.raises(RuntimeError) as refused:
        SqliteLedger(path)
    assert seen.get("connected"), "the connected step never fired: no post-connect check exists"
    assert "unsafe mode 0644" in str(refused.value) and _RESTART in str(refused.value)
    assert _fds_on_inode(path) == 0
    assert _db_rows(path) == []


# ------------------------------------------------------------------ 16, 17: premise, fork


@pytest.mark.parametrize("umask", [0o022, 0o000, 0o077])
def test_c1_16_sqlite_creates_the_ledgers_sidecars_with_the_database_mode(
    tmp_path: Path, umask: int
) -> None:
    """The premise the post-connect lstat check relies on: a 0600 ledger gives 0600 sidecars
    whatever the umask (GREEN at the pre-fix head too, by its post-connect chmod)."""

    result = _run_child(
        """
        import os, sys
        from datetime import UTC, datetime
        from pathlib import Path
        from chronos.execution.intents import IntentStatus
        from chronos.execution.sqlite_ledger import SqliteLedger
        os.umask(int(sys.argv[2])); path = Path(sys.argv[1])
        ledger = SqliteLedger(path)
        ledger.record_transition("intent-1", IntentStatus.SUBMITTED, datetime.now(UTC), "sent")
        def mode(p): return oct(os.stat(p).st_mode & 0o777)
        print(mode(path), mode(f"{path}-wal"), mode(f"{path}-shm"))
        ledger.close()
        """,
        str(tmp_path / "x.db"),
        str(umask),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["0o600", "0o600", "0o600"], result.stdout


def test_c1_17_a_fork_while_a_sibling_holds_the_window_lock_does_not_strand_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DBLOCK P2-NEW-3 shape: the forked child must not inherit a lock held by a thread it does
    not have; the at-fork reset gives it a fresh lock, the window STATE is inherited."""

    _close_window(tmp_path)
    live = tmp_path / "live.db"
    holder = SqliteLedger(live)
    admitted = tmp_path / "admitted.db"
    _ledger_file(admitted)
    inside = threading.Event()
    release = threading.Event()

    def hook(step: str) -> None:
        if step == "decided" and threading.current_thread().name == "sibling":
            inside.set()
            release.wait(10)

    _hook(monkeypatch, hook)
    sibling = threading.Thread(target=lambda: _ledger_file(tmp_path / "sib.db"), name="sibling")
    pid = 0
    child_reaped = False
    try:
        holder.record_intent(_intent(), IntentStatus.PENDING_SUBMISSION)
        rows = _db_rows(live)
        assert rows
        sibling.start()
        assert inside.wait(10), "the sibling never entered the window's critical section"
        pid = os.fork()
        if pid == 0:  # the child: its own process group, a bounded time, then exit
            os.setpgid(0, 0)
            signal.alarm(10)
            try:
                SqliteLedger(admitted).close()
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
        if pid and not child_reaped:  # never leave the forked child behind
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
        release.set()
        if sibling.ident is not None:
            sibling.join(10)
        holder.close()


def test_c1_18_a_relative_path_is_bound_before_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cwd change at connect cannot redirect the path admitted by the constructor."""

    admitted_root = tmp_path / "admitted"
    switched_root = tmp_path / "switched"
    admitted_root.mkdir()
    switched_root.mkdir()
    switched_path = switched_root / "ledger.db"
    switched_path.touch(mode=0o600)
    switched_path.chmod(0o600)
    original_cwd = Path.cwd()
    real_connect = sqlite3.connect

    def switch_cwd_then_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        os.chdir(switched_root)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(ledger_module.sqlite3, "connect", switch_cwd_then_connect)
    os.chdir(admitted_root)
    ledger = None
    try:
        ledger = SqliteLedger(Path("ledger.db"))
        connected_path = Path(
            ledger._connection.execute("PRAGMA database_list").fetchone()[2]
        ).resolve()
        assert connected_path == (admitted_root / "ledger.db").resolve()
        assert connected_path != switched_path.resolve()
    finally:
        if ledger is not None:
            ledger.close()
        os.chdir(original_cwd)
