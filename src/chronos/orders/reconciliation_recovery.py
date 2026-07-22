"""Restart reconciliation and SUBMISSION_UNKNOWN recovery.

On startup, and after any submit whose broker id is unknown, the working
intents are matched to broker truth by their Chronos-owned ``order_ref``
(CHR-) and by ``permId``/executions. A SUBMISSION_UNKNOWN intent is resolved to
its true state when the broker snapshot carries positive evidence, and a submit
is NEVER retried automatically. Mere ABSENCE from a snapshot is NOT resolved
automatically — a timed-out submit very often did reach the venue, so absence
leaves the intent unresolved for the operator (M5 review remediation).
:meth:`OrderRestartReconciler.operator_resolve` performs a bounded refresh
that applies only positive broker evidence. Absence remains non-terminal
because a broker snapshot cannot be atomic with the local lifecycle write
(ADR-0009 §6).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.enums import ConnectionState, OrderLifecycle
from chronos.domain.models import BrokerExecution, BrokerOrder
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
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


@dataclass(frozen=True, slots=True)
class RestartOrderObservation:
    """Broker proof (or lack of it) for one sent-active local intent."""

    intent_id: str
    local_status: OrderLifecycle
    broker_status: OrderLifecycle | None
    transition_applied: bool
    reason: str


@dataclass(frozen=True, slots=True)
class RemainingActiveIntent:
    """One sent-active intent that still blocks another opening action."""

    intent_id: str
    status: OrderLifecycle


@dataclass(frozen=True, slots=True)
class OrderRestartReconciliationReport:
    """Typed restart result that never conflates no-op with broker absence."""

    proven: tuple[RestartOrderObservation, ...]
    unresolved: tuple[RestartOrderObservation, ...]
    remaining_active: tuple[RemainingActiveIntent, ...]
    applied_updates: tuple[OrderStatusUpdate, ...]

    @property
    def broker_evidence_complete(self) -> bool:
        return not self.unresolved

    @property
    def permits_new_opening_order(self) -> bool:
        # The full portfolio reconciler reads a separate legacy ownership
        # store. Until order_intents are integrated there, any remaining sent-
        # active intent conservatively blocks another opening action.
        return self.broker_evidence_complete and not self.remaining_active


def _contract_matches_intent(intent: OrderIntentRecord, contract: object) -> bool:
    return (
        intent.con_id is not None
        and getattr(contract, "con_id", None) == intent.con_id
        and getattr(contract, "symbol", None) == intent.symbol
    )


def _working_order_matches_intent(
    intent: OrderIntentRecord,
    order: BrokerOrder,
    *,
    current_account_id: str,
    expected_broker_client_id: int,
    expected_limit_price: Decimal | None,
) -> bool:
    return (
        order.account_id == current_account_id
        and order.client_id == expected_broker_client_id
        and _contract_matches_intent(intent, order.contract)
        and order.side is intent.action
        and order.quantity == intent.quantity
        and expected_limit_price is not None
        and order.limit_price == expected_limit_price
    )


def _executions_match_intent(
    intent: OrderIntentRecord,
    executions: tuple[BrokerExecution, ...],
    *,
    current_account_id: str,
    expected_broker_client_id: int,
    working_order: BrokerOrder | None = None,
) -> bool:
    if not executions:
        return True
    broker_order_ids = {execution.broker_order_id for execution in executions}
    execution_ids = {execution.execution_id.strip() for execution in executions}
    client_ids = {execution.client_id for execution in executions}
    permanent_ids = {
        execution.permanent_id for execution in executions if execution.permanent_id is not None
    }
    return (
        len(broker_order_ids) == 1
        and len(execution_ids) == len(executions)
        and "" not in execution_ids
        and len(client_ids) == 1
        and client_ids == {expected_broker_client_id}
        and len(permanent_ids) <= 1
        and all(
            execution.account_id == current_account_id
            and _contract_matches_intent(intent, execution.contract)
            and execution.side is intent.action
            for execution in executions
        )
        and (
            working_order is None
            or all(
                execution.broker_order_id == working_order.broker_order_id
                and execution.client_id == working_order.client_id
                and (
                    execution.permanent_id is None
                    or working_order.permanent_id is None
                    or execution.permanent_id == working_order.permanent_id
                )
                for execution in executions
            )
        )
        and sum((execution.quantity for execution in executions), Decimal("0")) <= intent.quantity
    )


def resolve_from_broker_evidence(
    intent: OrderIntentRecord,
    *,
    open_orders: tuple[BrokerOrder, ...],
    executions: tuple[BrokerExecution, ...],
    current_account_id: str,
    expected_broker_client_id: int,
    expected_limit_price: Decimal | None,
    persisted_broker_order_id: int | None,
    now: datetime,
) -> OrderStatusUpdate | None:
    """Derive the true lifecycle for one intent from broker evidence.

    Matching begins with the intent's owned ``order_ref`` (CHR-) and requires
    coherent account and economic identity before Chronos acts. Chronos NEVER
    re-submits. Returns ``None`` (leave unresolved) when the order is absent:
    a timed-out submit very often DID reach the venue, so mere absence is not
    positive evidence of rejection and must not drive a live order to a wrong
    terminal state.
    """

    order_ref = intent.order_ref
    if not order_ref:
        return None
    working = [order for order in open_orders if order.order_ref == order_ref]
    fills = [execution for execution in executions if execution.order_ref == order_ref]

    if len(working) > 1:
        return None
    evidence_order_ids = {order.broker_order_id for order in working} | {
        execution.broker_order_id for execution in fills
    }
    # A broker id persisted at submit/ack time is authoritative. Reuse of the
    # same CHR- ref by a different broker order is conflicting evidence, not a
    # lifecycle update for this local intent.
    if persisted_broker_order_id is not None and any(
        broker_order_id != persisted_broker_order_id for broker_order_id in evidence_order_ids
    ):
        return None
    if working and not _working_order_matches_intent(
        intent,
        working[0],
        current_account_id=current_account_id,
        expected_broker_client_id=expected_broker_client_id,
        expected_limit_price=expected_limit_price,
    ):
        return None
    if not _executions_match_intent(
        intent,
        tuple(fills),
        current_account_id=current_account_id,
        expected_broker_client_id=expected_broker_client_id,
        working_order=working[0] if working else None,
    ):
        return None
    if len(working) == 1:
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
            permanent_id=next(
                (
                    execution.permanent_id
                    for execution in fills
                    if execution.permanent_id is not None
                ),
                None,
            ),
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


def _unresolved_evidence_reason(
    intent: OrderIntentRecord,
    *,
    open_orders: tuple[BrokerOrder, ...],
    executions: tuple[BrokerExecution, ...],
    current_account_id: str,
    expected_limit_price: Decimal | None,
    expected_broker_client_id: int,
    persisted_broker_order_id: int | None,
) -> str:
    order_ref = intent.order_ref
    if not order_ref:
        return "local intent has no owned broker order reference"
    matching_orders = tuple(order for order in open_orders if order.order_ref == order_ref)
    if len(matching_orders) > 1:
        return "multiple broker orders share the owned order reference"
    matching_executions = tuple(
        execution for execution in executions if execution.order_ref == order_ref
    )
    evidence_order_ids = {order.broker_order_id for order in matching_orders} | {
        execution.broker_order_id for execution in matching_executions
    }
    if persisted_broker_order_id is not None and any(
        broker_order_id != persisted_broker_order_id for broker_order_id in evidence_order_ids
    ):
        return "owned order reference matched a different persisted broker order id"
    if (
        matching_orders
        and not _working_order_matches_intent(
            intent,
            matching_orders[0],
            current_account_id=current_account_id,
            expected_broker_client_id=expected_broker_client_id,
            expected_limit_price=expected_limit_price,
        )
    ) or not _executions_match_intent(
        intent,
        matching_executions,
        current_account_id=current_account_id,
        expected_broker_client_id=expected_broker_client_id,
        working_order=matching_orders[0] if matching_orders else None,
    ):
        return "owned order reference matched conflicting account or economic identity"
    if matching_executions:
        return "broker execution evidence was present but could not be resolved"
    return "owned order was absent from the broker order and execution snapshots"


class OrderRestartReconciler:
    """Re-derive and persist the true state of every working intent."""

    def __init__(
        self,
        *,
        connection: BrokerConnectionManager,
        intents: OrderIntentRepository,
        tracker: OrderTracker,
        reconciliation_readiness: ReconciliationReadiness,
        expected_broker_client_id: int,
        market_timezone: str = "America/New_York",
    ) -> None:
        self._connection = connection
        self._intents = intents
        self._tracker = tracker
        self._reconciliation_readiness = reconciliation_readiness
        if expected_broker_client_id < 0:
            raise ValueError("expected broker client id must be non-negative")
        self._expected_broker_client_id = expected_broker_client_id
        self._market_tz = ZoneInfo(market_timezone)

    def reconcile(self, *, current_account_id: str, now: datetime) -> tuple[OrderStatusUpdate, ...]:
        return self.reconcile_report(current_account_id=current_account_id, now=now).applied_updates

    def reconcile_report(
        self, *, current_account_id: str, now: datetime
    ) -> OrderRestartReconciliationReport:
        working = tuple(
            intent
            for intent in self._intents.active(current_account_id=current_account_id)
            if intent.status in _UNRESOLVED_STATUSES
        )
        if not working:
            return OrderRestartReconciliationReport(
                proven=(),
                unresolved=(),
                remaining_active=(),
                applied_updates=(),
            )

        open_orders = self._connection.run(self._connection.broker.open_orders())
        executions = self._connection.run(self._connection.broker.executions())
        proven: list[RestartOrderObservation] = []
        unresolved: list[RestartOrderObservation] = []
        applied: list[OrderStatusUpdate] = []
        for intent in working:
            persisted_broker_order_id = self._tracker.broker_order_id(
                intent.intent_id,
                current_account_id=current_account_id,
            )
            expected_limit_price = self._tracker.effective_limit_price(
                intent.intent_id,
                original_limit_price=intent.limit_price,
                current_account_id=current_account_id,
            )
            update = resolve_from_broker_evidence(
                intent,
                open_orders=open_orders,
                executions=executions,
                current_account_id=current_account_id,
                expected_broker_client_id=self._expected_broker_client_id,
                expected_limit_price=expected_limit_price,
                persisted_broker_order_id=persisted_broker_order_id,
                now=now,
            )
            if update is None:
                unresolved.append(
                    RestartOrderObservation(
                        intent_id=intent.intent_id,
                        local_status=intent.status,
                        broker_status=None,
                        transition_applied=False,
                        reason=_unresolved_evidence_reason(
                            intent,
                            open_orders=open_orders,
                            executions=executions,
                            current_account_id=current_account_id,
                            expected_broker_client_id=self._expected_broker_client_id,
                            expected_limit_price=expected_limit_price,
                            persisted_broker_order_id=persisted_broker_order_id,
                        ),
                    )
                )
                continue
            transition_applied = self._tracker.ingest(update, current_account_id=current_account_id)
            if transition_applied:
                applied.append(update)
            proven.append(
                RestartOrderObservation(
                    intent_id=intent.intent_id,
                    local_status=intent.status,
                    broker_status=update.lifecycle,
                    transition_applied=transition_applied,
                    reason="matching broker order or execution observed",
                )
            )

        remaining_active = tuple(
            RemainingActiveIntent(intent_id=intent.intent_id, status=intent.status)
            for intent in self._intents.active(current_account_id=current_account_id)
            if intent.status in _UNRESOLVED_STATUSES
        )
        return OrderRestartReconciliationReport(
            proven=tuple(proven),
            unresolved=tuple(unresolved),
            remaining_active=remaining_active,
            applied_updates=tuple(applied),
        )

    def operator_resolve(
        self,
        intent_id: str,
        *,
        operator_note: str,
        current_account_id: str,
        now: datetime,
    ) -> bool:
        """Operator-triggered bounded refresh of a SUBMISSION_UNKNOWN intent.

        Positive broker evidence is applied through the normal reconciliation
        path. Absence never becomes terminal: a broker snapshot and local write
        cannot be atomic, so absence cannot prove an order never reached the
        venue.
        No terminal lifecycle is written from absence.
        The non-empty note keeps this privileged recovery attempt explicit.
        SUBMISSION_UNKNOWN remains locked until later positive reconciliation.
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
        provenance_before = self._reconciliation_readiness.snapshot()
        status_before = self._connection.run(self._connection.broker.connection_status())
        first_open_orders = self._connection.run(self._connection.broker.open_orders())
        first_executions = self._connection.run(self._connection.broker.executions())
        open_orders = self._connection.run(self._connection.broker.open_orders())
        executions = self._connection.run(self._connection.broker.executions())
        status_after = self._connection.run(self._connection.broker.connection_status())
        provenance_after = self._reconciliation_readiness.snapshot()
        scope_is_current = (
            status_before.connected
            and status_before.state is ConnectionState.CONNECTED
            and status_before.account_id == current_account_id
            and status_after.connected
            and status_after.state is ConnectionState.CONNECTED
            and status_after.account_id == current_account_id
        )
        provenance_is_current = (
            provenance_before.session_id == provenance_after.session_id
            and provenance_before.generation == provenance_after.generation
        )
        evidence_is_stable = first_open_orders == open_orders and first_executions == executions
        if not (scope_is_current and provenance_is_current and evidence_is_stable):
            raise ValueError(
                "broker absence is not provable: account, connection, or order evidence "
                "changed across the bounded recovery observation"
            )
        if not intent.order_ref:
            raise ValueError(
                "broker absence is not provable: the local intent has no owned order reference"
            )
        matching_orders = tuple(
            order for order in open_orders if order.order_ref == intent.order_ref
        )
        if len(matching_orders) > 1:
            raise ValueError("broker evidence is ambiguous: duplicate owned order references")
        update = resolve_from_broker_evidence(
            intent,
            open_orders=open_orders,
            executions=executions,
            current_account_id=current_account_id,
            expected_broker_client_id=self._expected_broker_client_id,
            expected_limit_price=self._tracker.effective_limit_price(
                intent.intent_id,
                original_limit_price=intent.limit_price,
                current_account_id=current_account_id,
            ),
            persisted_broker_order_id=self._tracker.broker_order_id(
                intent.intent_id,
                current_account_id=current_account_id,
            ),
            now=now,
        )
        if update is not None:
            # The broker DOES know this order — apply truth, refuse the manual
            # rejection (the operator was about to resolve against evidence).
            self._tracker.ingest(update, current_account_id=current_account_id)
            return False
        matching_executions = tuple(
            execution for execution in executions if execution.order_ref == intent.order_ref
        )
        if matching_orders or matching_executions:
            raise ValueError("broker evidence conflicts with the local order identity")
        raise ValueError(
            "broker absence cannot safely prove rejection; no terminal transition was written. "
            "Keep the intent SUBMISSION_UNKNOWN until positive broker evidence is available."
        )
