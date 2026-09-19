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
same ``execution_id`` with a different quantity, price, commission or identity is
:class:`ExecutionConflict` naming the field — the row is never overwritten.

Authority line (DESIGN.md §2.7): this module imports nothing from ``chronos.orders.risk``,
``orders.submission``, ``autonomy``, ``supervisor``, ``control`` or ``execution``, and none
of them import it — persisting an execution never feeds risk, submission or admission.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from chronos.domain.models import BrokerExecution
from chronos.persistence.repositories import (
    _reject_raw_account_event_data,
    _require_matching_account_scope,
)
from chronos.persistence.schema import CommissionRow, FillRow


class ExecutionConflict(ValueError):
    """The same ``execution_id`` arrived with different facts; nothing was written."""


@dataclass(frozen=True, slots=True)
class _Facts:
    """The comparable facts of a fill row, in the order a conflict is reported."""

    broker_order_id: int
    permanent_id: int | None
    client_id: int
    order_ref: str | None
    symbol: str
    contract_id: int
    security_type: str
    side: str
    quantity: Decimal
    price: Decimal
    multiplier: Decimal
    currency: str
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
        commission=execution.commission,
        commission_currency=execution.commission_currency,
    )


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
        commission=Decimal(commission.amount) if commission is not None else None,
        commission_currency=commission.currency if commission is not None else None,
    )


class ExecutionRepository:
    """Idempotent, execution-keyed writer for ``fills`` + ``commissions``."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

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
                correlation_id or "",
                wheel_cycle_id or "",
            ]
        )
        with self._sessions.begin() as session:
            scope = _require_matching_account_scope(session, execution.account_id)
            existing = session.get(FillRow, execution.execution_id)
            if existing is not None:
                stored_commission = session.scalar(
                    select(CommissionRow).where(
                        CommissionRow.execution_id == execution.execution_id
                    )
                )
                stored = _facts_of_row(existing, stored_commission)
                for field in _Facts.__dataclass_fields__:
                    if getattr(stored, field) != getattr(facts, field):
                        raise ExecutionConflict(
                            f"execution {execution.execution_id} was already recorded with "
                            f"a different {field}: stored {getattr(stored, field)!r}, "
                            f"received {getattr(facts, field)!r}; the row is never overwritten"
                        )
                return False
            session.add(
                FillRow(
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
            )
            session.flush()  # the fill row exists before the commission row that references it
            if facts.commission is not None:
                # the model already refuses an amount without a currency; the table refuses a
                # currency-less row too (NOT NULL) — stored together or not at all
                assert facts.commission_currency is not None
                session.add(
                    CommissionRow(
                        execution_id=execution.execution_id,
                        amount=facts.commission,
                        currency=facts.commission_currency,
                        received_at=execution.timestamp,
                    )
                )
        return True


__all__ = ["ExecutionConflict", "ExecutionRepository"]
