"""Broker executions persist as executions (BP-1).

The persistence ``fills`` + ``commissions`` tables are the canonical per-execution home
(OWNER-ASKS 3, ruled by Muse 2026-09-19); the execution plane's sqlite ledger stays a
DERIVED, per-intent view and is untouched. :class:`ExecutionRepository` writes ONE
:class:`FillRow` per broker ``execution_id`` — with the broker's ``permanent_id``, the
placing ``client_id``, Chronos's echoed ``order_ref``, price/quantity/multiplier/currency
and the scope's pseudonymous ``account_fingerprint`` (never the raw account id) — and ONE
:class:`CommissionRow` when the broker reported a commission (amount AND currency, which
:class:`~chronos.domain.models.BrokerExecution` already refuses to carry apart).

Replay rule (the ``event_key`` idempotency of ``order_repositories``): a second ``record``
of the same ``execution_id`` with the same facts returns ``False`` and writes nothing; the
same ``execution_id`` with a different fact is :class:`ExecutionConflict` naming the field —
the row is never overwritten. The IDENTITY compared is the immutable broker-fact set
(ruling R-b, BP-1 r2): execution_id + permanent_id + client_id + order_ref + contract
identity (symbol, contract_id, security_type, currency) + side + quantity + price +
multiplier + timestamp + commission amount + commission currency. The CALLER's linkage
(correlation_id, wheel_cycle_id) is NOT part of the identity: reconciliation observes the
same execution many times, under whatever correlation the caller has at that moment, so a
second observation returns ``False`` and keeps the first linkage.

Concurrency: the insert runs under ``session.begin_nested()``; an ``IntegrityError`` from a
concurrent writer is caught, the winner re-read and compared — identical → ``False``,
divergent → :class:`ExecutionConflict` — so two identical inserts racing return True/False
with one row, never an escaping ``IntegrityError``.

Every persisted string — execution_id, order_ref, symbol, security_type, side, currency,
commission_currency, correlation_id, wheel_cycle_id — passes ``_reject_raw_account_event_data``
before any write: a raw broker account id never reaches the table through any field.

Authority line (DESIGN.md §2.7): this module imports nothing from ``chronos.orders.risk``,
``orders.submission``, ``autonomy``, ``supervisor``, ``control`` or ``execution``, and none
of them import it — persisting an execution never feeds risk, submission or admission.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from chronos.domain.models import BrokerExecution
from chronos.persistence.repositories import (
    _reject_raw_account_event_data,
    _require_matching_account_scope,
)
from chronos.persistence.schema import CommissionRow, FillRow, OrderDraftRow, SubmittedOrderRow


class ExecutionConflict(ValueError):
    """The same ``execution_id`` arrived with different facts; nothing was written."""


class UnknownCorrelation(ValueError):
    """A caller named a ``correlation_id`` that is not an order draft; nothing was written.

    Raised BEFORE any insert (BP-1b, Muse item 2): ``fills.correlation_id`` is a foreign key to
    ``order_drafts.correlation_id``, and the database's refusal would otherwise surface as a raw
    ``IntegrityError`` from inside the write. The caller's linkage is checked as a fact of its
    own so the writer's refusals stay typed.
    """

    def __init__(self, execution_id: str, correlation_id: str) -> None:
        super().__init__(
            f"execution {execution_id} names correlation_id {correlation_id!r}, which is not "
            "an order draft; nothing was written"
        )
        self.execution_id = execution_id
        self.correlation_id = correlation_id


@dataclass(frozen=True, slots=True)
class _Facts:
    """The immutable broker-fact identity of an execution (ruling R-b), in the order a
    conflict is reported. The caller's linkage (correlation_id, wheel_cycle_id) is not here."""

    broker_order_id: int
    permanent_id: int | None
    client_id: (
        int | None
    )  # None only on a row from before 0013 — then any new observation conflicts
    order_ref: str | None
    symbol: str
    contract_id: int
    security_type: str
    side: str
    quantity: Decimal
    price: Decimal
    multiplier: Decimal
    currency: str
    timestamp: datetime
    commission: Decimal | None
    commission_currency: str | None


def _facts_of(execution: BrokerExecution) -> _Facts:
    contract = execution.contract
    multiplier = getattr(contract, "multiplier", None)
    return _Facts(
        broker_order_id=execution.broker_order_id,
        permanent_id=execution.permanent_id,
        client_id=execution.client_id,
        order_ref=execution.order_ref,
        symbol=contract.symbol,
        contract_id=contract.con_id,
        security_type=str(contract.security_type.value),
        side=str(execution.side.value),
        quantity=Decimal(execution.quantity),
        price=Decimal(execution.price),
        multiplier=Decimal(multiplier) if multiplier is not None else Decimal(1),
        currency=str(contract.currency),
        timestamp=_utc(execution.timestamp),
        commission=execution.commission,
        commission_currency=execution.commission_currency,
    )


def _utc(value: datetime) -> datetime:
    """Compare instants, not spellings: the column stores UTC; a tz-aware input is
    normalised to UTC, a naive one (the column's read-back) is taken as UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _facts_of_row(fill: FillRow, commission: CommissionRow | None) -> _Facts:
    return _Facts(
        broker_order_id=fill.broker_order_id,
        permanent_id=fill.permanent_id,
        client_id=fill.client_id,
        order_ref=fill.order_ref,
        symbol=fill.symbol,
        contract_id=fill.contract_id,
        security_type=fill.security_type,
        side=fill.side,
        quantity=Decimal(fill.quantity),
        price=Decimal(fill.price),
        multiplier=Decimal(fill.multiplier),
        currency=fill.currency,
        timestamp=_utc(fill.occurred_at),
        commission=Decimal(commission.amount) if commission is not None else None,
        commission_currency=commission.currency if commission is not None else None,
    )


def _require_same_facts(session: Session, stored_row: FillRow, facts: _Facts) -> None:
    stored_commission = session.scalar(
        select(CommissionRow).where(CommissionRow.execution_id == stored_row.execution_id)
    )
    stored = _facts_of_row(stored_row, stored_commission)
    for field in _Facts.__dataclass_fields__:
        if getattr(stored, field) != getattr(facts, field):
            raise ExecutionConflict(
                f"execution {stored_row.execution_id} was already recorded with a different "
                f"{field}: stored {getattr(stored, field)!r}, received {getattr(facts, field)!r}; "
                "the row is never overwritten"
            )


class ExecutionRepository:
    """Idempotent, execution-keyed writer for ``fills`` + ``commissions``."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def linked_correlation_id(self, order_ref: str | None) -> str | None:
        """The draft an execution's ``order_ref`` maps to, or None (the foreign/unmatched case).

        The only path at this head is ``submitted_orders.order_ref`` (unique) → its
        ``correlation_id`` (the foreign key to ``order_drafts``). A read of a linkage string,
        never a decision input: the caller stores it beside the execution and nothing else.
        """

        if order_ref is None or not order_ref.strip():
            return None
        with self._sessions() as session:
            return session.scalar(
                select(SubmittedOrderRow.correlation_id).where(
                    SubmittedOrderRow.order_ref == order_ref
                )
            )

    def record(
        self,
        execution: BrokerExecution,
        *,
        correlation_id: str | None,
        wheel_cycle_id: str | None = None,
    ) -> bool:
        """Persist one broker execution once.

        Returns ``True`` when the rows were written, ``False`` when an identical execution
        was already there (nothing written). The account must be the database's bound scope
        (:func:`_require_matching_account_scope`); no string field may carry a raw broker
        account id (:func:`_reject_raw_account_event_data`); a differing replay is
        :class:`ExecutionConflict`.
        """

        facts = _facts_of(execution)
        _reject_raw_account_event_data(
            persisted_values=[
                execution.execution_id,
                facts.order_ref or "",
                facts.symbol,
                facts.security_type,
                facts.side,
                facts.currency,
                facts.commission_currency or "",
                correlation_id or "",
                wheel_cycle_id or "",
            ]
        )
        with self._sessions.begin() as session:
            scope = _require_matching_account_scope(session, execution.account_id)
            existing = session.get(FillRow, execution.execution_id)
            if existing is not None:
                _require_same_facts(session, existing, facts)
                return False
            # BP-1b: the caller's linkage is checked before any insert, so an unknown draft is
            # a typed refusal of this repository and never the database's IntegrityError.
            if correlation_id is not None and session.get(OrderDraftRow, correlation_id) is None:
                raise UnknownCorrelation(execution.execution_id, correlation_id)
            fill = FillRow(
                execution_id=execution.execution_id,
                correlation_id=correlation_id,
                broker_order_id=facts.broker_order_id,
                permanent_id=facts.permanent_id,
                client_id=facts.client_id,
                order_ref=facts.order_ref,
                wheel_cycle_id=wheel_cycle_id,
                symbol=facts.symbol,
                contract_id=facts.contract_id,
                security_type=facts.security_type,
                side=facts.side,
                quantity=facts.quantity,
                price=facts.price,
                multiplier=facts.multiplier,
                currency=facts.currency,
                account_fingerprint=scope.account_fingerprint,
                occurred_at=execution.timestamp,
            )
            commission = None
            if facts.commission is not None:
                # the model already refuses an amount without a currency; the table refuses a
                # currency-less row too (NOT NULL) — stored together or not at all
                assert facts.commission_currency is not None
                commission = CommissionRow(
                    execution_id=execution.execution_id,
                    amount=facts.commission,
                    currency=facts.commission_currency,
                    received_at=execution.timestamp,
                )
            try:
                with session.begin_nested():
                    session.add(fill)
                    session.flush()  # the fill row exists before the commission that references it
                    if commission is not None:
                        session.add(commission)
                        session.flush()
            except IntegrityError:
                # A concurrent writer recorded this execution first: re-read the winner and
                # compare — identical → idempotent False, divergent → the typed conflict.
                session.expire_all()
                winner = session.get(FillRow, execution.execution_id)
                if winner is None:  # pragma: no cover — a different constraint fired
                    raise
                _require_same_facts(session, winner, facts)
                return False
        return True


__all__ = ["ExecutionConflict", "ExecutionRepository", "UnknownCorrelation"]
