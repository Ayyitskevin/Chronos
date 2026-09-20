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

from dataclasses import dataclass
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
    """Two distinct non-null values of one broker identity field were persisted for one
    intent (BP-2 r1, R-e; r2 extends it to the client id).

    Reconciliation treats the intent as UNRESOLVED with this reason — never the
    latest value, never the first; the contradiction is evidence, not a choice.
    """

    def __init__(
        self, intent_id: str, values: tuple[int, ...], *, field: str = "permanent_id"
    ) -> None:
        self.intent_id = intent_id
        self.field = field
        self.values = values
        self.permanent_ids = values if field == "permanent_id" else ()
        listed = " and ".join(str(value) for value in values)
        noun = "permanent ids" if field == "permanent_id" else "client ids"
        super().__init__(
            f"identity conflict: intent {intent_id!r} persisted {noun} {listed} disagree"
        )


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """What :meth:`OrderTracker.ingest` did with one observation (BP-2 r4, R-g).

    ``lifecycle_changed``: the intent's status advanced (a transition row was
    written). ``identity_refined``: a same-status observation supplied broker
    identity the rows lacked (an IDENTITY row was written) — evidence, never a
    lifecycle transition. Both False: a duplicate / stale / benign no-op. This is
    deliberately NOT a bool: the old single flag conflated the two meanings and
    the restart report published a refinement as an applied transition.
    """

    lifecycle_changed: bool
    identity_refined: bool

    @property
    def recorded(self) -> bool:
        return self.lifecycle_changed or self.identity_refined

    def __bool__(self) -> bool:
        raise TypeError(
            "IngestOutcome is not a bool: read .lifecycle_changed / .identity_refined / .recorded"
        )


_NOTHING_RECORDED = IngestOutcome(lifecycle_changed=False, identity_refined=False)


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

    def ingest(self, update: OrderStatusUpdate, *, current_account_id: str) -> IngestOutcome:
        """Apply one broker observation.

        Returns an :class:`IngestOutcome`: ``lifecycle_changed`` for a genuine
        transition, ``identity_refined`` for a same-status observation that first
        supplied broker identity (R-d), neither for a no-op/duplicate/stale event.
        """

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
                return _NOTHING_RECORDED

        machine = OrderLifecycleMachine(intent.status)
        # apply() raises OrderLifecycleError on a contradiction (surfaced to the
        # caller); returns False for a benign no-op we can skip — unless a
        # same-status callback newly supplies the broker identity (R-d).
        if not machine.apply(update.lifecycle):
            if update.lifecycle is not intent.status:
                return _NOTHING_RECORDED  # an out-of-order message after a terminal state
            refined = self._record_identity_refinement(update, intent.status, current_account_id)
            return IngestOutcome(lifecycle_changed=False, identity_refined=refined)

        event_key = (
            f"{update.intent_id}:{update.broker_order_id}:"
            f"{update.lifecycle.value}:{update.filled_quantity}"
        )
        advanced = self._tracker.record_transition(
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
        return IngestOutcome(lifecycle_changed=advanced, identity_refined=False)

    def _record_identity_refinement(
        self, update: OrderStatusUpdate, status: OrderLifecycle, current_account_id: str
    ) -> bool:
        """A same-status callback that first supplies permId/clientId (BP-2 r1, R-d).

        Appends ONE identity-refinement row (``from_status == to_status``, source
        ``IDENTITY``, carrying the permId and the client id the callback reported)
        through the same CAS write as every other event; it never edits an earlier
        row and moves no existing event. Either field newly arriving (None ->
        non-None) is new identity; a callback repeating the persisted values
        records nothing. A contradicting value IS recorded — it is evidence — and
        the accessor for that field then fails closed (:class:`OrderIdentityConflict`)
        instead of the contradiction being dropped.
        """

        # R-d (clarified in r2): EITHER field going None -> non-None is new identity;
        # a callback repeating the persisted values is idempotent. A non-null value
        # that DIFFERS is recorded too — evidence for the accessors to fail closed on.
        events = self._tracker.events(update.intent_id, current_account_id=current_account_id)
        new_permanent_id = update.permanent_id is not None and update.permanent_id != _latest(
            events, "permanent_id"
        )
        new_client_id = update.client_id is not None and update.client_id != _latest(
            events, "client_id"
        )
        if not (new_permanent_id or new_client_id):
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
        return self._single_identity(intent_id, "permanent_id", current_account_id)

    def client_id(self, intent_id: str, *, current_account_id: str) -> int | None:
        """The broker client id persisted for this intent, if any (BP-2 r2).

        Same policy as :meth:`permanent_id`: one non-None value is the identity;
        two distinct values fail closed with :class:`OrderIdentityConflict`.
        """
        return self._single_identity(intent_id, "client_id", current_account_id)

    def _single_identity(self, intent_id: str, field: str, current_account_id: str) -> int | None:
        events = self._tracker.events(intent_id, current_account_id=current_account_id)
        distinct = tuple(
            dict.fromkeys(
                int(value)
                for value in (getattr(event, field) for event in events)
                if value is not None
            )
        )
        if len(distinct) > 1:
            raise OrderIdentityConflict(intent_id, distinct, field=field)
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
