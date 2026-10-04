"""Durable SQLite order ledger for shadow/paper runs (ADR-0003).

Uses stdlib ``sqlite3`` with WAL journaling and synchronous=FULL: a ledger
write that returns has reached disk. The ledger is append-oriented: intents
are inserted once (duplicate ids violate the primary key), transitions and
fills are insert-only history. Nothing here updates or deletes rows.

The ledger file and its sidecars are secured (owner-only, one name, no
symlink) only BEFORE this process's first ledger connection; afterwards they
are only checked. See the repair window below.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import stat
import threading
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from chronos.execution.intents import IntentStatus, OrderIntent
from chronos.utils.secure_files import secure_owner_only

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    limit_price TEXT NOT NULL,
    stop_price TEXT,
    time_in_force TEXT NOT NULL,
    decision_timestamp_utc TEXT NOT NULL,
    source_bar_sequence_id TEXT NOT NULL,
    proposal_reason TEXT NOT NULL,
    initial_status TEXT NOT NULL,
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    at_utc TEXT NOT NULL,
    evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL,
    cumulative_quantity INTEGER NOT NULL,
    average_price TEXT,
    commission_usd TEXT,
    at_utc TEXT NOT NULL
);
"""

_SCHEMA_VERSION = 1
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

# >>> startup repair window (LEDGER-2; the DBLOCK shape of persistence/database.py, kept
# module-private because execution/ does not import persistence/ and ADR-0003 separates the
# platform ledger from the main database on purpose)
# POSIX record locks belong to the (process, file) pair, and close() of ANY descriptor for a
# file releases every lock the process holds on it. Re-opening the ledger or a sidecar by path
# while this process holds it through SQLite therefore silently drops that connection's WAL
# locks, and another process closing beside it can then delete the live WAL under it
# (P1-NEW-24: acknowledged commits lost). So files may be opened to be repaired only during
# the FIRST SqliteLedger construction in a process: the window closes permanently, under the
# lock, before that construction may connect, and is never reopened (not by close(), a failed
# constructor or a fork). Afterwards every check is lstat-only and refuses what it would have
# repaired: a restart repairs. A connection another component opens directly with sqlite3 (or
# a Database pointed at this path) is outside this window: the first repair would drop ITS
# locks, so no other abstraction may hold the ledger path in this process during that window.
_REPAIR_WINDOW_LOCK = threading.Lock()
_REPAIR_WINDOW_OPEN = True
_RESTART_TO_REPAIR = (
    "restart the process to repair: the startup repair window closed during this process's "
    "first SqliteLedger construction, and re-opening a ledger file it may hold through SQLite "
    "would drop that connection's locks"
)
#: Test-only seam: called as hook(step) at "decided" (the window state was read, the lock is
#: held), "linked" (a new ledger file was linked into place) and "connected" (the connection
#: exists, before the post-connect check). A no-op while unset.
_ADMISSION_STEP_HOOK: Callable[[str], None] | None = None


def _admission_step(step: str) -> None:
    hook = _ADMISSION_STEP_HOOK
    if hook is not None:
        hook(step)


def _reset_repair_window_lock_in_child() -> None:
    """A forked child gets a fresh lock; the window STATE is inherited unchanged.

    A sibling thread holding the lock at fork() does not exist in the child and could never
    release the inherited copy.
    """

    global _REPAIR_WINDOW_LOCK
    _REPAIR_WINDOW_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_repair_window_lock_in_child)
# <<< startup repair window (LEDGER-2)


class SqliteLedger:
    """LedgerPort implementation over a local SQLite file."""

    def __init__(self, path: Path) -> None:
        path = Path(os.path.abspath(path))
        _admit_ledger_files(path)
        # One post-connect boundary: whatever fails after the connection exists closes it
        # before the error propagates, so no failed constructor leaves a descriptor (and the
        # locks that go with it) behind. The connection becomes the live field only on success.
        connection = sqlite3.connect(str(path))
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(_SCHEMA)
            row = connection.execute("SELECT version FROM schema_info").fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_info (version) VALUES (?)", (_SCHEMA_VERSION,)
                )
                connection.commit()
            elif row[0] != _SCHEMA_VERSION:
                raise RuntimeError(
                    f"ledger schema version {row[0]} unsupported (expected {_SCHEMA_VERSION}); "
                    "refusing to run against an unknown schema"
                )
            _admission_step("connected")
            _verify_ledger_files(path)
        except BaseException:
            connection.close()
            raise
        self._connection = connection

    def close(self) -> None:
        self._connection.close()

    def record_intent(self, intent: OrderIntent, status: IntentStatus) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO intents (
                    intent_id, strategy_id, strategy_version, symbol, side, quantity,
                    limit_price, stop_price, time_in_force, decision_timestamp_utc,
                    source_bar_sequence_id, proposal_reason, initial_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.intent_id,
                    intent.strategy_id,
                    intent.strategy_version,
                    intent.symbol,
                    intent.side.value,
                    intent.quantity,
                    str(intent.limit_price),
                    str(intent.stop_price) if intent.stop_price is not None else None,
                    intent.time_in_force.value,
                    intent.decision_timestamp_utc.isoformat(),
                    intent.source_bar_sequence_id,
                    intent.proposal_reason,
                    status.value,
                ),
            )

    def has_intent(self, intent_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        return row is not None

    def record_transition(
        self, intent_id: str, status: IntentStatus, at_utc: datetime, evidence: str
    ) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO transitions (intent_id, status, at_utc, evidence) VALUES (?, ?, ?, ?)",
                (intent_id, status.value, at_utc.isoformat(), evidence),
            )

    def record_fill(
        self,
        intent_id: str,
        cumulative_quantity: int,
        average_price: Decimal | None,
        commission_usd: Decimal | None,
        at_utc: datetime,
    ) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO fills (intent_id, cumulative_quantity, average_price, "
                "commission_usd, at_utc) VALUES (?, ?, ?, ?, ?)",
                (
                    intent_id,
                    cumulative_quantity,
                    str(average_price) if average_price is not None else None,
                    str(commission_usd) if commission_usd is not None else None,
                    at_utc.isoformat(),
                ),
            )

    def working_intent_ids(self) -> tuple[str, ...]:
        """Intent ids whose latest transition is a working status."""

        rows = self._connection.execute(
            """
            SELECT t.intent_id, t.status FROM transitions t
            INNER JOIN (
                SELECT intent_id, MAX(id) AS max_id FROM transitions GROUP BY intent_id
            ) latest ON latest.max_id = t.id
            """
        ).fetchall()
        working = {
            IntentStatus.SUBMITTED.value,
            IntentStatus.PRE_SUBMITTED.value,
            IntentStatus.ACKNOWLEDGED.value,
            IntentStatus.PARTIALLY_FILLED.value,
            IntentStatus.PENDING_CANCEL.value,
        }
        return tuple(intent_id for intent_id, status in rows if status in working)

    def working_order_snapshots(self) -> dict[str, tuple[IntentStatus, int]]:
        """Latest (status, cumulative filled) for each working intent."""

        status_rows = self._connection.execute(
            """
            SELECT t.intent_id, t.status FROM transitions t
            INNER JOIN (
                SELECT intent_id, MAX(id) AS max_id FROM transitions GROUP BY intent_id
            ) latest ON latest.max_id = t.id
            """
        ).fetchall()
        fill_rows = self._connection.execute(
            "SELECT intent_id, MAX(cumulative_quantity) FROM fills GROUP BY intent_id"
        ).fetchall()
        filled = {intent_id: int(qty) for intent_id, qty in fill_rows if qty is not None}
        working = {
            IntentStatus.SUBMITTED.value,
            IntentStatus.PRE_SUBMITTED.value,
            IntentStatus.ACKNOWLEDGED.value,
            IntentStatus.PARTIALLY_FILLED.value,
            IntentStatus.PENDING_CANCEL.value,
        }
        return {
            intent_id: (IntentStatus(status), filled.get(intent_id, 0))
            for intent_id, status in status_rows
            if status in working
        }


def _ledger_namespace(path: Path) -> tuple[Path, ...]:
    return (path, *(path.with_name(path.name + suffix) for suffix in _SIDECAR_SUFFIXES))


def _admit_ledger_files(path: Path) -> None:
    """Prepare, check and (only while the window is open) repair, then close the window."""

    global _REPAIR_WINDOW_OPEN
    with _REPAIR_WINDOW_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _create_private_ledger_file(path)
            repair = _REPAIR_WINDOW_OPEN
            _admission_step("decided")
            namespace = _ledger_namespace(path)
            # Identity first, across every path, so a link refusal is never masked by a mode
            # refusal on another path.
            for name in namespace:
                _check_ledger_file_identity(name)
            for name in namespace:
                if repair:
                    _repair_ledger_file_mode(name)
                else:
                    _check_ledger_file_mode(name)
        finally:
            _REPAIR_WINDOW_OPEN = False


def _verify_ledger_files(path: Path) -> None:
    """lstat-only re-check after the connection exists: never opens, never repairs."""

    namespace = _ledger_namespace(path)
    for name in namespace:
        _check_ledger_file_identity(name)
    for name in namespace:
        _check_ledger_file_mode(name)


def _create_private_ledger_file(path: Path) -> None:
    """Create a missing ledger 0600 without ever closing a published name.

    A descriptor closed on the final name could belong to a file another connection in this
    process has just opened; so the file is created under an unpublished random name, closed,
    and only then linked into place (no-replace) and the temporary name removed.
    """

    try:
        os.lstat(path)
        return
    except FileNotFoundError:
        pass
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        temporary = f".{path.name}.create-{secrets.token_hex(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            _admission_step("linked")
        except FileExistsError:
            pass  # created meanwhile; checked as an existing file
        finally:
            os.unlink(temporary, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _check_ledger_file_identity(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError(f"Refusing symbolic-link ledger path: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"Refusing non-regular ledger path: {path}")
    if metadata.st_uid != os.geteuid():
        raise RuntimeError(f"Refusing ledger path not owned by this user: {path}")
    if metadata.st_nlink != 1:
        # SQLite keys a database's -wal/-shm by PATHNAME, so a second name for the same file
        # gets a second WAL namespace and acknowledged writes fork.
        stray = _interrupted_create_stray(path, metadata)
        if stray is not None:
            raise RuntimeError(
                f"Refusing ledger path with {metadata.st_nlink} hard links: {path}; an "
                f"interrupted Chronos create left the private temporary {stray} linked to "
                "it. Check that file, remove exactly it, then restart"
            )
        raise RuntimeError(
            f"Refusing ledger path with {metadata.st_nlink} hard links: {path}; a ledger "
            "file or sidecar must have exactly one name, because SQLite gives each name its "
            "own WAL files"
        )


def _interrupted_create_stray(path: Path, metadata: os.stat_result) -> Path | None:
    """The `.<name>.create-<hex>` temp of _create_private_ledger_file sharing this inode.

    Only NAMED in the refusal; never removed automatically (a name pattern is not proof the
    stray is ours).
    """

    prefix = f".{path.name}.create-"
    try:
        entries = list(os.scandir(path.parent))
    except OSError:
        return None
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        try:
            found = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if (found.st_dev, found.st_ino) == (metadata.st_dev, metadata.st_ino):
            return path.parent / entry.name
    return None


def _repair_ledger_file_mode(path: Path) -> None:
    """OPEN window only: the shared helper opens, fchmods 0600 and closes BY PATH.

    No SqliteLedger connection exists in this process yet, so no lock of ours can be dropped;
    a connection another component holds on this inode is outside the window (see above).
    """

    try:
        secure_owner_only(path)
    except PermissionError as error:
        raise RuntimeError(
            f"Refusing ledger path {path}: repair failed ({error.strerror}); "
            "chmod 600 it and restart"
        ) from error


def _check_ledger_file_mode(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o600 != 0o600 or mode & 0o077:
        raise RuntimeError(
            f"Refusing ledger path with unsafe mode {mode:04o}: {path}; {_RESTART_TO_REPAIR}"
        )
