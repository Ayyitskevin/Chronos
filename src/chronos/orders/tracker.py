"""Order lifecycle tracker: broker events -> validated, idempotent persistence.

The tracker consumes broker order-status / execution evidence, maps it to an
:class:`~chronos.domain.enums.OrderLifecycle`, validates the transition with the
pure :class:`~chronos.orders.state_machine.OrderLifecycleMachine`, and persists
it atomically through :class:`OrderTrackerRepository`. Duplicate or late broker
callbacks are absorbed: the event key (which folds in the cumulative fill) is
unique, and a stale callback reporting a *lower* cumulative fill is ignored so
fills only ever move forward.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from chronos.domain.enums import OrderLifecycle
from chronos.domain.models import ChronosModel
from chronos.orders.state_machine import OrderLifecycleMachine
from chronos.persistence.order_repositories import (
    OrderIntentRepository,
    OrderTrackerRepository,
)


class OrderStatusUpdate(ChronosModel):
    """One normalized broker order-status observation for a known intent."""

    intent_id: str
    broker_order_id: int | None = None
    permanent_id: int | None = None
    lifecycle: OrderLifecycle
    filled_quantity: Decimal = Decimal("0")
    remaining_quantity: Decimal = Decimal("0")
    source: str = "ORDER_STATUS"
    occurred_at: datetime


def broker_status_to_lifecycle(
    status: str,
    *,
    filled_quantity: Decimal,
    remaining_quantity: Decimal,
) -> OrderLifecycle:
    """Map an IBKR order-status string to a Chronos lifecycle.

    Unknown statuses fail closed to SUBMISSION_UNKNOWN so an unrecognized broker
    state is reconciled rather than assumed benign.
    """

    normalized = status.strip().lower()
    if normalized == "filled" and remaining_quantity <= 0:
        return OrderLifecycle.FILLED
    partially_filled = normalized in {"filled", "partiallyfilled"} or (
        filled_quantity > 0 and remaining_quantity > 0
    )
    if partially_filled:
        return OrderLifecycle.PARTIALLY_FILLED
    if normalized in {"cancelled", "canceled", "apicancelled", "apicanceled"}:
        return OrderLifecycle.CANCELLED
    if normalized in {"pendingcancel"}:
        return OrderLifecycle.CANCEL_PENDING
    if normalized in {"submitted", "presubmitted", "pendingsubmit"}:
        return OrderLifecycle.SUBMITTED
    if normalized in {"inactive", "rejected", "apirejected"}:
        return OrderLifecycle.REJECTED
    return OrderLifecycle.SUBMISSION_UNKNOWN


class OrderTracker:
    """Drive lifecycle persistence from normalized broker observations."""

    def __init__(
        self,
        intents: OrderIntentRepository,
        tracker_repo: OrderTrackerRepository,
    ) -> None:
        self._intents = intents
        self._tracker = tracker_repo

    def ingest(self, update: OrderStatusUpdate, *, current_account_id: str) -> bool:
        """Apply one broker observation. Returns False on a no-op/duplicate/stale event."""

        intent = self._intents.get(update.intent_id, current_account_id=current_account_id)
        if intent is None:
            raise ValueError(f"Unknown order intent {update.intent_id!r}")

        prior_filled = self._latest_filled(update.intent_id, current_account_id)
        if update.filled_quantity < prior_filled:
            # A stale/out-of-order callback that would move fills backward.
            return False

        machine = OrderLifecycleMachine(intent.status)
        # apply() raises OrderLifecycleError on a contradiction (surfaced to the
        # caller); returns False for a benign no-op we can skip.
        if not machine.apply(update.lifecycle):
            return False

        event_key = (
            f"{update.intent_id}:{update.broker_order_id}:"
            f"{update.lifecycle.value}:{update.filled_quantity}"
        )
        return self._tracker.record_transition(
            intent_id=update.intent_id,
            event_key=event_key,
            source=update.source,
            from_status=intent.status,
            to_status=update.lifecycle,
            current_account_id=current_account_id,
            broker_order_id=update.broker_order_id,
            filled_quantity=update.filled_quantity,
            remaining_quantity=update.remaining_quantity,
            evidence={
                "permanent_id": update.permanent_id,
                "occurred_at": update.occurred_at.isoformat(),
            },
            occurred_at=update.occurred_at,
        )

    def _latest_filled(self, intent_id: str, current_account_id: str) -> Decimal:
        events = self._tracker.events(intent_id, current_account_id=current_account_id)
        filled = [event.filled_quantity for event in events if event.filled_quantity is not None]
        return max(filled) if filled else Decimal("0")

    def broker_order_id(self, intent_id: str, *, current_account_id: str) -> int | None:
        """The most recent broker order id recorded for this intent, if any."""

        events = self._tracker.events(intent_id, current_account_id=current_account_id)
        for event in reversed(events):
            if event.broker_order_id is not None:
                return event.broker_order_id
        return None
