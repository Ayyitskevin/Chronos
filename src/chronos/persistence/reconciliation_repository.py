"""Account facts persist per reconciliation run (BP-3): the writer for ``reconciliation_runs``.

One row per run, append-only, never pruned (K1 = (a)). The row's ``broker_snapshot`` carries the
MASKED account summary — net liquidation, total cash, buying power, currency, ``as_of`` and the
account fingerprint — never the raw broker account id (K2 = (a)): every persisted value passes
``_reject_raw_account_event_data`` before a write. Positions carry a stable ``position_key`` so a
later provenance row (AP-1) can reference them. Foreign/manual positions are recorded, never acted
on (K3 = (a)).

The row is written ONLY after a decided result — the runtime calls the recorder after the
readiness latch has published (the BP-1b r2 lesson: never from ``finally``, never for a refused
or undecided pass) — and a persistence failure is one typed application event, never a raised
exception into the pass and never a change to the latch. Nothing in the order plane, autonomy,
supervisor or control reads these rows; ``chronos.cli reconciliation-runs`` and a fresh session
read them back.

The ``trigger`` a row carries is the caller's own word. The real callers today are ``startup``
(the api/main.py lifespan), ``operator`` (``POST /orders/reconcile``) and ``periodic``
(``reconcile_once``); a caller that passes nothing is recorded as ``unattributed`` — the honest
default, never a guessed label. ``reconnect`` and ``order_fill`` are vocabulary no caller emits yet.

Evidence only: verified on synthetic / demo evidence; UNVERIFIED on live until the M4 read-only
session (K4 unanswered).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import AwareDatetime, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from chronos.domain.enums import ReconciliationStatus
from chronos.domain.models import ChronosModel
from chronos.persistence.repositories import (
    ApplicationEventRepository,
    _reject_raw_account_event_data,
    _require_scope,
)
from chronos.persistence.schema import ReconciliationRunRow
from chronos.portfolio.provenance import record_position_provenance

if TYPE_CHECKING:  # type only: the runtime import drags chronos.broker into the operator CLI
    from chronos.services.reconciliation import ReconciliationResult

_LOGGER = logging.getLogger(__name__)

ReconciliationTrigger = Literal[
    "startup", "reconnect", "order_fill", "periodic", "operator", "unattributed"
]
TRIGGERS: tuple[str, ...] = (
    "startup",
    "reconnect",
    "order_fill",
    "periodic",
    "operator",
    "unattributed",
)


class ReconciliationRunConflict(ValueError):
    """The same run id was recorded before with different bytes; the row is never overwritten."""


class ReconciliationAccountFacts(ChronosModel):
    """The masked account summary of one run — the raw broker account id is never a field."""

    masked_account_id: str
    account_fingerprint: str
    net_liquidation: Any
    total_cash: Any
    buying_power: Any
    currency: str
    as_of: AwareDatetime


class ReconciliationSnapshot(ChronosModel):
    """The typed, presentation-safe content of one ``reconciliation_runs`` row."""

    run_id: str
    trigger: ReconciliationTrigger
    status: ReconciliationStatus
    started_at: AwareDatetime
    completed_at: AwareDatetime
    account: ReconciliationAccountFacts | None
    positions: tuple[dict[str, Any], ...] = ()
    open_orders: tuple[dict[str, Any], ...] = ()
    evidence_age_seconds: float | None = Field(default=None, ge=0)
    execution_count: int = Field(default=0, ge=0)
    decisions: tuple[dict[str, Any], ...] = ()
    reasons: tuple[str, ...] = ()
    readiness_status: ReconciliationStatus
    readiness_reason: str
    generation: int = Field(ge=0)

    @classmethod
    def from_result(
        cls,
        *,
        run_id: str,
        trigger: ReconciliationTrigger,
        portfolio: ReconciliationResult,
        readiness_status: ReconciliationStatus,
        readiness_reason: str,
        generation: int,
        started_at: datetime,
        completed_at: datetime,
        account_fingerprint: str,
        restart_decisions: tuple[dict[str, Any], ...] = (),
    ) -> ReconciliationSnapshot:
        snapshot = portfolio.snapshot
        account = None
        positions: tuple[dict[str, Any], ...] = ()
        open_orders: tuple[dict[str, Any], ...] = ()
        evidence_age = None
        execution_count = 0
        if snapshot is not None:
            account = ReconciliationAccountFacts(
                masked_account_id=snapshot.account.masked_account_id,
                account_fingerprint=account_fingerprint,
                net_liquidation=str(snapshot.account.net_liquidation),
                total_cash=str(snapshot.account.total_cash),
                buying_power=str(snapshot.account.buying_power),
                currency=snapshot.account.currency,
                as_of=snapshot.account.as_of,
            )
            positions = tuple(_position_entry(view) for view in snapshot.positions)
            open_orders = tuple(
                json_ready(view.model_dump(mode="json")) for view in snapshot.open_orders
            )
            evidence_age = max(0.0, (completed_at - snapshot.captured_at).total_seconds())
            execution_count = snapshot.execution_count
        decisions = tuple(
            {
                "symbol": symbol.symbol,
                "status": symbol.status.value,
                "stage": symbol.stage.value,
                "manual_review_required": symbol.manual_review_required,
                "reasons": list(symbol.reasons),
            }
            for symbol in portfolio.symbols
        ) + tuple(restart_decisions)
        return cls(
            run_id=run_id,
            trigger=trigger,
            status=portfolio.status,
            started_at=started_at,
            completed_at=completed_at,
            account=account,
            positions=positions,
            open_orders=open_orders,
            evidence_age_seconds=evidence_age,
            execution_count=execution_count,
            decisions=decisions,
            reasons=tuple(portfolio.reasons),
            readiness_status=readiness_status,
            readiness_reason=readiness_reason,
            generation=generation,
        )

    def broker_snapshot_json(self) -> dict[str, Any]:
        return json_ready_dict(
            {
                # "account_facts", not "account": the raw-id guard refuses the bare key "account"
                "account_facts": self.account.model_dump(mode="json") if self.account else None,
                "positions": list(self.positions),
                "open_orders": list(self.open_orders),
                "evidence_age_seconds": self.evidence_age_seconds,
                "execution_count": self.execution_count,
                "readiness": {
                    "status": self.readiness_status.value,
                    "reason": self.readiness_reason,
                    "generation": self.generation,
                },
                "reasons": list(self.reasons),
            }
        )

    def decisions_json(self) -> list[dict[str, Any]]:
        return [json_ready(decision) for decision in self.decisions]


def _position_entry(view: Any) -> dict[str, Any]:
    """A position with a stable key AP-1 can reference: ``<symbol>:<con_id>``."""

    contract = view.contract
    con_id = getattr(contract, "con_id", None)
    entry = json_ready_dict(view.model_dump(mode="json"))
    entry["position_key"] = f"{contract.symbol}:{con_id if con_id is not None else 'none'}"
    return entry


def json_ready(value: Any) -> Any:
    """Decimals and datetimes as strings so the JSON column round-trips byte-for-byte."""

    return json.loads(json.dumps(value, default=str, sort_keys=False))


def json_ready_dict(value: dict[str, Any]) -> dict[str, Any]:
    ready = json_ready(value)
    assert isinstance(ready, dict)
    return ready


@dataclass(frozen=True, slots=True)
class ReconciliationRunRecord:
    """A row read back, typed."""

    run_id: str
    trigger: str
    status: str
    broker_snapshot: dict[str, Any]
    decisions: list[dict[str, Any]]
    started_at: datetime
    completed_at: datetime | None


class ReconciliationRepository:
    """Idempotent, run-keyed writer and reader for ``reconciliation_runs``."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def record_run(self, snapshot: ReconciliationSnapshot) -> bool:
        """Write ONE row for ``snapshot.run_id``.

        Returns ``False`` when the identical row already exists (a replay); raises
        :class:`ReconciliationRunConflict` when the same run id was recorded with different
        bytes — the row is never overwritten. The raw broker account id is refused before any
        write (K2 = (a)).
        """

        broker_snapshot = snapshot.broker_snapshot_json()
        decisions = snapshot.decisions_json()
        _reject_raw_account_event_data(
            persisted_values=(snapshot.run_id, snapshot.trigger, broker_snapshot, decisions)
        )
        with self._sessions.begin() as session:
            _require_scope(session)
            existing = session.get(ReconciliationRunRow, snapshot.run_id)
            if existing is not None:
                _require_same_bytes(existing, snapshot, broker_snapshot, decisions)
                return False
            row = ReconciliationRunRow(
                id=snapshot.run_id,
                trigger=snapshot.trigger,
                status=snapshot.status.value,
                broker_snapshot=broker_snapshot,
                decisions=decisions,
                started_at=snapshot.started_at,
                completed_at=snapshot.completed_at,
            )
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
            except IntegrityError:
                # a concurrent writer recorded this run first: re-read and compare
                session.expire_all()
                winner = session.get(ReconciliationRunRow, snapshot.run_id)
                if winner is None:  # pragma: no cover — a different constraint fired
                    raise
                _require_same_bytes(winner, snapshot, broker_snapshot, decisions)
                return False
        return True

    def recent(self, *, limit: int = 20) -> tuple[ReconciliationRunRecord, ...]:
        """The newest ``limit`` rows, oldest first within the window (ordered by ``started_at``)."""

        with self._sessions() as session:
            _require_scope(session)
            rows = session.scalars(
                select(ReconciliationRunRow)
                .order_by(ReconciliationRunRow.started_at.desc(), ReconciliationRunRow.id.desc())
                .limit(max(0, limit))
            ).all()
        records = [
            ReconciliationRunRecord(
                run_id=row.id,
                trigger=row.trigger,
                status=row.status,
                broker_snapshot=dict(row.broker_snapshot),
                decisions=list(row.decisions),
                started_at=row.started_at,
                completed_at=row.completed_at,
            )
            for row in rows
        ]
        records.reverse()
        return tuple(records)


def _require_same_bytes(
    stored: ReconciliationRunRow,
    snapshot: ReconciliationSnapshot,
    broker_snapshot: dict[str, Any],
    decisions: list[dict[str, Any]],
) -> None:
    same = (
        stored.trigger == snapshot.trigger
        and stored.status == snapshot.status.value
        and json_ready(stored.broker_snapshot) == broker_snapshot
        and json_ready(stored.decisions) == decisions
    )
    if not same:
        raise ReconciliationRunConflict(
            f"reconciliation run {snapshot.run_id} was already recorded with different bytes; "
            "the row is never overwritten"
        )


class ReconciliationRunRecorder:
    """The runtime's seam: called AFTER the readiness latch published, never raises.

    A persistence failure becomes one typed application event
    (``reconciliation_run_persist_failed``) and the pass returns exactly what it decided; the
    latch never waits on the database (ADR-0020's timing is not a persistence question).
    """

    EVENT_TYPE = "reconciliation_run_persist_failed"

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions
        self._runs = ReconciliationRepository(sessions)
        self._events = ApplicationEventRepository(sessions)

    def record(self, snapshot: ReconciliationSnapshot) -> bool | None:
        try:
            written = self._runs.record_run(snapshot)
        except Exception as exc:  # evidence only; the pass already decided
            _LOGGER.warning(
                "reconciliation run was not persisted; readiness is unaffected",
                extra={"event": self.EVENT_TYPE, "run_id": snapshot.run_id},
            )
            try:
                self._events.append(
                    event_type=self.EVENT_TYPE,
                    message=f"{exc.__class__.__name__}: {exc}"[:500],
                    severity="WARNING",
                    event_data={"run_id": snapshot.run_id, "trigger": snapshot.trigger},
                )
            except Exception:  # the event is best effort too
                _LOGGER.warning("application event for the failed run write was not persisted")
            return None
        if written:
            # AP-1: provenance rows follow the persisted run — never raises, never reads the broker
            record_position_provenance(self._sessions, snapshot.run_id)
        return written
