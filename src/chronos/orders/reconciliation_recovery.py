"""Restart reconciliation and SUBMISSION_UNKNOWN recovery.

On startup, and after any submit whose broker id is unknown, the working
intents are matched to broker truth by their Chronos-owned ``order_ref``
(CHR-) and by ``permId``/executions. A SUBMISSION_UNKNOWN intent is resolved to
its true state when the broker snapshot carries positive evidence, and a submit
is NEVER retried automatically. Mere ABSENCE from a snapshot is NOT resolved
automatically — a timed-out submit very often did reach the venue, so absence
leaves the intent unresolved for the operator (M5 review remediation). The
designed exit is :meth:`OrderRestartReconciler.operator_resolve`: an audited,
typed-note operator action that drives a broker-absent SUBMISSION_UNKNOWN
intent to REJECTED only against a fresh snapshot taken in the same call
(ADR-0009 §6).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.enums import OrderLifecycle
from chronos.domain.models import BrokerExecution, BrokerOrder
from chronos.orders.tracker import OrderStatusUpdate, OrderTracker
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
) -> OrderStatusUpdate | None:
    """Derive the true lifecycle for one intent from broker evidence.

    Matching is by the intent's ``order_ref`` (CHR-) only — Chronos never acts
    on an order it does not own, and NEVER re-submits. Returns ``None`` (leave
    unresolved for the operator) when the order is absent from this snapshot:
    a timed-out submit very often DID reach the venue, so mere absence is not
    positive evidence of rejection and must not drive a live order to a wrong
    terminal state.
    """

    order_ref = intent.order_ref
    working = [order for order in open_orders if order.order_ref == order_ref]
    fills = [execution for execution in executions if execution.order_ref == order_ref]

    if working:
        order = working[0]
        # The adapter already delivers a typed OrderLifecycle; do NOT re-map its
        # value through the raw-IBKR-string mapper. Refine only by fill quantity.
        if order.filled_quantity >= order.quantity:
            lifecycle = OrderLifecycle.FILLED
        elif order.filled_quantity > 0:
            lifecycle = OrderLifecycle.PARTIALLY_FILLED
        else:
            lifecycle = order.lifecycle
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

    # Absent from this snapshot: leave unresolved (surfaced to the operator via
    # list_orders) rather than concluding a terminal REJECTED without positive
    # evidence the order never reached the venue.
    return None


class OrderRestartReconciler:
    """Re-derive and persist the true state of every working intent."""

    def __init__(
        self,
        *,
        connection: BrokerConnectionManager,
        intents: OrderIntentRepository,
        tracker: OrderTracker,
        market_timezone: str = "America/New_York",
    ) -> None:
        self._connection = connection
        self._intents = intents
        self._tracker = tracker
        self._market_tz = ZoneInfo(market_timezone)

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
            if update is None:
                # Absent from the snapshot: left unresolved for operator review.
                continue
            if self._tracker.ingest(update, current_account_id=current_account_id):
                applied.append(update)
        return tuple(applied)

    def operator_resolve(
        self,
        intent_id: str,
        *,
        operator_note: str,
        current_account_id: str,
        now: datetime,
    ) -> bool:
        """Audited operator resolution of a broker-absent SUBMISSION_UNKNOWN intent.

        Drives the intent to REJECTED **only** when a FRESH broker snapshot
        taken inside this call shows no matching working order and no
        executions for its ``order_ref`` — otherwise the normal reconciliation
        evidence applies instead and this refuses. Requires a non-empty typed
        note; the transition is recorded with ``source="OPERATOR"`` and the
        snapshot evidence, so recovery is a designed action, never DB surgery
        (ADR-0009 §6).
        """

        note = operator_note.strip()
        if not note:
            raise ValueError("operator resolution requires a non-empty typed note")
        intent = self._intents.get(intent_id, current_account_id=current_account_id)
        if intent is None:
            raise ValueError(f"unknown order intent {intent_id!r}")
        if intent.status is not OrderLifecycle.SUBMISSION_UNKNOWN:
            raise ValueError(
                "operator resolution applies only to SUBMISSION_UNKNOWN intents; "
                f"this intent is {intent.status.value}"
            )
        # Evidence-window guard (M7 review finding F2, the dangerous direction):
        # IBKR's executions snapshot covers the CURRENT trading session only, so
        # "no executions" is NOT provable absence for a submit from a prior
        # session — a Friday fill is invisible on Monday, and resolving it to
        # REJECTED would hide a real position. Refuse unless the intent entered
        # SUBMISSION_UNKNOWN in the same market-timezone session the snapshot
        # covers.
        unknown_since = self._tracker.first_event_time(
            intent_id,
            to_status=OrderLifecycle.SUBMISSION_UNKNOWN,
            current_account_id=current_account_id,
        )
        if unknown_since is None or (
            unknown_since.astimezone(self._market_tz).date()
            != now.astimezone(self._market_tz).date()
        ):
            raise ValueError(
                "broker absence is not provable: the executions window covers only "
                "the current trading session, and this intent entered "
                "SUBMISSION_UNKNOWN outside it. Verify the order manually in TWS "
                "(orders AND trade log AND positions) before any resolution."
            )
        open_orders = self._connection.run(self._connection.broker.open_orders())
        executions = self._connection.run(self._connection.broker.executions())
        update = resolve_from_broker_evidence(
            intent, open_orders=open_orders, executions=executions, now=now
        )
        if update is not None:
            # The broker DOES know this order — apply truth, refuse the manual
            # rejection (the operator was about to resolve against evidence).
            self._tracker.ingest(update, current_account_id=current_account_id)
            return False
        return self._tracker.record_operator_rejection(
            intent_id=intent_id,
            current_account_id=current_account_id,
            note=note,
            snapshot_open_orders=len(open_orders),
            snapshot_executions=len(executions),
            now=now,
        )
