"""Single-writer lease: contention, expiry takeover, renewal loss."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from chronos.persistence.database import Database
from chronos.utils.locking import WriterLease

T0 = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'lease.db'}")
    database.initialize()
    return database


def test_first_acquire_wins_second_loses(tmp_path: Path) -> None:
    database = _database(tmp_path)
    first = WriterLease(database.sessions, holder="one")
    second = WriterLease(database.sessions, holder="two")
    assert first.acquire(now=T0) is True
    assert second.acquire(now=T0) is False
    state = second.state(now=T0)
    assert state.held is True
    assert state.holder == "one"
    database.dispose()


def test_expired_lease_can_be_taken_over(tmp_path: Path) -> None:
    database = _database(tmp_path)
    first = WriterLease(database.sessions, holder="one", ttl=timedelta(seconds=30))
    second = WriterLease(database.sessions, holder="two", ttl=timedelta(seconds=30))
    assert first.acquire(now=T0) is True
    later = T0 + timedelta(seconds=31)
    assert second.acquire(now=later) is True
    assert second.state(now=later).holder == "two"
    # The original holder has lost the lease: renewal must fail.
    assert first.renew(now=later) is False
    database.dispose()


def test_renewal_extends_and_release_frees(tmp_path: Path) -> None:
    database = _database(tmp_path)
    first = WriterLease(database.sessions, holder="one", ttl=timedelta(seconds=30))
    assert first.acquire(now=T0) is True
    assert first.renew(now=T0 + timedelta(seconds=20)) is True
    # Renewed at +20s with 30s ttl: still held at +45s.
    assert first.state(now=T0 + timedelta(seconds=45)).held is True
    first.release()
    second = WriterLease(database.sessions, holder="two")
    assert second.acquire(now=T0 + timedelta(seconds=46)) is True
    database.dispose()


def test_reacquire_by_same_instance_is_idempotent(tmp_path: Path) -> None:
    database = _database(tmp_path)
    lease = WriterLease(database.sessions, holder="one")
    assert lease.acquire(now=T0) is True
    assert lease.acquire(now=T0 + timedelta(seconds=1)) is True
    database.dispose()
