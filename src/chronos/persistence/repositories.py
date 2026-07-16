"""Small repositories that keep SQLAlchemy details out of services."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from chronos.persistence.schema import ApplicationEventRow


class ApplicationEventRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def append(
        self,
        *,
        event_type: str,
        message: str,
        severity: str = "INFO",
        correlation_id: str | None = None,
        symbol: str | None = None,
        event_data: dict[str, Any] | None = None,
    ) -> int:
        with self._sessions.begin() as session:
            row = ApplicationEventRow(
                event_type=event_type,
                severity=severity,
                correlation_id=correlation_id,
                symbol=symbol,
                message=message,
                event_data=event_data or {},
            )
            session.add(row)
            session.flush()
            return row.id

    def recent(self, *, limit: int = 100) -> Sequence[ApplicationEventRow]:
        with self._sessions() as session:
            statement = (
                select(ApplicationEventRow)
                .order_by(ApplicationEventRow.occurred_at.desc())
                .limit(limit)
            )
            return tuple(session.scalars(statement))
