"""Repositories for the Milestone 5 human-in-the-loop order pipeline.

These persist the intent-keyed order-management spine (``order_intents``,
``order_events``, ``order_confirmations``, ``risk_decisions`` /
``risk_check_results``). They are deliberately separate from the autonomous
platform's correlation-keyed ``order_drafts``/``submitted_orders``/``fills``
tables: the live-Wheel pipeline is self-contained here and is matched to broker
truth by its ``order_ref`` (CHR-) alone, so it never entangles the autonomous
execution ledger.

Every method obtains and matches the single bound account scope first (reusing
the same guards as :mod:`chronos.persistence.repositories`) and persists only
the pseudonymous ``account_fingerprint`` — never a raw broker account id. Writes
are idempotent: an exact replay returns ``False`` and mutates nothing; a
conflicting write raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from chronos.domain.enums import OrderLifecycle, OrderSide, ProductFamily, RiskCheckStatus
from chronos.persistence.repositories import _require_matching_account_scope
from chronos.persistence.schema import (
    OrderConfirmationRow,
    OrderEventRow,
    OrderIntentRow,
    RiskCheckResultRow,
    RiskDecisionRow,
)
from chronos.utils.identifiers import account_fingerprint

# Intent statuses that mean the order is still working (not yet terminal) and
# therefore still encumbers cash/shares for the next order's risk evaluation.
_ACTIVE_INTENT_STATUSES = frozenset(
    {
        OrderLifecycle.USER_CONFIRMED,
        OrderLifecycle.SUBMITTED,
        OrderLifecycle.SUBMISSION_UNKNOWN,
        OrderLifecycle.PARTIALLY_FILLED,
        OrderLifecycle.CANCEL_PENDING,
    }
)


@dataclass(frozen=True, slots=True)
class OrderIntentRecord:
    """Read model for one persisted order intent."""

    intent_id: str
    idempotency_key: str
    account_fingerprint: str
    environment: str
    product_family: ProductFamily
    wheel_cycle_id: str | None
    symbol: str
    con_id: int | None
    local_symbol: str | None
    action: OrderSide
    open_close_effect: str
    quantity: Decimal
    order_type: str
    limit_price: Decimal | None
    time_in_force: str
    outside_rth: bool
    quote_snapshot_id: str | None
    risk_snapshot_id: str | None
    preview_id: str | None
    confirmation_hash: str | None
    order_ref: str | None
    status: OrderLifecycle
    created_at: datetime
    confirmed_at: datetime | None
    submitted_at: datetime | None
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class OrderEventRecord:
    intent_id: str
    event_key: str
    sequence: int
    source: str
    from_status: OrderLifecycle | None
    to_status: OrderLifecycle
    broker_order_id: int | None
    filled_quantity: Decimal | None
    remaining_quantity: Decimal | None
    evidence: dict[str, Any]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class OrderConfirmationRecord:
    intent_id: str
    summary_hash: str
    ui_session_id: str | None
    confirmed_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RiskCheckRecord:
    sequence: int
    check_name: str
    status: RiskCheckStatus
    detail: str
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RiskDecisionRecord:
    decision_id: str
    correlation_id: str | None
    account_fingerprint: str
    symbol: str
    product_family: ProductFamily
    overall_result: RiskCheckStatus
    decided_at: datetime
    expires_at: datetime
    evidence: dict[str, Any]
    checks: tuple[RiskCheckRecord, ...]


def _economic_signature(row: OrderIntentRow | OrderIntentRecord) -> tuple[object, ...]:
    """Immutable economic identity of an order (ignores lifecycle/status fields).

    Two intents with the same signature are the same economic order, so an
    idempotency-key match on them is a benign replay rather than a conflict.
    """

    # Decimal quantity/limit_price are compared as Decimals (not formatted
    # strings) so a DB round-trip to Numeric(20,8) — which reappends trailing
    # zeros — still equals the freshly-built record. Decimal.__eq__ ignores the
    # trailing zeros; tuple equality delegates to it element-wise.
    return (
        row.account_fingerprint,
        str(row.product_family),
        row.symbol,
        row.con_id,
        str(row.action),
        row.open_close_effect,
        row.quantity,
        row.limit_price,
        row.order_type,
        row.time_in_force,
        bool(row.outside_rth),
        row.environment,
        row.order_ref,
    )


def _intent_row_to_record(row: OrderIntentRow) -> OrderIntentRecord:
    return OrderIntentRecord(
        intent_id=row.intent_id,
        idempotency_key=row.idempotency_key,
        account_fingerprint=row.account_fingerprint,
        environment=row.environment,
        product_family=ProductFamily(row.product_family),
        wheel_cycle_id=row.wheel_cycle_id,
        symbol=row.symbol,
        con_id=row.con_id,
        local_symbol=row.local_symbol,
        action=OrderSide(row.action),
        open_close_effect=row.open_close_effect,
        quantity=row.quantity,
        order_type=row.order_type,
        limit_price=row.limit_price,
        time_in_force=row.time_in_force,
        outside_rth=row.outside_rth,
        quote_snapshot_id=row.quote_snapshot_id,
        risk_snapshot_id=row.risk_snapshot_id,
        preview_id=row.preview_id,
        confirmation_hash=row.confirmation_hash,
        order_ref=row.order_ref,
        status=OrderLifecycle(row.status),
        created_at=row.created_at,
        confirmed_at=row.confirmed_at,
        submitted_at=row.submitted_at,
        expires_at=row.expires_at,
    )


class OrderIntentRepository:
    """Create, read, and advance the status of intent-keyed orders."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create(self, record: OrderIntentRecord, *, current_account_id: str) -> bool:
        """Persist a new intent. Idempotent on ``idempotency_key``.

        Returns ``False`` when an identical idempotency key already maps to the
        same economic order (a benign replay); raises when the key already maps
        to a *different* order (a genuine duplicate that must be refused).
        """

        with self._sessions.begin() as session:
            scope = _require_matching_account_scope(session, current_account_id)
            if record.account_fingerprint != scope.account_fingerprint:
                raise ValueError("Order intent account does not match the bound database scope")
            existing = self._by_idempotency_key(session, record.idempotency_key)
            if existing is not None:
                if _economic_signature(existing) == _economic_signature(record):
                    return False  # benign replay of the same economic order
                raise ValueError(
                    "Idempotency key already maps to a different order intent; refusing duplicate"
                )
            row = _record_to_intent_row(record)
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
            except IntegrityError as error:
                concurrent = self._by_idempotency_key(session, record.idempotency_key)
                if concurrent is not None and _economic_signature(
                    concurrent
                ) == _economic_signature(record):
                    return False
                raise ValueError(
                    "Order intent conflicted with a concurrent write or missing owner"
                ) from error
            return True

    def get(self, intent_id: str, *, current_account_id: str) -> OrderIntentRecord | None:
        with self._sessions() as session:
            _require_matching_account_scope(session, current_account_id)
            row = session.get(OrderIntentRow, intent_id)
            return _intent_row_to_record(row) if row is not None else None

    def by_order_ref(self, order_ref: str, *, current_account_id: str) -> OrderIntentRecord | None:
        with self._sessions() as session:
            _require_matching_account_scope(session, current_account_id)
            row = session.scalar(
                select(OrderIntentRow).where(OrderIntentRow.order_ref == order_ref)
            )
            return _intent_row_to_record(row) if row is not None else None

    def active(self, *, current_account_id: str) -> tuple[OrderIntentRecord, ...]:
        """Return every still-working intent for the bound account."""

        fingerprint = account_fingerprint(current_account_id)
        with self._sessions() as session:
            _require_matching_account_scope(session, current_account_id)
            rows = session.scalars(
                select(OrderIntentRow)
                .where(OrderIntentRow.account_fingerprint == fingerprint)
                .where(OrderIntentRow.status.in_([s.value for s in _ACTIVE_INTENT_STATUSES]))
                .order_by(OrderIntentRow.created_at)
            )
            return tuple(_intent_row_to_record(row) for row in rows)

    def ids_in_status(self, status: OrderLifecycle, *, current_account_id: str) -> tuple[str, ...]:
        """Intent ids currently in exactly ``status`` (live reconciliation gate)."""

        fingerprint = account_fingerprint(current_account_id)
        with self._sessions() as session:
            _require_matching_account_scope(session, current_account_id)
            rows = session.scalars(
                select(OrderIntentRow.intent_id)
                .where(OrderIntentRow.account_fingerprint == fingerprint)
                .where(OrderIntentRow.status == status.value)
                .order_by(OrderIntentRow.created_at)
            )
            return tuple(rows)

    def list_all(self, *, current_account_id: str) -> tuple[OrderIntentRecord, ...]:
        fingerprint = account_fingerprint(current_account_id)
        with self._sessions() as session:
            _require_matching_account_scope(session, current_account_id)
            rows = session.scalars(
                select(OrderIntentRow)
                .where(OrderIntentRow.account_fingerprint == fingerprint)
                .order_by(OrderIntentRow.created_at)
            )
            return tuple(_intent_row_to_record(row) for row in rows)

    def count_opening_since(self, *, current_account_id: str, since: datetime) -> int:
        """Count opening intents created on/after ``since``, whatever their side.

        **The side filter this used to carry was a bug (R-25).** It matched
        ``action == SELL``, which was correct when the only way to open anything
        was a short put — and silently wrong from the moment stocks and crypto
        arrived, because those open with a BUY. Intersecting the two intent sets
        makes the gap exact: ``OPEN ∧ SELL`` is ``{OPEN_SHORT_PUT,
        OPEN_COVERED_CALL}``, so ``OPEN_LONG_STOCK`` and ``OPEN_LONG_CRYPTO``
        were invisible to the daily cap. ``open_close_effect`` is the only
        predicate that means "this creates exposure", and it is the only one
        used now.

        **Created, not filled.** An intent that was created and then refused
        still counts. The limit exists to bound an unthrottled decision loop
        (R-25), and a loop that could mint a thousand rejected intents without
        moving the counter would be bounded by nothing at all. Counting
        *attempts to open* is the conservative reading and the one that matches
        what the limit is for.
        """

        fingerprint = account_fingerprint(current_account_id)
        with self._sessions() as session:
            _require_matching_account_scope(session, current_account_id)
            count = session.scalar(
                select(func.count())
                .select_from(OrderIntentRow)
                .where(OrderIntentRow.account_fingerprint == fingerprint)
                .where(OrderIntentRow.open_close_effect == "OPEN")
                .where(OrderIntentRow.created_at >= since)
            )
            return int(count or 0)

    def set_status(
        self,
        intent_id: str,
        status: OrderLifecycle,
        *,
        current_account_id: str,
        at: datetime,
        confirmation_hash: str | None = None,
        risk_snapshot_id: str | None = None,
        preview_id: str | None = None,
        order_ref: str | None = None,
    ) -> None:
        """Advance an intent's status (transition legality is enforced upstream)."""

        with self._sessions.begin() as session:
            _require_matching_account_scope(session, current_account_id)
            row = session.get(OrderIntentRow, intent_id)
            if row is None:
                raise ValueError(f"Unknown order intent {intent_id!r}")
            row.status = status.value
            if confirmation_hash is not None:
                row.confirmation_hash = confirmation_hash
            if risk_snapshot_id is not None:
                row.risk_snapshot_id = risk_snapshot_id
            if preview_id is not None:
                row.preview_id = preview_id
            if order_ref is not None:
                row.order_ref = order_ref
            if status is OrderLifecycle.USER_CONFIRMED:
                row.confirmed_at = at
            if status is OrderLifecycle.SUBMITTED and row.submitted_at is None:
                row.submitted_at = at

    @staticmethod
    def _by_idempotency_key(session: Session, key: str) -> OrderIntentRow | None:
        return session.scalar(select(OrderIntentRow).where(OrderIntentRow.idempotency_key == key))


def _record_to_intent_row(record: OrderIntentRecord) -> OrderIntentRow:
    return OrderIntentRow(
        intent_id=record.intent_id,
        idempotency_key=record.idempotency_key,
        account_fingerprint=record.account_fingerprint,
        environment=record.environment,
        product_family=record.product_family.value,
        wheel_cycle_id=record.wheel_cycle_id,
        symbol=record.symbol,
        con_id=record.con_id,
        local_symbol=record.local_symbol,
        action=record.action.value,
        open_close_effect=record.open_close_effect,
        quantity=record.quantity,
        order_type=record.order_type,
        limit_price=record.limit_price,
        time_in_force=record.time_in_force,
        outside_rth=record.outside_rth,
        quote_snapshot_id=record.quote_snapshot_id,
        risk_snapshot_id=record.risk_snapshot_id,
        preview_id=record.preview_id,
        confirmation_hash=record.confirmation_hash,
        order_ref=record.order_ref,
        status=record.status.value,
        created_at=record.created_at,
        confirmed_at=record.confirmed_at,
        submitted_at=record.submitted_at,
        expires_at=record.expires_at,
    )


class OrderTrackerRepository:
    """Append lifecycle events and advance the owning intent atomically."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def record_transition(
        self,
        *,
        intent_id: str,
        event_key: str,
        source: str,
        from_status: OrderLifecycle | None,
        to_status: OrderLifecycle,
        current_account_id: str,
        broker_order_id: int | None = None,
        filled_quantity: Decimal | None = None,
        remaining_quantity: Decimal | None = None,
        evidence: dict[str, Any] | None = None,
        occurred_at: datetime,
        enforce_from_status: bool = False,
    ) -> bool:
        """Write one order_events row and set the intent status in one txn.

        Returns ``False`` when ``event_key`` was already recorded (a duplicate
        or late broker callback), mutating nothing.

        With ``enforce_from_status=True`` this is a true CAS (ADR-0009 §4): the
        intent's CURRENT status must equal ``from_status`` inside this same
        transaction, otherwise nothing is written and ``False`` is returned —
        the guard the submission boundary uses so an intent that left
        USER_CONFIRMED while gate evidence was being gathered can never be
        clobbered back into the submit path.
        """

        with self._sessions.begin() as session:
            _require_matching_account_scope(session, current_account_id)
            intent = session.get(OrderIntentRow, intent_id)
            if intent is None:
                raise ValueError(f"Unknown order intent {intent_id!r}")
            if enforce_from_status and (from_status is None or intent.status != from_status.value):
                return False
            if session.scalar(select(OrderEventRow.id).where(OrderEventRow.event_key == event_key)):
                return False
            sequence = self._next_sequence(session, intent_id)
            row = OrderEventRow(
                intent_id=intent_id,
                event_key=event_key,
                sequence=sequence,
                source=source,
                from_status=from_status.value if from_status is not None else None,
                to_status=to_status.value,
                broker_order_id=broker_order_id,
                filled_quantity=filled_quantity,
                remaining_quantity=remaining_quantity,
                evidence=evidence or {},
                occurred_at=occurred_at,
            )
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
            except IntegrityError:
                # A concurrent writer recorded the same event first: idempotent.
                return False
            intent.status = to_status.value
            if to_status is OrderLifecycle.SUBMITTED and intent.submitted_at is None:
                intent.submitted_at = occurred_at
            return True

    def events(self, intent_id: str, *, current_account_id: str) -> tuple[OrderEventRecord, ...]:
        with self._sessions() as session:
            _require_matching_account_scope(session, current_account_id)
            rows = session.scalars(
                select(OrderEventRow)
                .where(OrderEventRow.intent_id == intent_id)
                .order_by(OrderEventRow.sequence)
            )
            return tuple(_event_row_to_record(row) for row in rows)

    @staticmethod
    def _next_sequence(session: Session, intent_id: str) -> int:
        current = session.scalar(
            select(func.max(OrderEventRow.sequence)).where(OrderEventRow.intent_id == intent_id)
        )
        return int(current or 0) + 1


def _event_row_to_record(row: OrderEventRow) -> OrderEventRecord:
    return OrderEventRecord(
        intent_id=row.intent_id,
        event_key=row.event_key,
        sequence=row.sequence,
        source=row.source,
        from_status=OrderLifecycle(row.from_status) if row.from_status is not None else None,
        to_status=OrderLifecycle(row.to_status),
        broker_order_id=row.broker_order_id,
        filled_quantity=row.filled_quantity,
        remaining_quantity=row.remaining_quantity,
        evidence=dict(row.evidence),
        occurred_at=row.occurred_at,
    )


class OrderConfirmationRepository:
    """Persist typed operator confirmations and read the latest one back."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def record(
        self,
        *,
        intent_id: str,
        summary_hash: str,
        current_account_id: str,
        ui_session_id: str | None,
        quote_snapshot_id: str | None,
        risk_snapshot_id: str | None,
        confirmed_at: datetime,
        expires_at: datetime,
    ) -> None:
        with self._sessions.begin() as session:
            _require_matching_account_scope(session, current_account_id)
            if session.get(OrderIntentRow, intent_id) is None:
                raise ValueError(f"Unknown order intent {intent_id!r}")
            session.add(
                OrderConfirmationRow(
                    intent_id=intent_id,
                    summary_hash=summary_hash,
                    ui_session_id=ui_session_id,
                    quote_snapshot_id=quote_snapshot_id,
                    risk_snapshot_id=risk_snapshot_id,
                    confirmed_at=confirmed_at,
                    expires_at=expires_at,
                )
            )

    def latest(self, intent_id: str, *, current_account_id: str) -> OrderConfirmationRecord | None:
        with self._sessions() as session:
            _require_matching_account_scope(session, current_account_id)
            row = session.scalar(
                select(OrderConfirmationRow)
                .where(OrderConfirmationRow.intent_id == intent_id)
                .order_by(OrderConfirmationRow.confirmed_at.desc(), OrderConfirmationRow.id.desc())
                .limit(1)
            )
            if row is None:
                return None
            return OrderConfirmationRecord(
                intent_id=row.intent_id,
                summary_hash=row.summary_hash,
                ui_session_id=row.ui_session_id,
                confirmed_at=row.confirmed_at,
                expires_at=row.expires_at,
            )


class RiskDecisionRepository:
    """Persist a structured risk decision with one row per check."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def store(self, record: RiskDecisionRecord, *, current_account_id: str) -> bool:
        """Idempotent on ``decision_id``; returns False on an exact replay."""

        with self._sessions.begin() as session:
            scope = _require_matching_account_scope(session, current_account_id)
            if record.account_fingerprint != scope.account_fingerprint:
                raise ValueError("Risk decision account does not match the bound database scope")
            if session.get(RiskDecisionRow, record.decision_id) is not None:
                return False
            try:
                with session.begin_nested():
                    session.add(
                        RiskDecisionRow(
                            decision_id=record.decision_id,
                            correlation_id=record.correlation_id,
                            account_fingerprint=record.account_fingerprint,
                            symbol=record.symbol,
                            product_family=record.product_family.value,
                            overall_result=record.overall_result.value,
                            decided_at=record.decided_at,
                            expires_at=record.expires_at,
                            evidence=record.evidence,
                        )
                    )
                    # Flush the parent before the children so the FK from
                    # risk_check_results resolves under SQLite's immediate FK.
                    session.flush()
                    for check in record.checks:
                        session.add(
                            RiskCheckResultRow(
                                decision_id=record.decision_id,
                                sequence=check.sequence,
                                check_name=check.check_name,
                                status=check.status.value,
                                detail=check.detail,
                                evidence=check.evidence,
                            )
                        )
                    session.flush()
            except IntegrityError:
                return False
            return True

    def get(self, decision_id: str, *, current_account_id: str) -> RiskDecisionRecord | None:
        with self._sessions() as session:
            _require_matching_account_scope(session, current_account_id)
            header = session.get(RiskDecisionRow, decision_id)
            if header is None:
                return None
            check_rows = session.scalars(
                select(RiskCheckResultRow)
                .where(RiskCheckResultRow.decision_id == decision_id)
                .order_by(RiskCheckResultRow.sequence)
            )
            checks = tuple(
                RiskCheckRecord(
                    sequence=check.sequence,
                    check_name=check.check_name,
                    status=RiskCheckStatus(check.status),
                    detail=check.detail,
                    evidence=dict(check.evidence),
                )
                for check in check_rows
            )
            return RiskDecisionRecord(
                decision_id=header.decision_id,
                correlation_id=header.correlation_id,
                account_fingerprint=header.account_fingerprint,
                symbol=header.symbol,
                product_family=ProductFamily(header.product_family),
                overall_result=RiskCheckStatus(header.overall_result),
                decided_at=header.decided_at,
                expires_at=header.expires_at,
                evidence=dict(header.evidence),
                checks=checks,
            )
