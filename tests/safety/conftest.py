"""Shared fixtures for the supervisor safety suites.

## Which backend a test runs on, and why it is a per-test decision

`:memory:` sqlite gives every session on one `Database` the *same* connection
(`StaticPool`, `persistence/database.py:100`). That is invisible to almost every
test here and it is not a defect to be swept away: an audit ran all four
supervisor safety files on both backends at two heads and **no test changed
result** (160/160, 164/164 — `shared/handoffs/2026-09-04_chronos-memory-sqlite-audit-fable2.md`,
issue #154). Those suites take one session per test and drive a cycle through
it; a single connection's commit and rollback behave identically either way.

So the rule is by **subject**, not by file:

- the suite's verdict does not depend on transaction topology → `:memory:`,
  and it stays fast;
- the test's *subject* **is** transaction topology — two sessions on one
  database, a commit inside a cycle, a rollback after a partial commit,
  write-lock contention, crash survival → :func:`file_sessions`.

Applying the file everywhere would slow 183 tests that measurably do not need
it, and would spend the fixture's signal: a fixture used everywhere stops saying
anything about the test that asks for it.

`tests/safety/test_sqlite_transaction_topology.py` holds the executable
statement of what the two backends actually differ on.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from chronos.persistence.database import Database


@pytest.fixture
def file_sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A FILE-backed database: the topology production actually runs.

    Be precise about what this buys, because the obvious claim is wrong. The
    ADR-0052 crash test's verdict is the SAME on both pools — measured four
    ways, file and ``:memory:`` crossed with the commit present and absent, and
    the reservation survives in both and dies in both. It has to be: ADR-0052
    commits the cycle's OWN session, so no second connection is ever involved
    and ``StaticPool`` has nothing to hide.

    What ``StaticPool`` does hide is the failure of the designs this one
    replaced. It gives an in-memory URL one connection shared by every session,
    so a *second* session's commit adopts the first's pending writes — which is
    exactly how a reserve-from-another-session shape appears to work in tests
    while deadlocking against SQLite's single writer in production.

    So this fixture is not what makes those tests valid; it is what keeps them
    honest if the implementation ever moves back toward a second connection.
    A durability test should run the durability configuration (WAL,
    ``synchronous=FULL``) rather than one chosen for speed.
    """

    database = Database(f"sqlite+pysqlite:///{tmp_path / 'chronos.db'}")
    database.initialize()
    try:
        yield database.sessions
    finally:
        database.dispose()
