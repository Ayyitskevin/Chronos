"""Allocation provenance for every observed position (AP-1): recorded, never acted on.

After a reconciliation run is persisted (BP-3's ``reconciliation_runs`` row), every position
the broker reported gets ONE ``position_provenance`` row naming its origin class:

* ``MANAGED`` — an autonomy binding (``managed_position_bindings``) resolves, through its
  opening order intent's persisted ``con_id``, to the position's contract; evidence = the
  binding's ``position_id``;
* ``WHEEL`` — an open wheel cycle or a basis-ledger entry holds the symbol / contract;
  evidence = the ``wheel_cycle_id``;
* ``MANUAL`` — the run's own decisions carry an operator acknowledgement for the key
  (``{"kind": "operator_acknowledgement", "position_key": ...}``; no producer writes one yet);
  evidence = ``operator_acknowledgement:<run id>``;
* ``FOREIGN`` — everything else; evidence = ``<first-seen run id>:<snapshot digest>``.

Precedence when a position matches more than one source: MANAGED > WHEEL > MANUAL > FOREIGN.

The classifier reads the persisted run and the persisted ledgers only — never the broker.
Append-only (K1(a)): the same (run, key, class, evidence) is written once; a class change on a
later run is a NEW row, the old one is never edited. Rows carry the account FINGERPRINT only
(K2(a)); every persisted value passes ``_reject_raw_account_event_data``. Nothing in the order
plane, autonomy, supervisor or control imports this module or reads the table (K3(a)):
``chronos.cli position-provenance --run <id>`` is the only reader.

Evidence only: verified on synthetic / demo evidence; UNVERIFIED on live (K4 unanswered).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from chronos.persistence.repositories import (
    ApplicationEventRepository,
    _reject_raw_account_event_data,
    _require_scope,
)
from chronos.persistence.schema import (
    BasisEntryRow,
    ManagedPositionBindingRow,
    OrderIntentRow,
    PositionProvenanceRow,
    ReconciliationRunRow,
    WheelCycleRow,
)
from chronos.utils.time import utc_now

_LOGGER = logging.getLogger(__name__)

OriginClass = Literal["MANAGED", "WHEEL", "FOREIGN", "MANUAL"]
ORIGIN_CLASSES: tuple[str, ...] = ("MANAGED", "WHEEL", "FOREIGN", "MANUAL")
ACKNOWLEDGEMENT_KIND = "operator_acknowledgement"
EVENT_TYPE = "position_provenance_persist_failed"


class UnknownReconciliationRun(LookupError):
    """No persisted reconciliation run carries this id."""


def position_key(*, con_id: int, security_type: str, quantity: Decimal) -> str:
    """The stable key: ``<con_id>:<security_type>:<side>`` (side from the quantity sign)."""

    side = "LONG" if quantity > 0 else "SHORT" if quantity < 0 else "FLAT"
    return f"{con_id}:{security_type}:{side}"


@dataclass(frozen=True, slots=True)
class PositionClassification:
    """One position's origin class at one run, with the evidence that named it."""

    position_key: str
    symbol: str
    con_id: int
    quantity: Decimal
    origin_class: str
    evidence_ref: str
    first_seen_run_id: str
    observed_run_id: str


@dataclass(frozen=True, slots=True)
class ProvenanceRecord:
    """A row read back, typed and masked (the fingerprint is the only account field)."""

    id: int
    account_fingerprint: str
    position_key: str
    origin_class: str
    evidence_ref: str
    first_seen_run_id: str
    observed_run_id: str
    quantity: Decimal
    recorded_at: datetime


def snapshot_digest(broker_snapshot: dict[str, Any]) -> str:
    """SHA-256 of the run's broker snapshot, canonical JSON — the FOREIGN evidence anchor."""

    canonical = json.dumps(broker_snapshot, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _positions(run: ReconciliationRunRow) -> list[dict[str, Any]]:
    positions = run.broker_snapshot.get("positions", [])
    return [entry for entry in positions if isinstance(entry, dict)]


def _managed_contracts(session: Session, fingerprint: str) -> dict[int, str]:
    """``con_id`` → binding ``position_id`` for every binding of this account.

    A binding names its opening order only (``opening_order_ref`` → ``order_intents.order_ref``,
    a foreign key); the contract is the intent's persisted ``con_id`` — never a broker read.
    """

    resolved: dict[int, str] = {}
    rows = session.execute(
        select(ManagedPositionBindingRow.position_id, OrderIntentRow.con_id)
        .join(
            OrderIntentRow, OrderIntentRow.order_ref == ManagedPositionBindingRow.opening_order_ref
        )
        .where(ManagedPositionBindingRow.account_fingerprint == fingerprint)
        .order_by(ManagedPositionBindingRow.id.asc())
    ).all()
    for position_id, con_id in rows:
        if isinstance(con_id, int):
            resolved.setdefault(con_id, position_id)
    return resolved


def _wheel_cycles(session: Session, fingerprint: str) -> tuple[dict[str, str], dict[int, str]]:
    """(symbol → cycle id) for OPEN cycles and (con_id → cycle id) from the basis ledger."""

    by_symbol: dict[str, str] = {}
    for cycle in session.scalars(select(WheelCycleRow).where(WheelCycleRow.status == "OPEN")):
        by_symbol.setdefault(cycle.symbol, cycle.id)
    by_con_id: dict[int, str] = {}
    for entry in session.scalars(
        select(BasisEntryRow).where(BasisEntryRow.account_fingerprint == fingerprint)
    ):
        if entry.contract_id is not None:
            by_con_id.setdefault(entry.contract_id, entry.wheel_cycle_id)
        by_symbol.setdefault(entry.symbol, entry.wheel_cycle_id)
    return by_symbol, by_con_id


def _acknowledged_keys(run: ReconciliationRunRow) -> set[str]:
    keys: set[str] = set()
    for decision in run.decisions:
        if not isinstance(decision, dict) or decision.get("kind") != ACKNOWLEDGEMENT_KIND:
            continue
        key = decision.get("position_key")
        if isinstance(key, str) and key:
            keys.add(key)
    return keys


def _first_seen(session: Session, fingerprint: str, key: str, fallback: str) -> str:
    earliest = session.scalar(
        select(PositionProvenanceRow.first_seen_run_id)
        .where(
            PositionProvenanceRow.account_fingerprint == fingerprint,
            PositionProvenanceRow.position_key == key,
        )
        .order_by(PositionProvenanceRow.id.asc())
        .limit(1)
    )
    return earliest if earliest is not None else fallback


def classify(session: Session, run: ReconciliationRunRow) -> tuple[PositionClassification, ...]:
    """One classification per position the persisted run reported (no broker call)."""

    facts = run.broker_snapshot.get("account_facts") or {}
    fingerprint = str(facts.get("account_fingerprint") or "")
    if not fingerprint:
        return ()
    managed = _managed_contracts(session, fingerprint)
    wheel_by_symbol, wheel_by_con_id = _wheel_cycles(session, fingerprint)
    acknowledged = _acknowledged_keys(run)
    digest = snapshot_digest(run.broker_snapshot)
    classified: list[PositionClassification] = []
    for entry in _positions(run):
        contract = entry.get("contract") or {}
        con_id = contract.get("con_id")
        symbol = contract.get("symbol")
        security_type = contract.get("security_type")
        if not isinstance(con_id, int) or not isinstance(symbol, str) or not security_type:
            continue
        quantity = Decimal(str(entry.get("quantity", "0")))
        key = position_key(con_id=con_id, security_type=str(security_type), quantity=quantity)
        if con_id in managed:
            origin, evidence = "MANAGED", managed[con_id]
        elif con_id in wheel_by_con_id:
            origin, evidence = "WHEEL", wheel_by_con_id[con_id]
        elif symbol in wheel_by_symbol:
            origin, evidence = "WHEEL", wheel_by_symbol[symbol]
        elif key in acknowledged:
            origin, evidence = "MANUAL", f"{ACKNOWLEDGEMENT_KIND}:{run.id}"
        else:
            origin = "FOREIGN"
            evidence = f"{_first_seen(session, fingerprint, key, run.id)}:{digest}"
        classified.append(
            PositionClassification(
                position_key=key,
                symbol=symbol,
                con_id=con_id,
                quantity=quantity,
                origin_class=origin,
                evidence_ref=evidence,
                first_seen_run_id=_first_seen(session, fingerprint, key, run.id),
                observed_run_id=run.id,
            )
        )
    return tuple(classified)


class PositionProvenanceRepository:
    """Append-only writer and masked reader for ``position_provenance``."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def record_run(self, run_id: str) -> int:
        """Classify the persisted run ``run_id`` and append the rows not yet present.

        Returns the number of rows written (0 on a replay). Raises
        :class:`UnknownReconciliationRun` when no run carries the id. Never updates a row.
        """

        with self._sessions.begin() as session:
            _require_scope(session)
            run = session.get(ReconciliationRunRow, run_id)
            if run is None:
                raise UnknownReconciliationRun(run_id)
            facts = run.broker_snapshot.get("account_facts") or {}
            fingerprint = str(facts.get("account_fingerprint") or "")
            written = 0
            now = utc_now()
            for item in classify(session, run):
                _reject_raw_account_event_data(
                    persisted_values=(
                        fingerprint,
                        item.position_key,
                        item.origin_class,
                        item.evidence_ref,
                        item.first_seen_run_id,
                        item.observed_run_id,
                    )
                )
                exists = session.scalar(
                    select(PositionProvenanceRow.id).where(
                        PositionProvenanceRow.observed_run_id == item.observed_run_id,
                        PositionProvenanceRow.position_key == item.position_key,
                        PositionProvenanceRow.origin_class == item.origin_class,
                        PositionProvenanceRow.evidence_ref == item.evidence_ref,
                    )
                )
                if exists is not None:
                    continue  # the same (run, key, class, evidence) is written once
                session.add(
                    PositionProvenanceRow(
                        account_fingerprint=fingerprint,
                        position_key=item.position_key,
                        origin_class=item.origin_class,
                        evidence_ref=item.evidence_ref,
                        first_seen_run_id=item.first_seen_run_id,
                        observed_run_id=item.observed_run_id,
                        quantity=item.quantity,
                        recorded_at=now,
                    )
                )
                written += 1
            return written

    def rows_for_run(self, run_id: str) -> tuple[ProvenanceRecord, ...]:
        """The rows observed at ``run_id``, insertion order; raises on an unknown run."""

        with self._sessions() as session:
            _require_scope(session)
            if session.get(ReconciliationRunRow, run_id) is None:
                raise UnknownReconciliationRun(run_id)
            rows = session.scalars(
                select(PositionProvenanceRow)
                .where(PositionProvenanceRow.observed_run_id == run_id)
                .order_by(PositionProvenanceRow.id.asc())
            ).all()
            return tuple(
                ProvenanceRecord(
                    id=row.id,
                    account_fingerprint=row.account_fingerprint,
                    position_key=row.position_key,
                    origin_class=row.origin_class,
                    evidence_ref=row.evidence_ref,
                    first_seen_run_id=row.first_seen_run_id,
                    observed_run_id=row.observed_run_id,
                    quantity=row.quantity,
                    recorded_at=row.recorded_at,
                )
                for row in rows
            )


def record_position_provenance(sessions: sessionmaker[Session], run_id: str) -> int | None:
    """The seam for the run recorder: never raises; a failure is one typed application event."""

    try:
        return PositionProvenanceRepository(sessions).record_run(run_id)
    except Exception as exc:  # evidence only; the run row is already persisted
        _LOGGER.warning(
            "position provenance was not persisted; the run row stands",
            extra={"event": EVENT_TYPE, "run_id": run_id},
        )
        try:
            ApplicationEventRepository(sessions).append(
                event_type=EVENT_TYPE,
                message=f"{exc.__class__.__name__}: {exc}"[:500],
                severity="WARNING",
                event_data={"run_id": run_id},
            )
        except Exception:  # the event is best effort too
            _LOGGER.warning("application event for the failed provenance write was not persisted")
        return None
