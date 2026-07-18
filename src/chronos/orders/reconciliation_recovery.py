"""Restart reconciliation and SUBMISSION_UNKNOWN recovery.

On startup, and after any submit whose broker id is unknown, the working
intents are matched to broker truth by their Chronos-owned ``order_ref``
(CHR-) and by ``permId``/executions. A SUBMISSION_UNKNOWN intent is resolved to
its true state — SUBMITTED / PARTIALLY_FILLED / FILLED / REJECTED — and a submit
is NEVER retried automatically: if the order is not found at the broker at all,
it never reached the venue and resolves to REJECTED.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.enums import OrderLifecycle
from chronos.domain.models import BrokerExecution, BrokerOrder
from chronos.orders.tracker import (
    OrderStatusUpdate,
    OrderTracker,
    broker_status_to_lifecycle,
)
from chronos.persistence.order_repositories import (
    OrderIntentRecord,
    OrderIntentRepository,
)

# Intent statuses whose true broker state must be re-derived on restart.
_UNRESOLVED_STATUSES = frozenset(
    {
        OrderLifecycle.SUBMITTED,
        OrderLifecycle.SUBMISSION_UNKNOWN,
        OrderLifecycle.PARTIALLY_FILLED,
        OrderLifecycle.CANCEL_PENDING,
    }
)


def resolve_from_broker_evidence(
    intent: OrderIntentRecord,
    *,
    open_orders: tuple[BrokerOrder, ...],
    executions: tuple[BrokerExecution, ...],
    now: datetime,
) -> OrderStatusUpdate:
    """Derive the true lifecycle for one intent from broker evidence.

    Matching is by the intent's ``order_ref`` (CHR-) only — Chronos never acts
    on an order it does not own. An order absent from both open orders and
    executions never reached the broker and resolves to REJECTED.
    """

    order_ref = intent.order_ref
    working = [order for order in open_orders if order.order_ref == order_ref]
    fills = [execution for execution in executions if execution.order_ref == order_ref]

    if working:
        order = working[0]
        lifecycle = broker_status_to_lifecycle(
            order.lifecycle.value,
            filled_quantity=order.filled_quantity,
            remaining_quantity=order.remaining_quantity,
        )
        return OrderStatusUpdate(
            intent_id=intent.intent_id,
            broker_order_id=order.broker_order_id,
            permanent_id=order.permanent_id,
            lifecycle=lifecycle,
            filled_quantity=order.filled_quantity,
            remaining_quantity=order.remaining_quantity,
            source="RECONCILE",
            occurred_at=now,
        )

    if fills:
        filled = sum((execution.quantity for execution in fills), Decimal("0"))
        remaining = max(intent.quantity - filled, Decimal("0"))
        lifecycle = OrderLifecycle.FILLED if remaining <= 0 else OrderLifecycle.PARTIALLY_FILLED
        return OrderStatusUpdate(
            intent_id=intent.intent_id,
            broker_order_id=fills[0].broker_order_id,
            permanent_id=fills[0].permanent_id,
            lifecycle=lifecycle,
            filled_quantity=filled,
            remaining_quantity=remaining,
            source="RECONCILE",
            occurred_at=now,
        )

    # Not found anywhere: the order never reached the broker. Never re-submit.
    return OrderStatusUpdate(
        intent_id=intent.intent_id,
        lifecycle=OrderLifecycle.REJECTED,
        source="RECONCILE",
        occurred_at=now,
    )


class OrderRestartReconciler:
    """Re-derive and persist the true state of every working intent."""

    def __init__(
        self,
        *,
        connection: BrokerConnectionManager,
        intents: OrderIntentRepository,
        tracker: OrderTracker,
    ) -> None:
        self._connection = connection
        self._intents = intents
        self._tracker = tracker

    def reconcile(self, *, current_account_id: str, now: datetime) -> tuple[OrderStatusUpdate, ...]:
        working = [
            intent
            for intent in self._intents.active(current_account_id=current_account_id)
            if intent.status in _UNRESOLVED_STATUSES
        ]
        if not working:
            return ()
        open_orders = self._connection.run(self._connection.broker.open_orders())
        executions = self._connection.run(self._connection.broker.executions())
        applied: list[OrderStatusUpdate] = []
        for intent in working:
            update = resolve_from_broker_evidence(
                intent,
                open_orders=open_orders,
                executions=executions,
                now=now,
            )
            if self._tracker.ingest(update, current_account_id=current_account_id):
                applied.append(update)
        return tuple(applied)
