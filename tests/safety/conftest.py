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
from itertools import count
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from chronos.persistence.database import Database

_MEMORY_IS_THE_SUBJECT = "in_memory_sqlite_is_the_subject"


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


def _is_in_memory_sqlite(url: str) -> bool:
    """Mirror ``Database``'s own StaticPool predicate, rather than approximate it.

    ``Database`` applies ``StaticPool`` when the backend is sqlite **and** the URL
    names no database — ``configured_url.database in {None, "", ":memory:"}``
    (``persistence/database.py:100-101``). A redirect keyed on
    ``url.endswith(":memory:")`` is narrower than that: ``sqlite+pysqlite://``
    names no database either, gets ``StaticPool`` just the same, and so slipped
    through the lane entirely. Predicting the pool with a different rule than the
    one that chooses it is the whole defect; this asks the same question.

    Both halves are load-bearing, in opposite directions. Drop the database check
    and every sqlite file URL gets redirected. Drop the **backend** check and
    ``postgresql://host/`` (database ``""``) or ``postgresql+psycopg://user@host``
    (database ``None``) would be rewritten to a sqlite file — measured, both parse
    that way.

    A URL this cannot parse is not the lane's business: return ``False`` and let
    the constructor raise on it exactly as it would have. ``make_url`` raises more
    than one type — ``ArgumentError`` for ``"not a url"``, ``ValueError`` for
    ``"sqlite://:memory:"`` — and the point here is "should this be redirected",
    to which any unparseable answer is no.
    """

    try:
        configured = make_url(url)
    except Exception:
        return False
    return configured.get_backend_name() == "sqlite" and configured.database in {
        None,
        "",
        ":memory:",
    }


@pytest.fixture(autouse=True)
def _file_backed_sqlite_lane(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Under ``--file-backed-sqlite``, give every ``:memory:`` database a real file.

    Inert without the flag, which is the point: the default suite keeps the
    speed the audit justified, and CI runs the same tests a second time on the
    topology production uses. What it buys is **not** coverage of today's tests
    — measured, they do not care (185/185 unchanged, issue #154) — it is a red
    build the first time a test starts depending on ``StaticPool``'s shared
    connection without saying so. That dependency is invisible by construction:
    the test passes, and the design it implies deadlocks in production.

    Each construction gets its **own** file, because each ``:memory:`` URL is
    its own database; pointing them at one file would introduce sharing the
    original code never had and fail for a reason the lane is not about.

    Tests whose subject *is* in-memory behaviour opt out with
    ``@pytest.mark.in_memory_sqlite_is_the_subject`` — redirecting those would
    assert the opposite of what they exist to pin.
    """

    if not request.config.getoption("--file-backed-sqlite"):
        yield
        return
    if request.node.get_closest_marker(_MEMORY_IS_THE_SUBJECT):
        yield
        return

    real_init = Database.__init__
    counter = count(1)

    def _init(self: Database, url: str) -> None:
        if isinstance(url, str) and _is_in_memory_sqlite(url):
            url = f"sqlite+pysqlite:///{tmp_path / f'lane-{next(counter)}.db'}"
        real_init(self, url)

    monkeypatch.setattr(Database, "__init__", _init)
    yield
