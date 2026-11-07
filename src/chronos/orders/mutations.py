"""Order modification, cancellation, and the buy-to-close intent builder.

Modification is limit-price only (``OrderModification`` carries nothing else)
and re-places on the same ``broker_order_id`` — it is NOT a lifecycle
transition, so the order stays SUBMITTED/PARTIALLY_FILLED and only a MODIFIED
event is recorded. Cancellation drives SUBMITTED/PARTIALLY_FILLED ->
CANCEL_PENDING and, when the broker confirms in the same call, -> CANCELLED;
otherwise the confirming callback resolves it via the tracker. Ownership rests
on the service-layer tracker lookup: only broker ids Chronos recorded for its
own intents ever reach these services.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from chronos.broker.base import BrokerRefusedBeforeSend
from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.enums import OrderIntent, OrderLifecycle
from chronos.domain.models import (
    CancellationResult,
    OptionContract,
    OrderModification,
    OrderSubmission,
)
from chronos.orders.intent import WheelOrderIntent, build_option_intent
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.state_machine import is_terminal
from chronos.orders.tracker import OrderTracker
from chronos.persistence.order_repositories import (
    OrderIntentRepository,
    OrderTrackerRepository,
    RecordedFillsAdvanced,
)

_MODIFIABLE_STATUSES = frozenset({OrderLifecycle.SUBMITTED, OrderLifecycle.PARTIALLY_FILLED})
_CANCELLABLE_STATUSES = frozenset({OrderLifecycle.SUBMITTED, OrderLifecycle.PARTIALLY_FILLED})
logger = logging.getLogger(__name__)


class OrderMutationError(RuntimeError):
    """A modify/cancel request is not valid for the order's current state."""


class OrderModificationService:
    """Re-place a working order at a new limit price on the same broker id.

    LIVE refusal (ADR-0009 §7): a modify re-prices a working order at the venue
    through none of the ten live gates, and the per-order confirmation hash
    binds the ORIGINAL limit price — so for a live environment this service
    refuses outright. The live re-pricing workflow is cancel + re-propose +
    the full gate walk. Cancellation stays available (risk-reducing).
    """

    def __init__(
        self,
        *,
        connection: BrokerConnectionManager,
        intents: OrderIntentRepository,
        tracker: OrderTracker,
        tracker_repo: OrderTrackerRepository,
        live_environment: bool = False,
    ) -> None:
        self._connection = connection
        self._intents = intents
        self._tracker = tracker
        self._tracker_repo = tracker_repo
        self._live_environment = live_environment

    def modify(
        self,
        intent_id: str,
        new_limit_price: Decimal,
        *,
        current_account_id: str,
        now: datetime,
    ) -> OrderSubmission:
        if self._live_environment:
            raise OrderMutationError(
                "live order modification is refused (ADR-0009 §7): the typed "
                "confirmation binds the original limit price and a modify walks "
                "zero live gates — cancel the working order and re-propose at "
                "the new price instead"
            )
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
        # Record a MODIFIED event without changing the lifecycle status. A true CAS
        # (RECHECK-355 P3-4): a fill or cancel that landed while the broker call was in
        # flight must not be put back to the status read before the call.
        self._record_modified(
            intent_id=intent_id,
            broker_order_id=broker_order_id,
            new_limit_price=new_limit_price,
            expected_status=intent.status,
            current_account_id=current_account_id,
            now=now,
        )
        return submission

    def _record_modified(
        self,
        *,
        intent_id: str,
        broker_order_id: int,
        new_limit_price: Decimal,
        expected_status: OrderLifecycle,
        current_account_id: str,
        now: datetime,
    ) -> None:
        """Write the MODIFIED event after the broker call, never silently skipping it.

        REVIEW-TIDY P3-3. The broker has already re-priced the order and the intent row does not
        carry the price, so this event is the only durable record of it. When the compare-and-set
        writes nothing, re-read the status to tell why:

        * unchanged: the event key was already recorded (the same price asked twice); benign,
          logged as a duplicate, not as a moved status.
        * moved to another working status (a partial fill): retry once from the current status.
          Same event type, same evidence; the order is still working at the new limit.
        * moved to a terminal status: raise, as the cancel completion does for a contradicting
          terminal status. The recorded status stands, nothing is written over a settled order,
          and the operator sees a 409 telling them to reconcile. No readiness freeze is added
          here (D-69 is an open decision about freezes), and no new event type.
        """

        evidence = {"new_limit_price": format(new_limit_price, "f")}
        event_key = f"{intent_id}:{broker_order_id}:MODIFIED:{new_limit_price}"

        def write(from_status: OrderLifecycle) -> bool:
            return self._tracker_repo.record_transition(
                intent_id=intent_id,
                event_key=event_key,
                source="MODIFY",
                from_status=from_status,
                to_status=from_status,
                current_account_id=current_account_id,
                broker_order_id=broker_order_id,
                evidence=evidence,
                occurred_at=now,
                enforce_from_status=True,
            )

        if write(expected_status):
            return
        current = self._intents.get(intent_id, current_account_id=current_account_id)
        status = current.status if current is not None else None
        if status is expected_status:
            logger.info(
                "modify_event_duplicate: this modify was already recorded for this order",
                extra={"event": "modify_event_duplicate", "intent_id": intent_id},
            )
            return
        if status is not None and not is_terminal(status) and write(status):
            logger.warning(
                "modify_status_moved: recorded from the current status after a move in flight",
                extra={"event": "modify_status_moved", "intent_id": intent_id},
            )
            return
        logger.warning(
            "modify_status_moved: the re-price was not recorded",
            extra={"event": "modify_status_moved", "intent_id": intent_id},
        )
        shown = status.value if status is not None else "unknown"
        raise OrderMutationError(
            f"the modify reached the broker (new limit {format(new_limit_price, 'f')}) but the "
            f"order is now recorded as {shown}; the re-price was not recorded and the recorded "
            "status stands: reconcile before acting"
        )


class OrderCancellationService:
    """Request cancellation, moving through CANCEL_PENDING.

    DELIBERATELY un-gated by arming/kill-switch on every branch (ADR-0009 §7):
    cancellation is risk-reducing and must work precisely when the kill switch
    is engaged — an emergency stop halts new exposure AND pulls working orders.
    It still requires the writer lease (enforced at the API layer) and the
    account scope (enforced here by the repositories).
    """

    def __init__(
        self,
        *,
        connection: BrokerConnectionManager,
        intents: OrderIntentRepository,
        tracker: OrderTracker,
        tracker_repo: OrderTrackerRepository,
        reconciliation_readiness: ReconciliationReadiness,
    ) -> None:
        self._connection = connection
        self._intents = intents
        self._tracker = tracker
        self._tracker_repo = tracker_repo
        self._reconciliation_readiness = reconciliation_readiness

    def cancel(
        self,
        intent_id: str,
        *,
        current_account_id: str,
        now: datetime,
    ) -> CancellationResult:
        intent = self._intents.get(intent_id, current_account_id=current_account_id)
        if intent is not None and intent.status is OrderLifecycle.CANCEL_PENDING:
            raise OrderMutationError(
                "a cancel is already pending for this order (or was interrupted); if the order "
                "still works at the venue, an interrupted cancel is recovered automatically only "
                "on an adapter that supports it, and only after a wait; otherwise reconcile the "
                "order or cancel it at the broker"
            )
        if intent is None or intent.status not in _CANCELLABLE_STATUSES:
            raise OrderMutationError("order is not in a cancellable state")
        broker_order_id = self._tracker.broker_order_id(
            intent_id, current_account_id=current_account_id
        )
        if broker_order_id is None:
            raise OrderMutationError("order has no broker order id to cancel")
        # Mark CANCEL_PENDING first so an in-flight cancel is durably visible. The key carries the
        # attempt time: a retry after a refusal must be recorded again (a fixed key would be a
        # duplicate and write nothing, so the retry would send with no durable CANCEL_PENDING).
        # The write is a compare-and-set against the status read above: if another cancel (or a
        # broker callback) moved the order since, or this attempt was already recorded, nothing is
        # written and NOTHING IS SENT. Only the attempt that wins the mark may later revert it.
        attempt = now.isoformat()
        prior_status = intent.status
        recorded = self._tracker_repo.record_transition(
            intent_id=intent_id,
            event_key=f"{intent_id}:{broker_order_id}:CANCEL_PENDING:{attempt}",
            source="CANCEL",
            from_status=prior_status,
            to_status=OrderLifecycle.CANCEL_PENDING,
            current_account_id=current_account_id,
            broker_order_id=broker_order_id,
            evidence={"phase": "cancel_requested"},
            occurred_at=now,
            enforce_from_status=True,
        )
        if not recorded:
            raise OrderMutationError(
                "cancel not recorded: the order's status changed or this cancel was already "
                "recorded; no cancel was sent"
            )
        try:
            result = self._connection.run(self._connection.broker.cancel_order(broker_order_id))
        except BrokerRefusedBeforeSend as refusal:
            # The broker PROVES nothing was sent: the order is still working. Any other
            # failure is ambiguous (the send may have left) and keeps CANCEL_PENDING.
            self._record_refusal(
                intent_id, broker_order_id, prior_status, str(refusal), current_account_id, now
            )
            raise OrderMutationError(f"cancel refused before send: {refusal}") from refusal
        if not result.requested and result.lifecycle is not OrderLifecycle.CANCELLED:
            self._record_refusal(
                intent_id, broker_order_id, prior_status, result.message, current_account_id, now
            )
            raise OrderMutationError(f"cancel refused: {result.message}")
        if result.lifecycle is OrderLifecycle.CANCELLED:
            self._complete_cancel(intent_id, broker_order_id, result, current_account_id)
        return result

    def _complete_cancel(
        self,
        intent_id: str,
        broker_order_id: int,
        result: CancellationResult,
        current_account_id: str,
    ) -> None:
        """Record ``CANCEL_PENDING -> CANCELLED`` as a compare-and-set, never over a raced callback.

        A broker callback can land while the cancel is in flight. If it moved the order, the
        recorded broker status stands and this write loses: already ``CANCELLED`` is idempotent; a
        different terminal status (FILLED, REJECTED) contradicts the cancel answer, so it is
        surfaced instead of written over; a non-terminal status (a partial fill) is left to the
        venue's own callbacks, which carry the final status.
        """

        try:
            written = self._tracker_repo.record_transition(
                intent_id=intent_id,
                event_key=f"{intent_id}:{broker_order_id}:CANCELLED",
                source="CANCEL",
                from_status=OrderLifecycle.CANCEL_PENDING,
                to_status=OrderLifecycle.CANCELLED,
                current_account_id=current_account_id,
                broker_order_id=broker_order_id,
                evidence={"message": result.message},
                occurred_at=result.timestamp,
                filled_quantity=result.filled_quantity,
                enforce_from_status=True,
            )
        except RecordedFillsAdvanced as conflict:
            recorded = format(conflict.recorded.normalize(), "f")
            attempted = format(conflict.attempted.normalize(), "f")
            reason = (
                f"cancel acknowledgement reports {attempted} filled, below the {recorded} "
                "already recorded; the order remains unresolved; reconcile before acting"
            )
            logger.warning(
                "cancel acknowledgement fill floor conflict: attempted=%s recorded=%s",
                attempted,
                recorded,
            )
            self._reconciliation_readiness.invalidate(
                f"cancel acknowledgement fill floor conflict: {attempted} below {recorded}"
            )
            raise OrderMutationError(reason) from conflict
        if written:
            return
        intent = self._intents.get(intent_id, current_account_id=current_account_id)
        status = intent.status if intent is not None else None
        if status is not None and status is not OrderLifecycle.CANCELLED and is_terminal(status):
            raise OrderMutationError(
                f"the cancel answered CANCELLED but the order is already recorded as "
                f"{status.value}; the recorded status stands: reconcile before acting"
            )

    def _record_refusal(
        self,
        intent_id: str,
        broker_order_id: int,
        prior_status: OrderLifecycle,
        message: str,
        current_account_id: str,
        now: datetime,
    ) -> None:
        """Compensating event: ``CANCEL_PENDING`` back to the status the order held.

        A compare-and-set (``enforce_from_status``): if a broker callback already moved the
        intent (filled, cancelled), nothing is written and that status stands. The pure
        state-machine table has no ``CANCEL_PENDING -> SUBMITTED`` edge; this repository write
        does not consult it, and later broker updates read the persisted (reverted) status.
        """

        self._tracker_repo.record_transition(
            intent_id=intent_id,
            event_key=f"{intent_id}:{broker_order_id}:CANCEL_REFUSED:{now.isoformat()}",
            source="CANCEL",
            from_status=OrderLifecycle.CANCEL_PENDING,
            to_status=prior_status,
            current_account_id=current_account_id,
            broker_order_id=broker_order_id,
            evidence={"phase": "cancel_refused", "message": message},
            occurred_at=now,
            enforce_from_status=True,
        )


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
