"""The ``--file-backed-sqlite`` lane's redirect predicate (issue #154 follow-up).

The lane rewrites in-memory sqlite URLs to real files so that a test which has
started depending on ``StaticPool``'s shared connection goes red. That only works
if "is this in-memory sqlite?" is answered the **same way** ``Database`` answers
it when it chooses the pool. It was not, once: a predicate of
``url.endswith(":memory:")`` missed ``sqlite+pysqlite://``, which names no
database, gets ``StaticPool`` all the same, and so passed straight through the
lane it was supposed to trip.

These rows exist so that divergence fails here rather than by silently widening
the hole again.
"""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool

from chronos.persistence.database import Database
from tests.safety.conftest import _is_in_memory_sqlite


@pytest.mark.parametrize(
    ("url", "redirected"),
    [
        ("sqlite+pysqlite:///:memory:", True),
        ("sqlite+pysqlite://", True),  # names no database; StaticPool all the same
        ("sqlite+pysqlite:////tmp/chronos-lane.db", False),
        # The backend half, in the other direction: these name no database either,
        # and rewriting them to a sqlite file would be a spectacular way to fail.
        ("postgresql://host/", False),
        ("postgresql+psycopg://user@host", False),
        # Unparseable is not the lane's business — ArgumentError and ValueError
        # respectively — and the constructor still raises on them as it always did.
        ("not a url", False),
        ("sqlite://:memory:", False),
    ],
)
def test_the_redirect_predicate_matches_the_pool_it_predicts(url: str, redirected: bool) -> None:
    assert _is_in_memory_sqlite(url) is redirected


# The lane would redirect these very constructions, which is the point of the
# exemption marker: this test IS about the in-memory pool.
@pytest.mark.in_memory_sqlite_is_the_subject
@pytest.mark.parametrize("url", ["sqlite+pysqlite:///:memory:", "sqlite+pysqlite://"])
def test_every_url_the_predicate_claims_really_does_get_staticpool(url: str) -> None:
    """The claim above is about production behaviour, so ask production."""

    database = Database(url)
    database.initialize()
    try:
        assert isinstance(database.engine.pool, StaticPool)
    finally:
        database.dispose()
