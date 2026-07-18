"""Order modification, cancellation, and the buy-to-close intent builder.

Modification is limit-price only (``OrderModification`` carries nothing else)
and re-places on the same ``broker_order_id`` — it is NOT a lifecycle
transition, so the order stays SUBMITTED/PARTIALLY_FILLED and only a MODIFIED
event is recorded. Cancellation drives SUBMITTED/PARTIALLY_FILLED ->
CANCEL_PENDING and, when the broker confirms in the same call, -> CANCELLED;
otherwise the confirming callback resolves it via the tracker. CHR- ownership
is enforced by the adapter, which refuses any order it does not own.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.enums import OrderIntent, OrderLifecycle
from chronos.domain.models import (
    CancellationResult,
    OptionContract,
    OrderModification,
    OrderSubmission,
)
from chronos.orders.intent import WheelOrderIntent, build_option_intent
from chronos.orders.tracker import OrderTracker
from chronos.persistence.order_repositories import (
    OrderIntentRepository,
    OrderTrackerRepository,
)

_MODIFIABLE_STATUSES = frozenset({OrderLifecycle.SUBMITTED, OrderLifecycle.PARTIALLY_FILLED})
_CANCELLABLE_STATUSES = frozenset({OrderLifecycle.SUBMITTED, OrderLifecycle.PARTIALLY_FILLED})


class OrderMutationError(RuntimeError):
    """A modify/cancel request is not valid for the order's current state."""


class OrderModificationService:
    """Re-place a working order at a new limit price on the same broker id."""

    def __init__(
        self,
        *,
        connection: BrokerConnectionManager,
        intents: OrderIntentRepository,
        tracker: OrderTracker,
        tracker_repo: OrderTrackerRepository,
    ) -> None:
        self._connection = connection
        self._intents = intents
        self._tracker = tracker
        self._tracker_repo = tracker_repo

    def modify(
        self,
        intent_id: str,
        new_limit_price: Decimal,
        *,
        current_account_id: str,
        now: datetime,
    ) -> OrderSubmission:
        if new_limit_price <= 0:
            raise OrderMutationError("new limit price must be positive")
        intent = self._intents.get(intent_id, current_account_id=current_account_id)
        if intent is None or intent.status not in _MODIFIABLE_STATUSES:
            raise OrderMutationError("order is not in a modifiable state")
        broker_order_id = self._tracker.broker_order_id(
            intent_id, current_account_id=current_account_id
        )
        if broker_order_id is None:
            raise OrderMutationError("order has no broker order id to modify")
        submission = self._connection.run(
            self._connection.broker.modify_order(
                OrderModification(
                    correlation_id=intent.order_ref or intent_id,
                    broker_order_id=broker_order_id,
                    new_limit_price=new_limit_price,
                )
            )
        )
        # Record a MODIFIED event without changing the lifecycle status.
        self._tracker_repo.record_transition(
            intent_id=intent_id,
            event_key=f"{intent_id}:{broker_order_id}:MODIFIED:{new_limit_price}",
            source="MODIFY",
            from_status=intent.status,
            to_status=intent.status,
            current_account_id=current_account_id,
            broker_order_id=broker_order_id,
            evidence={"new_limit_price": format(new_limit_price, "f")},
            occurred_at=now,
        )
        return submission


class OrderCancellationService:
    """Request cancellation, moving through CANCEL_PENDING."""

    def __init__(
        self,
        *,
        connection: BrokerConnectionManager,
        intents: OrderIntentRepository,
        tracker: OrderTracker,
        tracker_repo: OrderTrackerRepository,
    ) -> None:
        self._connection = connection
        self._intents = intents
        self._tracker = tracker
        self._tracker_repo = tracker_repo

    def cancel(
        self,
        intent_id: str,
        *,
        current_account_id: str,
        now: datetime,
    ) -> CancellationResult:
        intent = self._intents.get(intent_id, current_account_id=current_account_id)
        if intent is None or intent.status not in _CANCELLABLE_STATUSES:
            raise OrderMutationError("order is not in a cancellable state")
        broker_order_id = self._tracker.broker_order_id(
            intent_id, current_account_id=current_account_id
        )
        if broker_order_id is None:
            raise OrderMutationError("order has no broker order id to cancel")
        # Mark CANCEL_PENDING first so an in-flight cancel is durably visible.
        self._tracker_repo.record_transition(
            intent_id=intent_id,
            event_key=f"{intent_id}:{broker_order_id}:CANCEL_PENDING",
            source="CANCEL",
            from_status=intent.status,
            to_status=OrderLifecycle.CANCEL_PENDING,
            current_account_id=current_account_id,
            broker_order_id=broker_order_id,
            evidence={"phase": "cancel_requested"},
            occurred_at=now,
        )
        result = self._connection.run(self._connection.broker.cancel_order(broker_order_id))
        if result.lifecycle is OrderLifecycle.CANCELLED:
            self._tracker_repo.record_transition(
                intent_id=intent_id,
                event_key=f"{intent_id}:{broker_order_id}:CANCELLED",
                source="CANCEL",
                from_status=OrderLifecycle.CANCEL_PENDING,
                to_status=OrderLifecycle.CANCELLED,
                current_account_id=current_account_id,
                broker_order_id=broker_order_id,
                evidence={"message": result.message},
                occurred_at=result.timestamp,
            )
        return result


def build_buy_to_close_intent(
    *,
    account_id: str,
    contract: OptionContract,
    quantity: int,
    limit_price: Decimal,
    correlation_id: str | None = None,
) -> WheelOrderIntent:
    """Build the intent that closes an existing short option (BUY to close)."""

    return build_option_intent(
        account_id=account_id,
        intent=OrderIntent.CLOSE_SHORT_OPTION,
        contract=contract,
        quantity=quantity,
        limit_price=limit_price,
        correlation_id=correlation_id,
    )
