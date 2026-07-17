"""Single-writer lease: at most one Chronos backend may write orders.

The lease is a single row in the Chronos SQLite database (durable across
crashes, visible to every process sharing the DATABASE_URL). A writer
acquires the row with a random token and renews it on a heartbeat; a second
process finds a live lease and must start **read-only**. A crashed writer's
lease expires after ``ttl`` and can be taken over.

Fail-closed rules:

- Acquisition and renewal are single-UPDATE compare-and-swap operations on
  the token, so two processes cannot both hold a valid lease.
- ``renew()`` returning False means the lease was lost (expired and taken,
  or clock trouble): the caller must treat itself as read-only immediately.
- ``release()`` only clears the row when the token still matches.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_TTL = timedelta(seconds=30)

# Kept byte-compatible with persistence.schema.WriterLeaseRow (the canonical
# definition used by create_all/migrations/drift checks); this DDL is only a
# safety net for databases created before the table existed. Single-row use
# (id = 1) is enforced by the queries below, not a CHECK constraint.
_TABLE_DDL = text(
    """
    CREATE TABLE IF NOT EXISTS writer_lease (
        id INTEGER NOT NULL PRIMARY KEY,
        token TEXT NOT NULL,
        holder TEXT NOT NULL,
        acquired_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """
)


@dataclass(frozen=True, slots=True)
class LeaseState:
    held: bool
    holder: str
    expires_at: datetime | None


class WriterLease:
    """Durable single-writer lease over the shared SQLite database."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        holder: str,
        ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")
        self._sessions = sessions
        self._holder = holder
        self._ttl = ttl
        self._token = secrets.token_hex(16)
        with self._sessions() as session:
            session.execute(_TABLE_DDL)
            session.commit()

    @property
    def holder(self) -> str:
        return self._holder

    def acquire(self, *, now: datetime | None = None) -> bool:
        """Try to take the lease. Returns False when a live lease exists."""

        current = now or datetime.now(tz=UTC)
        expires = current + self._ttl
        with self._sessions() as session:
            row = session.execute(
                text("SELECT token, expires_at FROM writer_lease WHERE id = 1")
            ).first()
            if row is None:
                session.execute(
                    text(
                        "INSERT INTO writer_lease (id, token, holder, acquired_at, expires_at) "
                        "VALUES (1, :token, :holder, :acquired, :expires)"
                    ),
                    {
                        "token": self._token,
                        "holder": self._holder,
                        "acquired": current.isoformat(),
                        "expires": expires.isoformat(),
                    },
                )
                session.commit()
                return True
            existing_expires = datetime.fromisoformat(str(row[1]))
            if str(row[0]) != self._token and existing_expires > current:
                return False  # someone else holds a live lease
            # Expired (or our own) lease: compare-and-swap takeover. The WHERE
            # clause re-checks the condition so two racers cannot both win.
            result = session.execute(
                text(
                    "UPDATE writer_lease SET token = :token, holder = :holder, "
                    "acquired_at = :acquired, expires_at = :expires "
                    "WHERE id = 1 AND (token = :token OR expires_at <= :now)"
                ),
                {
                    "token": self._token,
                    "holder": self._holder,
                    "acquired": current.isoformat(),
                    "expires": expires.isoformat(),
                    "now": current.isoformat(),
                },
            )
            session.commit()
            return bool(getattr(result, "rowcount", 0))

    def renew(self, *, now: datetime | None = None) -> bool:
        """Extend the lease. False means it was lost — go read-only."""

        current = now or datetime.now(tz=UTC)
        expires = current + self._ttl
        with self._sessions() as session:
            result = session.execute(
                text(
                    "UPDATE writer_lease SET expires_at = :expires "
                    "WHERE id = 1 AND token = :token AND expires_at > :now"
                ),
                {"expires": expires.isoformat(), "token": self._token, "now": current.isoformat()},
            )
            session.commit()
            return bool(getattr(result, "rowcount", 0))

    def release(self) -> None:
        with self._sessions() as session:
            session.execute(
                text("DELETE FROM writer_lease WHERE id = 1 AND token = :token"),
                {"token": self._token},
            )
            session.commit()

    def state(self, *, now: datetime | None = None) -> LeaseState:
        current = now or datetime.now(tz=UTC)
        with self._sessions() as session:
            row = session.execute(
                text("SELECT holder, expires_at FROM writer_lease WHERE id = 1")
            ).first()
        if row is None:
            return LeaseState(held=False, holder="", expires_at=None)
        expires = datetime.fromisoformat(str(row[1]))
        return LeaseState(held=expires > current, holder=str(row[0]), expires_at=expires)
