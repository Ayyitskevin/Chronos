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
from decimal import Decimal, InvalidOperation

from chronos.domain.enums import OrderLifecycle
from chronos.domain.models import ChronosModel
from chronos.orders.state_machine import OrderLifecycleMachine
from chronos.persistence.order_repositories import (
    OrderEventRecord,
    OrderIntentRepository,
    OrderTrackerRepository,
)


class OrderIdentityConflict(ValueError):
    """Two distinct non-null broker permIds were persisted for one intent (BP-2 r1, R-e).

    Reconciliation treats the intent as UNRESOLVED with this reason — never the
    latest value, never the first; the contradiction is evidence, not a choice.
    """

    def __init__(self, intent_id: str, permanent_ids: tuple[int, ...]) -> None:
        self.intent_id = intent_id
        self.permanent_ids = permanent_ids
        listed = " and ".join(str(value) for value in permanent_ids)
        super().__init__(
            f"identity conflict: intent {intent_id!r} persisted permanent ids {listed} disagree"
        )


class OrderStatusUpdate(ChronosModel):
    """One normalized broker order-status observation for a known intent."""

    intent_id: str
    broker_order_id: int | None = None
    permanent_id: int | None = None
    client_id: int | None = None
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
    # Terminal/administrative statuses are decided FIRST: an order that partially
    # filled and was then cancelled/rejected reports filled>0 AND remaining>0, so
    # the generic partial-fill heuristic below would otherwise misclassify it as
    # PARTIALLY_FILLED and it would never reach a terminal state.
    if normalized in {"cancelled", "canceled", "apicancelled", "apicanceled"}:
        return OrderLifecycle.CANCELLED
    if normalized in {"inactive", "rejected", "apirejected"}:
        return OrderLifecycle.REJECTED
    if normalized in {"pendingcancel"}:
        return OrderLifecycle.CANCEL_PENDING
    if normalized == "filled" and remaining_quantity <= 0:
        return OrderLifecycle.FILLED
    partially_filled = normalized in {"filled", "partiallyfilled"} or (
        filled_quantity > 0 and remaining_quantity > 0
    )
    if partially_filled:
        return OrderLifecycle.PARTIALLY_FILLED
    if normalized in {"submitted", "presubmitted", "pendingsubmit"}:
        return OrderLifecycle.SUBMITTED
    return OrderLifecycle.SUBMISSION_UNKNOWN


def _latest(events: tuple[OrderEventRecord, ...], field: str) -> int | None:
    for event in reversed(events):
        value = getattr(event, field)
        if value is not None:
            return int(value)
    return None


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

        # The monotonic stale-fill guard applies ONLY to fill-progress updates:
        # a fill callback reporting a lower cumulative fill than already seen is
        # stale and ignored. Terminal/administrative transitions (CANCELLED,
        # REJECTED, CANCEL_PENDING) must NOT be dropped for carrying a lower
        # filled_quantity — the state machine decides their legality instead, so
        # a real terminal resolution is applied (or a contradiction surfaces).
        if update.lifecycle in {OrderLifecycle.PARTIALLY_FILLED, OrderLifecycle.FILLED}:
            prior_filled = self._latest_filled(update.intent_id, current_account_id)
            if update.filled_quantity < prior_filled:
                return False

        machine = OrderLifecycleMachine(intent.status)
        # apply() raises OrderLifecycleError on a contradiction (surfaced to the
        # caller); returns False for a benign no-op we can skip — unless a
        # same-status callback newly supplies the broker identity (R-d).
        if not machine.apply(update.lifecycle):
            if update.lifecycle is not intent.status:
                return False  # an out-of-order message after a terminal state: absorb it
            return self._record_identity_refinement(update, intent.status, current_account_id)

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
            # the broker's order identity lands as columns (BP-2) — the evidence
            # JSON keeps its copy for readers that predate the columns
            permanent_id=update.permanent_id,
            client_id=update.client_id,
            filled_quantity=update.filled_quantity,
            remaining_quantity=update.remaining_quantity,
            evidence={
                "permanent_id": update.permanent_id,
                "occurred_at": update.occurred_at.isoformat(),
            },
            occurred_at=update.occurred_at,
            enforce_from_status=True,
        )

    def _record_identity_refinement(
        self, update: OrderStatusUpdate, status: OrderLifecycle, current_account_id: str
    ) -> bool:
        """A same-status callback that first supplies permId/clientId (BP-2 r1, R-d).

        Appends ONE identity-refinement row (``from_status == to_status``, source
        ``IDENTITY``, carrying the permId and the client id the callback reported)
        through the same CAS write as every other event; it never edits an earlier
        row and moves no existing event. A callback whose permId is absent, or
        equal to what is already persisted, records nothing. A contradicting permId
        IS recorded — it is evidence — and the accessor then fails closed
        (:class:`OrderIdentityConflict`) instead of the contradiction being dropped.
        """

        # The identity that matters is the permId — the value reconciliation matches
        # on when the venue drops the orderRef. A callback that only repeats or only
        # adds a client id is not new identity: the expected client id is
        # configuration (settings.ib_client_id), never persisted state, so such an
        # observation stays the benign no-op it always was (proven, not applied).
        events = self._tracker.events(update.intent_id, current_account_id=current_account_id)
        persisted_permanent_id = _latest(events, "permanent_id")
        if update.permanent_id is None or update.permanent_id == persisted_permanent_id:
            return False
        return self._tracker.record_transition(
            intent_id=update.intent_id,
            event_key=(
                f"{update.intent_id}:{update.broker_order_id}:identity:"
                f"{update.permanent_id}:{update.client_id}"
            ),
            source="IDENTITY",
            from_status=status,
            to_status=status,
            current_account_id=current_account_id,
            broker_order_id=update.broker_order_id,
            permanent_id=update.permanent_id,
            client_id=update.client_id,
            filled_quantity=update.filled_quantity,
            remaining_quantity=update.remaining_quantity,
            evidence={
                "permanent_id": update.permanent_id,
                "client_id": update.client_id,
                "refinement": "identity",
                "observed_via": update.source,
                "occurred_at": update.occurred_at.isoformat(),
            },
            occurred_at=update.occurred_at,
            enforce_from_status=True,
        )

    def record_operator_rejection(
        self,
        *,
        intent_id: str,
        current_account_id: str,
        note: str,
        snapshot_open_orders: int,
        snapshot_executions: int,
        now: datetime,
    ) -> bool:
        """Audited SUBMISSION_UNKNOWN -> REJECTED by explicit operator action.

        True-CAS guarded (``enforce_from_status``): if the intent left
        SUBMISSION_UNKNOWN between the caller's snapshot and this write —
        e.g. a late broker callback finally resolved it — nothing is written
        and ``False`` is returned (ADR-0009 §6).
        """

        return self._tracker.record_transition(
            intent_id=intent_id,
            event_key=f"{intent_id}:operator_resolution:REJECTED",
            source="OPERATOR",
            from_status=OrderLifecycle.SUBMISSION_UNKNOWN,
            to_status=OrderLifecycle.REJECTED,
            current_account_id=current_account_id,
            evidence={
                "operator_note": note,
                "fresh_snapshot_open_orders": snapshot_open_orders,
                "fresh_snapshot_executions": snapshot_executions,
                "resolution": "broker_absent_after_fresh_snapshot",
            },
            occurred_at=now,
            enforce_from_status=True,
        )

    def first_event_time(
        self,
        intent_id: str,
        *,
        to_status: OrderLifecycle,
        current_account_id: str,
    ) -> datetime | None:
        """When the intent FIRST transitioned into ``to_status`` (audit trail)."""

        events = self._tracker.events(intent_id, current_account_id=current_account_id)
        for event in events:
            if event.to_status is to_status:
                return event.occurred_at
        return None

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

    def permanent_id(self, intent_id: str, *, current_account_id: str) -> int | None:
        """The broker permId persisted for this intent, if any (BP-2).

        A later event may carry the permId an earlier one lacked; the rows are
        append-only, so the single non-None value is the intent's identity. Two
        DISTINCT non-None values fail closed with :class:`OrderIdentityConflict`
        (R-e) — never the latest, never the first.
        """
        events = self._tracker.events(intent_id, current_account_id=current_account_id)
        distinct = tuple(
            dict.fromkeys(event.permanent_id for event in events if event.permanent_id is not None)
        )
        if len(distinct) > 1:
            raise OrderIdentityConflict(intent_id, distinct)
        return distinct[0] if distinct else None

    def effective_limit_price(
        self,
        intent_id: str,
        *,
        original_limit_price: Decimal | None,
        current_account_id: str,
    ) -> Decimal | None:
        """Latest persisted working limit, or the immutable intent value.

        Paper modifications keep lifecycle state unchanged and persist their
        replacement price in the MODIFIED event evidence. Invalid modification
        evidence returns ``None`` so restart reconciliation fails closed.
        """

        events = self._tracker.events(intent_id, current_account_id=current_account_id)
        for event in reversed(events):
            if event.source != "MODIFY":
                continue
            raw_price = event.evidence.get("new_limit_price")
            if not isinstance(raw_price, str):
                return None
            try:
                value = Decimal(raw_price)
            except InvalidOperation:
                return None
            return value if value > 0 else None
        return original_limit_price
