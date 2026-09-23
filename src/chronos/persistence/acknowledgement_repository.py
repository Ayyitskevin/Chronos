"""Operator acknowledgements of positions (AP-1b): the MANUAL producer, record-only.

``chronos.cli position-acknowledge --key <position_key> --note <text>`` appends ONE immutable
row; the BP-3 run recorder reads the CURRENT rows when it persists a run and carries
``{"kind": "operator_acknowledgement", "position_key": ..., "acknowledgement_id": ...}`` into the
run's decisions, where AP-1's classifier records MANUAL (evidence ``operator_acknowledgement:<run
id>``). That decisions JSON is the ONLY reader path: nothing in the order plane, autonomy,
supervisor or control imports this module or the table (K3(a)).

Append-only (K1(a)): a withdrawal is a NEW row whose ``superseded_by`` points at the old one;
the old row is never edited; ORM listeners refuse any UPDATE or DELETE of a row with a typed
error. CURRENT = rows with ``superseded_by IS NULL`` that no later row supersedes; a withdrawal
row is never itself current; re-acknowledging after a withdrawal is a fresh row.

Fingerprint only (K2(a)): ``operator_fingerprint`` is 16 lower-hex of sha256(os user + host) —
never a name, never an account. Every persisted value passes ``_reject_raw_account_event_data``.

Evidence only: verified on synthetic / demo evidence; UNVERIFIED on live (K4 unanswered).
"""

from __future__ import annotations

import getpass
import hashlib
import re
import socket
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from chronos.persistence.repositories import _reject_raw_account_event_data, _require_scope
from chronos.persistence.schema import PositionAcknowledgementRow
from chronos.utils.time import utc_now

ACKNOWLEDGEMENT_KIND = "operator_acknowledgement"
_POSITION_KEY = re.compile(r"[0-9]{1,12}:[A-Z]{1,8}:(LONG|SHORT|FLAT)")
_NOTE = re.compile(r"[^\t\r\n]{1,500}")
_FINGERPRINT = re.compile(r"[0-9a-f]{16}")


class InvalidAcknowledgement(ValueError):
    """The key, note or fingerprint does not have the required shape."""


class UnknownAcknowledgement(LookupError):
    """No acknowledgement row carries this id, or it is already superseded."""


class AcknowledgementImmutable(RuntimeError):
    """A row of ``position_acknowledgements`` was about to be updated or deleted."""


@event.listens_for(PositionAcknowledgementRow, "before_update")
def _refuse_update(mapper: Any, connection: Any, target: PositionAcknowledgementRow) -> None:
    raise AcknowledgementImmutable(
        f"position acknowledgement {target.id} is append-only; write a superseding row instead"
    )


@event.listens_for(PositionAcknowledgementRow, "before_delete")
def _refuse_delete(mapper: Any, connection: Any, target: PositionAcknowledgementRow) -> None:
    raise AcknowledgementImmutable(
        f"position acknowledgement {target.id} is append-only; write a superseding row instead"
    )


@event.listens_for(Engine, "before_execute")
def _refuse_bulk_dml(
    connection: Any,
    clauseelement: Any,
    multiparams: Any,
    params: Any,
    execution_options: Any,
) -> None:
    """AP-2: the append-only rule at the ENGINE, not only the ORM unit of work.

    The mapper listeners above see rows the ORM flushes; a Core statement (``session.execute``
    of an ORM-enabled statement, or ``connection.execute`` on the table) skips them. Every
    engine therefore refuses a data-manipulation construct that is not an insert when its
    target is this table. Inserts (``acknowledge``, ``withdraw``) pass. A raw SQL string or a
    DB-API cursor is not a construct and is not seen here; closing that needs a database
    trigger, which is a migration and out of this module's reach.
    """

    del connection, multiparams, params, execution_options
    if not getattr(clauseelement, "is_dml", False) or getattr(clauseelement, "is_insert", False):
        return
    target = getattr(clauseelement, "table", None)
    if getattr(target, "name", None) != PositionAcknowledgementRow.__tablename__:
        return
    raise AcknowledgementImmutable(
        "position acknowledgements are append-only: a Core statement that edits or removes rows "
        "is refused; write a superseding row instead"
    )


def operator_fingerprint(*, user: str | None = None, host: str | None = None) -> str:
    """16 lower-hex of sha256(user + host): stable per operator seat, never a name."""

    material = f"{user or getpass.getuser()}\x1f{host or socket.gethostname()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _require_shape(position_key: str, note: str, fingerprint: str) -> None:
    if _POSITION_KEY.fullmatch(position_key) is None:
        raise InvalidAcknowledgement(
            "position_key must be <con_id>:<security_type>:<LONG|SHORT|FLAT>"
        )
    if _NOTE.fullmatch(note) is None:
        raise InvalidAcknowledgement("note must be 1-500 characters with no tab, CR or LF")
    if _FINGERPRINT.fullmatch(fingerprint) is None:
        raise InvalidAcknowledgement("operator_fingerprint must be 16 lower-hex characters")


@dataclass(frozen=True, slots=True)
class AcknowledgementRecord:
    """A row read back, typed (the fingerprint is the only operator field)."""

    id: int
    position_key: str
    note: str
    acknowledged_at: datetime
    operator_fingerprint: str
    superseded_by: int | None


def _record(row: PositionAcknowledgementRow) -> AcknowledgementRecord:
    return AcknowledgementRecord(
        id=row.id,
        position_key=row.position_key,
        note=row.note,
        acknowledged_at=row.acknowledged_at,
        operator_fingerprint=row.operator_fingerprint,
        superseded_by=row.superseded_by,
    )


class AcknowledgementRepository:
    """Append-only writer and reader for ``position_acknowledgements``."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def acknowledge(
        self,
        *,
        position_key: str,
        note: str,
        operator_fingerprint: str,
        now: datetime | None = None,
    ) -> int:
        """Append ONE acknowledgement row for ``position_key``; returns its id."""

        _require_shape(position_key, note, operator_fingerprint)
        _reject_raw_account_event_data(persisted_values=(position_key, note, operator_fingerprint))
        with self._sessions.begin() as session:
            _require_scope(session)
            row = PositionAcknowledgementRow(
                position_key=position_key,
                note=note,
                acknowledged_at=now or utc_now(),
                operator_fingerprint=operator_fingerprint,
                superseded_by=None,
            )
            session.add(row)
            session.flush()
            return int(row.id)

    def withdraw(
        self,
        *,
        acknowledgement_id: int,
        note: str,
        operator_fingerprint: str,
        now: datetime | None = None,
    ) -> int:
        """Append ONE superseding row for ``acknowledgement_id``; the old row is untouched."""

        with self._sessions.begin() as session:
            _require_scope(session)
            old = session.get(PositionAcknowledgementRow, acknowledgement_id)
            if old is None or old.superseded_by is not None:
                raise UnknownAcknowledgement(acknowledgement_id)
            already = session.scalar(
                select(PositionAcknowledgementRow.id).where(
                    PositionAcknowledgementRow.superseded_by == acknowledgement_id
                )
            )
            if already is not None:
                raise UnknownAcknowledgement(acknowledgement_id)
            _require_shape(old.position_key, note, operator_fingerprint)
            _reject_raw_account_event_data(
                persisted_values=(old.position_key, note, operator_fingerprint)
            )
            row = PositionAcknowledgementRow(
                position_key=old.position_key,
                note=note,
                acknowledged_at=now or utc_now(),
                operator_fingerprint=operator_fingerprint,
                superseded_by=acknowledgement_id,
            )
            session.add(row)
            session.flush()
            return int(row.id)

    def current(self) -> tuple[AcknowledgementRecord, ...]:
        """The acknowledgements in force: not withdrawals, and not superseded by any later row."""

        with self._sessions() as session:
            _require_scope(session)
            rows = session.scalars(
                select(PositionAcknowledgementRow)
                .where(PositionAcknowledgementRow.superseded_by.is_(None))
                .order_by(PositionAcknowledgementRow.id.asc())
            ).all()
            superseded = set(
                session.scalars(
                    select(PositionAcknowledgementRow.superseded_by).where(
                        PositionAcknowledgementRow.superseded_by.is_not(None)
                    )
                ).all()
            )
            return tuple(_record(row) for row in rows if row.id not in superseded)

    def recent(self, *, limit: int = 20) -> tuple[AcknowledgementRecord, ...]:
        """The newest ``limit`` rows of every kind, oldest first within the window."""

        with self._sessions() as session:
            _require_scope(session)
            rows = session.scalars(
                select(PositionAcknowledgementRow)
                .order_by(PositionAcknowledgementRow.id.desc())
                .limit(max(0, limit))
            ).all()
            return tuple(_record(row) for row in reversed(rows))


def acknowledgement_decisions(records: tuple[AcknowledgementRecord, ...]) -> list[dict[str, Any]]:
    """The decisions the run recorder appends: one per CURRENT key, the newest id per key."""

    newest: dict[str, int] = {}
    for record in records:
        newest[record.position_key] = max(newest.get(record.position_key, 0), record.id)
    return [
        {"kind": ACKNOWLEDGEMENT_KIND, "position_key": key, "acknowledgement_id": newest[key]}
        for key in sorted(newest)
    ]
