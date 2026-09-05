"""What the two sqlite backends actually differ on, stated executably (issue #154).

ADR-0052's context paragraph explains that its rejected alternatives — reserve
the activity from a *second*, independently committed session — looked correct
under test and would deadlock in production. That explanation is prose, and
prose does not fail when it stops being true. This file is the one test that
makes it a guard.

The difference is not academic and it is not about speed. On an in-memory URL
``Database`` uses ``StaticPool`` (``persistence/database.py:100``), so every
session on that database shares **one** connection: a second session's commit
adopts the first session's pending writes, and a later rollback of the first
session cannot undo them. On a file each session gets its own connection against
SQLite's single writer, so the second writer is refused outright.

An audit measured that no existing supervisor safety test can tell the two apart
(160/160 and 164/164, zero result changes —
``shared/handoffs/2026-09-04_chronos-memory-sqlite-audit-fable2.md``). That is
why the suites stay on ``:memory:`` and why this file exists: the hazard is real,
nothing observes it, and the shape that would be fooled by it is exactly the
shape a future reservation design would reach for.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from chronos.persistence import database as database_module
from chronos.persistence.database import Database
from chronos.supervisor import durable

#: Two DIFFERENT accounts, named rather than shared, because the choice decides
#: what the in-memory branch is even asserting. With one fingerprint the second
#: session's SELECT finds the first's uncommitted row on the shared connection and
#: increments it, so the read-back is 2 and mixes "adopted a pending INSERT" with
#: "added its own increment". With two, each session inserts its own row and the
#: read-back of ``_ACCOUNT_ONE`` is exactly 1 — a row that exists only because a
#: *different* session committed, after its own session rolled back. That is the
#: hazard in its cleanest form, and it is also why the file branch below uses two:
#: the lock it hits is the database's, not the row's.
_ACCOUNT_ONE = "a" * 64
_ACCOUNT_TWO = "b" * 64
_NOW = datetime(2026, 9, 5, 14, 30, tzinfo=UTC)

#: The file branch below waits this long for the write lock instead of the
#: production 5 s (``_SQLITE_BUSY_TIMEOUT_MS``). The subject of the test is that
#: the second writer is **refused**, not how long it is willing to wait, and a
#: five-second test in a suite that runs in two is a test somebody eventually
#: deletes.
#:
#: **The patch is load-bearing, not decorative**, and it works only because of a
#: fact worth writing down: the connect listener executes
#: ``PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}`` at *connect* time
#: (``persistence/database.py:444``) and is registered with only ``file_backed``
#: bound, so patching the module global **before** the ``Database`` is
#: constructed takes effect. Measured on this shape: 5.01 s unpatched, 0.10 s
#: patched. Move the patch after construction and the wait silently returns to
#: five seconds per case.
_SHORT_BUSY_TIMEOUT_MS = 100


@pytest.fixture
def sessions_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str], sessionmaker[Session]]]:
    """Build a ``Database`` on either backend, with a short lock wait.

    ``raising=True`` (the default) is deliberate: if
    ``_SQLITE_BUSY_TIMEOUT_MS`` is ever renamed, this fails loudly here rather
    than silently reverting the file branch to a five-second wait that still
    passes.
    """

    monkeypatch.setattr(database_module, "_SQLITE_BUSY_TIMEOUT_MS", _SHORT_BUSY_TIMEOUT_MS)
    built: list[Database] = []

    def _build(backend: str) -> sessionmaker[Session]:
        url = (
            f"sqlite+pysqlite:///{tmp_path / 'topology.db'}"
            if backend == "file"
            else "sqlite+pysqlite:///:memory:"
        )
        database = Database(url)
        database.initialize()
        built.append(database)
        return database.sessions

    try:
        yield _build
    finally:
        for database in built:
            database.dispose()


def _pending_write(session: Session, account_fingerprint: str) -> None:
    """One activity write, left uncommitted on purpose.

    ``record_activity`` reaches ``_ensure_counter_row`` (called at
    ``durable.py:491``), which on a fresh database ``session.add(...)`` +
    ``flush()``es the row eagerly at ``durable.py:584-585``. The write therefore
    lands *here*, inside the call — not at the caller's commit.
    """

    durable.record_activity(
        session,
        account_fingerprint=account_fingerprint,
        now=_NOW,
        orders_submitted=1,
    )


def test_a_second_write_transaction_is_refused_on_a_file(
    sessions_for: Callable[[str], sessionmaker[Session]],
) -> None:
    """The production topology: SQLite has one writer, and says so.

    ``s1`` holds an uncommitted activity write. ``s2`` — a genuinely separate
    connection — tries to write and commit a **different** account's counters.
    It cannot: the lock is SQLite's, held against the whole database rather than
    the row, and after the (shortened, 100 ms) ``busy_timeout`` it refuses.

    The refusal lands on the **INSERT**, not on the commit — worth knowing if
    you are designing a reservation, because it means the second writer never
    gets far enough to have something to commit. The assertion spans both
    statements rather than naming one, so it stays true if a future SQLAlchemy
    defers the flush.

    This is the assertion that cannot pass on ``:memory:``. Point this test at
    an in-memory URL and both statements succeed, so the ``raises`` never fires
    — which is the whole reason the fixture above takes a backend.
    """

    sessions = sessions_for("file")
    s1 = sessions()
    s2 = sessions()
    try:
        _pending_write(s1, _ACCOUNT_ONE)
        s1.flush()  # take the write lock, exactly as a mid-cycle write does
        # DO NOT narrow this block to `s2.commit()`. Measured on a fresh
        # database, the INSERT is flushed eagerly inside `record_activity`
        # (`_ensure_counter_row`'s add/flush, durable.py:584-585), so `s2`
        # raises on the WRITE and never reaches its commit — a raises-on-commit
        # would ERROR here, not fail, and an errored test is not a pin. Spanning
        # both statements is indifferent to where the flush lands, which is what
        # a pin has to be to survive maintenance.
        with pytest.raises(OperationalError, match="database is locked"):
            _pending_write(s2, _ACCOUNT_TWO)
            s2.commit()
    finally:
        s2.rollback()
        s2.close()
        s1.rollback()
        s1.close()


@pytest.mark.in_memory_sqlite_is_the_subject
def test_on_memory_a_second_session_commits_the_first_sessions_pending_write(
    sessions_for: Callable[[str], sessionmaker[Session]],
) -> None:
    """The hazard, asserted rather than described — this is the dangerous half.

    On ``StaticPool`` there is no second connection to contend with, so ``s2``
    commits happily **and takes ``s1``'s pending write with it**. ``s1`` then
    rolls back, and the write it rolled back is still there.

    That is how a reserve-from-a-second-session design passes its tests and
    deadlocks in production, and it is why ADR-0052 rejected exactly that shape.

    **This test deliberately pins third-party behaviour** (SQLAlchemy's
    ``StaticPool`` plus SQLite). If a future version stops sharing the
    connection, this fails — and that is the correct outcome, not a bug to be
    silenced: the hazard model this suite is built around would have changed and
    somebody should be told. Do not "fix" it by deleting the assertion.

    It also pins **our own** premise first. The sharing exists because
    ``Database`` gives an in-memory URL ``StaticPool``
    (``persistence/database.py:100-101``); if that choice ever changes, the
    branch's subject dies silently and the rest of this file stops meaning
    anything. The pool assertion below is what makes "our premise moved" and
    "the library changed" fail differently.
    """

    sessions = sessions_for("memory")
    s1 = sessions()
    s2 = sessions()
    try:
        bind = s1.get_bind()
        assert isinstance(bind, Engine)
        assert isinstance(bind.pool, StaticPool), (
            "this branch's premise is OUR pool choice at persistence/database.py:100, "
            f"not SQLAlchemy's default; the engine reports {type(bind.pool).__name__}, "
            "so the sharing this test describes may no longer be what happens"
        )
        _pending_write(s1, _ACCOUNT_ONE)
        s1.flush()
        _pending_write(s2, _ACCOUNT_TWO)
        s2.commit()  # no contention: same connection
        s1.rollback()  # and this does NOT undo what s2 committed
    finally:
        s2.close()
        s1.close()

    fresh = sessions()
    try:
        # s1's OWN row, specifically: s2 wrote _ACCOUNT_TWO, so anything found
        # here arrived only because s2's commit carried s1's pending INSERT.
        counters = durable.load_counters(fresh, account_fingerprint=_ACCOUNT_ONE, now=_NOW)
        assert counters.orders_submitted == 1, (
            "the rolled-back write survived, because the second session's commit "
            "adopted it — the exact illusion that makes a second-session "
            "reservation look correct under test"
        )
    finally:
        fresh.close()


def test_the_shared_file_fixture_really_is_file_backed(
    file_sessions: sessionmaker[Session],
) -> None:
    """``file_sessions`` claims the durability configuration; this checks it.

    The fixture's docstring is explicit that it is *not* what makes the ADR-0052
    crash tests valid — their verdicts are the same on both pools, measured four
    ways — so no existing test fails if it is quietly pointed back at
    ``:memory:``. That makes its backend an unasserted claim, and unasserted
    claims are how a fixture stops doing the thing its name promises.

    ``journal_mode`` is the cheapest honest witness: a file-backed database
    reports ``wal``, and an in-memory one legitimately reports ``memory``
    (``persistence/database.py:436``).
    """

    session = file_sessions()
    try:
        journal_mode = session.execute(text("PRAGMA journal_mode")).scalar_one()
        synchronous = session.execute(text("PRAGMA synchronous")).scalar_one()
    finally:
        session.close()

    assert str(journal_mode).lower() == "wal", (
        f"file_sessions reported journal_mode={journal_mode!r}; it is no longer "
        "file-backed, so the tests that ask for it are not running the topology "
        "they say they are"
    )
    assert int(synchronous) == 2, f"expected synchronous=FULL (2), got {synchronous!r}"
