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

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import Decimal
from functools import partial
from zoneinfo import ZoneInfo

from chronos.broker.base import CompletedOrderEvidence
from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.enums import ConnectionState, OrderLifecycle
from chronos.domain.models import BrokerExecution, BrokerOrder
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.state_machine import (
    TERMINAL_ORDER_LIFECYCLES,
    OrderLifecycleError,
    OrderLifecycleMachine,
)
from chronos.orders.tracker import OrderIdentityConflict, OrderStatusUpdate, OrderTracker
from chronos.persistence.order_repositories import (
    OrderIntentRecord,
    OrderIntentRepository,
    RecordedFillsAdvanced,
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


# Fill quantity may refine ONLY a working lifecycle, plus a typed FILLED row (already settled; its
# figures refine as before). Any other typed lifecycle that reports fills is not settled by quantity
# and the intent stays locked: REJECTED/CANCELLED with fills is terminal-with-fills (the S-3 policy
# leaves it for the operator rather than guess), and SUBMISSION_UNKNOWN is what
# broker_status_to_lifecycle returns for an UNRECOGNISED status, which must not gain working-order
# authority from quantities alone (D-27).
_QUANTITY_REFINES = frozenset(
    {
        OrderLifecycle.SUBMITTED,
        OrderLifecycle.PARTIALLY_FILLED,
        OrderLifecycle.CANCEL_PENDING,
        OrderLifecycle.FILLED,
    }
)


def _fills_without_a_working_lifecycle(order: BrokerOrder) -> bool:
    return order.filled_quantity > 0 and order.lifecycle not in _QUANTITY_REFINES


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


def _permid_contradicts(reported: int | None, persisted: int | None) -> bool:
    return reported is not None and persisted is not None and reported != persisted


def _is_owned(
    observation: BrokerOrder | BrokerExecution,
    *,
    order_ref: str,
    persisted_permanent_id: int | None,
) -> bool:
    """The identity set (BP-2, narrowed in r1 by R-f).

    A NONBLANK observation reference must equal the owned CHR- reference, and its
    reported permId (when any) must agree with the persisted one. The persisted
    permId is the identity ONLY when the observation carries no reference at all
    (absent/blank) — a venue that reports the order back without its ``orderRef``.
    A nonblank, mismatching reference is never ours, whatever permId it carries.
    """

    if observation.order_ref:
        if observation.order_ref != order_ref:
            return False
        return not _permid_contradicts(observation.permanent_id, persisted_permanent_id)
    return persisted_permanent_id is not None and observation.permanent_id == persisted_permanent_id


def _identity_conflict_reason(
    *,
    order_ref: str,
    persisted_permanent_id: int | None,
    open_orders: tuple[BrokerOrder, ...],
    executions: tuple[BrokerExecution, ...],
) -> str | None:
    """The typed reason for a snapshot that contradicts the persisted identity (R-f)."""

    if persisted_permanent_id is None:
        return None
    for kind, observations in (("order", open_orders), ("execution", executions)):
        for observation in observations:
            if (
                observation.order_ref
                and observation.order_ref != order_ref
                and observation.permanent_id == persisted_permanent_id
            ):
                return (
                    f"identity conflict: broker {kind} {observation.broker_order_id} carrying "
                    f"persisted permId {persisted_permanent_id} reports order reference "
                    f"{observation.order_ref!r}"
                )
            if observation.order_ref == order_ref and _permid_contradicts(
                observation.permanent_id, persisted_permanent_id
            ):
                return (
                    f"identity conflict: owned order reference reports permId "
                    f"{observation.permanent_id} on broker {kind} "
                    f"{observation.broker_order_id}, persisted {persisted_permanent_id}"
                )
    return None


def _terminal_below_recorded_fills(order: BrokerOrder, recorded: Decimal | None) -> bool:
    """LEGACY-5: a terminal listed row that reports FEWER fills than Chronos has recorded.

    The adapter reports filled 0 whenever its order-status cache is empty (every order after a
    reconnect), so such a row cannot lower what was recorded: settling on it would drop the fills
    and release the opening-order lock. ``None`` (the default) judges nothing, and a row AT the
    recorded figure or above it is not below it.
    """

    return (
        recorded is not None
        and order.lifecycle in TERMINAL_ORDER_LIFECYCLES
        and order.filled_quantity < recorded
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
    persisted_permanent_id: int | None = None,
    recorded_filled_quantity: Decimal | None = None,
    now: datetime,
) -> OrderStatusUpdate | None:
    """Derive the true lifecycle for one intent from broker evidence.

    Matching begins with the intent's owned ``order_ref`` (CHR-); the persisted
    ``permanent_id`` from the events is used as the identity only when the
    observation's reference is absent/blank (BP-2 r1, R-f: a venue that reports
    the order back without its ``orderRef``). A nonblank, mismatching reference
    with our permId, or our reference with a contradicting permId, is a typed
    identity conflict and leaves the intent unresolved. Matching then requires
    coherent account and economic identity before Chronos acts. Chronos NEVER
    re-submits. Returns ``None`` (leave unresolved) when the order is absent: a
    timed-out submit very often DID reach the venue, so mere absence is not
    positive evidence of rejection and must not drive a live order to a wrong
    terminal state.

    ``recorded_filled_quantity`` (LEGACY-5) is the cumulative fills Chronos has already
    recorded for the intent. A terminal listed row strictly below it settles nothing and the
    intent stays locked; ``None`` (the default) keeps every existing caller's behaviour.
    """

    order_ref = intent.order_ref
    if not order_ref:
        return None
    working = [
        order
        for order in open_orders
        if _is_owned(order, order_ref=order_ref, persisted_permanent_id=persisted_permanent_id)
    ]
    fills = [
        execution
        for execution in executions
        if _is_owned(execution, order_ref=order_ref, persisted_permanent_id=persisted_permanent_id)
    ]

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
        if _fills_without_a_working_lifecycle(order):
            return None  # not settled by quantity: stay locked, never FILLED/PARTIALLY_FILLED
        if _terminal_below_recorded_fills(order, recorded_filled_quantity):
            return None  # LEGACY-5: a terminal row cannot lower the fills already recorded
        # The adapter already delivers a typed OrderLifecycle; do NOT re-map its
        # value through the raw-IBKR-string mapper. Refine only by fill quantity
        # (the working lifecycles and a typed FILLED row, per the guard above).
        if order.filled_quantity >= order.quantity:
            lifecycle = OrderLifecycle.FILLED
        elif order.filled_quantity > 0:
            lifecycle = OrderLifecycle.PARTIALLY_FILLED
        else:
            lifecycle = order.lifecycle
        if lifecycle in TERMINAL_ORDER_LIFECYCLES and not order.order_ref:
            # owned by the persisted permId alone (BP-2 R-f): progress, never a terminal write
            return None
        return OrderStatusUpdate(
            intent_id=intent.intent_id,
            broker_order_id=order.broker_order_id,
            permanent_id=order.permanent_id,
            client_id=order.client_id,
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
        referenced = sum((e.quantity for e in fills if e.order_ref), Decimal("0"))
        if lifecycle is OrderLifecycle.FILLED and referenced < intent.quantity:
            # the last share may not come from an execution owned by permId alone (S-12)
            return None
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
            client_id=fills[0].client_id,  # _executions_match_intent proved one client id
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
    persisted_permanent_id: int | None = None,
    recorded_filled_quantity: Decimal | None = None,
) -> str:
    order_ref = intent.order_ref
    if not order_ref:
        return "local intent has no owned broker order reference"
    conflict = _identity_conflict_reason(
        order_ref=order_ref,
        persisted_permanent_id=persisted_permanent_id,
        open_orders=open_orders,
        executions=executions,
    )
    if conflict is not None:
        return conflict
    matching_orders = tuple(
        order
        for order in open_orders
        if _is_owned(order, order_ref=order_ref, persisted_permanent_id=persisted_permanent_id)
    )
    if len(matching_orders) > 1:
        return "multiple broker orders share the owned order reference"
    matching_executions = tuple(
        execution
        for execution in executions
        if _is_owned(execution, order_ref=order_ref, persisted_permanent_id=persisted_permanent_id)
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
    unsettled = next(
        (order for order in matching_orders if _fills_without_a_working_lifecycle(order)), None
    )
    if unsettled is not None:
        return (
            f"owned broker order is typed {unsettled.lifecycle.value} (not a working lifecycle) "
            "but reports fills: fill quantity cannot settle it, the intent stays locked"
        )
    below = next(
        (
            order
            for order in matching_orders
            if _terminal_below_recorded_fills(order, recorded_filled_quantity)
        ),
        None,
    )
    if below is not None:
        row_fills = format(below.filled_quantity.normalize(), "f")
        recorded = format(Decimal(str(recorded_filled_quantity)).normalize(), "f")
        return (
            f"owned broker order is typed {below.lifecycle.value} at {row_fills} filled, "
            f"below the {recorded} filled already recorded: a terminal write "
            "would drop recorded fills, the intent stays locked"
        )
    if any(not order.order_ref for order in matching_orders) or any(
        not execution.order_ref for execution in matching_executions
    ):
        return (
            "broker evidence carries no order reference and is owned by the persisted permId "
            "alone: it may record progress but never settles a terminal state, the intent "
            "stays locked"
        )
    if matching_executions:
        return "broker execution evidence was present but could not be resolved"
    return "owned order was absent from the broker order and execution snapshots"


# Statuses the settle planner owns: it alone decides their exit, from one evidence generation.
_SETTLE_STATUSES = frozenset({OrderLifecycle.CANCEL_PENDING, OrderLifecycle.PARTIALLY_FILLED})
_TERMINAL_VENUE = frozenset(
    {OrderLifecycle.CANCELLED, OrderLifecycle.FILLED, OrderLifecycle.REJECTED}
)
_NO_COMPLETED_EVIDENCE = "completed-orders evidence is not available"

#: A once-per-pass read of the venue's completed orders: (records, why-they-are-unavailable).
_CompletedRead = Callable[[], tuple[tuple[BrokerOrder, ...] | None, str]]


def _owned_signature(
    working: list[BrokerOrder], fills: list[BrokerExecution]
) -> tuple[tuple[BrokerOrder, ...], tuple[BrokerExecution, ...]]:
    """The owned evidence a settle write rests on, WHOLE rows, comparable across two snapshots.

    Every field (account, reference, ids, contract, side, quantities, limit, status, execution
    quantity and timestamp) takes part, not a reduced key (Daybreak U-A r1 P1-2).
    """

    return (
        tuple(sorted(working, key=lambda o: (o.broker_order_id, o.lifecycle.value))),
        tuple(sorted(fills, key=lambda e: e.execution_id.strip())),
    )


@dataclass(frozen=True, slots=True)
class _SettlePlan:
    """What the planner decided for one intent: write ONE CAS, hold, refuse, or defer.

    ``kind``: ``write`` (one lifecycle/figure CAS), ``hold`` (proven and nothing to change),
    ``refuse`` (typed reason; nothing is written), ``legacy`` (not the planner's: the existing
    resolver decides, e.g. a partly filled order the venue still holds).
    """

    kind: str
    reason: str = ""
    to_status: OrderLifecycle | None = None
    event_key: str = ""
    filled: Decimal = Decimal("0")
    remaining: Decimal = Decimal("0")
    broker_order_id: int | None = None
    permanent_id: int | None = None
    client_id: int | None = None
    evidence: tuple[tuple[str, str], ...] = ()
    owned_open: tuple[BrokerOrder, ...] = ()
    owned_fills: tuple[BrokerExecution, ...] = ()
    record: BrokerOrder | None = None  # the bound terminal record (completed order or open row)


def _plain(value: Decimal) -> str:
    """A Decimal as plain text (the book stores 8 places; evidence reads ``5``, not ``5E+0``)."""

    return format(value.normalize(), "f")


def _session_start(now: datetime, market_tz: ZoneInfo) -> datetime:
    """S0: the start of the market-timezone date containing ``now``, as an aware UTC instant."""

    local = now.astimezone(market_tz)
    return datetime.combine(local.date(), time.min, tzinfo=market_tz).astimezone(UTC)


def _completed_record_bound(
    intent: OrderIntentRecord,
    record: BrokerOrder,
    *,
    current_account_id: str,
    expected_broker_client_id: int,
    expected_limit_price: Decimal | None,
    persisted_broker_order_id: int | None,
    persisted_permanent_id: int | None,
    persisted_client_id: int | None,
) -> bool:
    """Exact identity (design §2.1): a record binds only when EVERY identifying field agrees.

    Used for a completed-order record AND a terminal open-book row. The reference matched
    already (that is how it became a candidate). The persisted sets are exact (Daybreak r1
    P1-1): the ONE persisted permId (positive) and the ONE persisted client id are required and
    equal to the record's, and a record that names a venue order id (0 = absent on completed
    rows) must name the ONE persisted broker order id -- an intent with none persisted never
    binds it. A persisted conflict never reaches here (the caller refuses it first).
    """

    return (
        persisted_permanent_id is not None
        and persisted_permanent_id > 0
        and record.permanent_id == persisted_permanent_id
        and persisted_client_id is not None
        and record.client_id == persisted_client_id
        and (not record.broker_order_id or record.broker_order_id == persisted_broker_order_id)
        and _working_order_matches_intent(
            intent,
            record,
            current_account_id=current_account_id,
            expected_broker_client_id=expected_broker_client_id,
            expected_limit_price=expected_limit_price,
        )
    )


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
        settle_plans: list[tuple[OrderIntentRecord, _SettlePlan, Callable[..., _SettlePlan]]] = []
        read_completed = self._completed_reader()
        for intent in working:
            persisted_broker_order_id = self._tracker.broker_order_id(
                intent.intent_id,
                current_account_id=current_account_id,
            )
            try:
                persisted_permanent_id = self._tracker.permanent_id(
                    intent.intent_id,
                    current_account_id=current_account_id,
                )
                # a divergent client id is the same contradiction (R-e, r2): fail closed
                persisted_client_id = self._tracker.client_id(
                    intent.intent_id, current_account_id=current_account_id
                )
                if intent.status in _SETTLE_STATUSES:
                    # the settle planner binds the EXACT persisted broker-order-id set, not the
                    # latest (design §2.1; Daybreak U-A r1 P1-1): two distinct ids fail closed
                    self._tracker.single_broker_order_id(
                        intent.intent_id, current_account_id=current_account_id
                    )
            except OrderIdentityConflict as conflict:
                # R-e: contradictory persisted permIds fail closed — never the
                # latest, never the first; the operator sees the typed reason.
                unresolved.append(
                    RestartOrderObservation(
                        intent_id=intent.intent_id,
                        local_status=intent.status,
                        broker_status=None,
                        transition_applied=False,
                        reason=str(conflict),
                    )
                )
                continue
            expected_limit_price = self._tracker.effective_limit_price(
                intent.intent_id,
                original_limit_price=intent.limit_price,
                current_account_id=current_account_id,
            )
            if intent.status in _SETTLE_STATUSES:
                # the same planner, bound to this intent's persisted facts, re-runs over the
                # confirming snapshot before any write (design §3.1; Daybreak U-A r1 P1-2)
                planner = partial(
                    self._plan_settle,
                    intent,
                    current_account_id=current_account_id,
                    now=now,
                    expected_limit_price=expected_limit_price,
                    persisted_broker_order_id=persisted_broker_order_id,
                    persisted_permanent_id=persisted_permanent_id,
                    persisted_client_id=persisted_client_id,
                )
                plan = planner(
                    open_orders=open_orders,
                    executions=executions,
                    read_completed=read_completed,
                )
                if plan.kind != "legacy":
                    settle_plans.append((intent, plan, planner))
                    continue
            recorded_filled_quantity = self._tracker.recorded_filled_quantity(
                intent.intent_id,
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
                persisted_permanent_id=persisted_permanent_id,
                recorded_filled_quantity=recorded_filled_quantity,
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
                            persisted_permanent_id=persisted_permanent_id,
                            recorded_filled_quantity=recorded_filled_quantity,
                        ),
                    )
                )
                continue
            try:
                outcome = self._tracker.ingest(
                    update, current_account_id=current_account_id, fills_floor=True
                )
            except RecordedFillsAdvanced as refused:
                # LEGACY-5 r1: a fill was recorded after this pass read the maximum (another
                # writer); the terminal write was refused atomically and the intent stays locked.
                unresolved.append(
                    RestartOrderObservation(
                        intent_id=intent.intent_id,
                        local_status=intent.status,
                        broker_status=update.lifecycle,
                        transition_applied=False,
                        reason=str(refused),
                    )
                )
                continue
            # R-g: only a LIFECYCLE change is an applied transition (the integer the
            # operator sees as applied_count); an identity refinement is evidence and
            # is named through the existing reason string — the report keeps its shape.
            if outcome.lifecycle_changed:
                applied.append(update)
            proven.append(
                RestartOrderObservation(
                    intent_id=intent.intent_id,
                    local_status=intent.status,
                    broker_status=update.lifecycle,
                    transition_applied=outcome.lifecycle_changed,
                    reason=(
                        "identity evidence refined; lifecycle unchanged"
                        if outcome.identity_refined and not outcome.lifecycle_changed
                        else "matching broker order or execution observed"
                    ),
                )
            )

        self._act_on_settle_plans(
            settle_plans,
            current_account_id=current_account_id,
            now=now,
            proven=proven,
            unresolved=unresolved,
            applied=applied,
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

    # --- the CANCEL_PENDING / PARTIALLY_FILLED settle planner (design r3) --------------------
    def _completed_reader(self) -> _CompletedRead:
        """A once-per-pass lazy read of the venue's completed orders: (records, why-not)."""

        cache: list[tuple[tuple[BrokerOrder, ...] | None, str]] = []

        def read() -> tuple[tuple[BrokerOrder, ...] | None, str]:
            if not cache:
                broker = self._connection.broker
                if not isinstance(broker, CompletedOrderEvidence):
                    cache.append((None, f"{_NO_COMPLETED_EVIDENCE}: the broker has no such read"))
                else:
                    try:
                        cache.append((self._connection.run(broker.completed_orders()), ""))
                    except Exception as error:
                        # ANY failure of this optional read is a typed, fail-closed hold for the
                        # intents that need it -- never an exception out of the whole pass
                        # (Daybreak U-A r1 P3-1: e.g. an earlier unanswered id-less request)
                        cache.append((None, f"{_NO_COMPLETED_EVIDENCE}: {error}"))
            return cache[0]

        return read

    def _witnessed_lower_bound(
        self,
        figures: tuple[tuple[datetime, Decimal], ...],
        fills: list[BrokerExecution],
        now: datetime,
    ) -> Decimal:
        """LB = max(max(X, E_prior) + E_today, Y), partitioned at the session boundary S0.

        X / Y: the largest cumulative figure the book recorded before / since S0. E_prior /
        E_today: owned execution quantity stamped before / since S0. Fills of different
        sessions are disjoint, so they add; figures within one session overlap, so they max.
        """

        s0 = _session_start(now, self._market_tz)
        prior = max((f for t, f in figures if t < s0), default=Decimal("0"))
        today = max((f for t, f in figures if t >= s0), default=Decimal("0"))
        e_prior = sum((e.quantity for e in fills if e.timestamp < s0), Decimal("0"))
        e_today = sum((e.quantity for e in fills if e.timestamp >= s0), Decimal("0"))
        return max(max(prior, e_prior) + e_today, today)

    def _plan_settle(
        self,
        intent: OrderIntentRecord,
        *,
        current_account_id: str,
        now: datetime,
        open_orders: tuple[BrokerOrder, ...],
        executions: tuple[BrokerExecution, ...],
        read_completed: _CompletedRead,
        expected_limit_price: Decimal | None,
        persisted_broker_order_id: int | None,
        persisted_permanent_id: int | None,
        persisted_client_id: int | None,
    ) -> _SettlePlan:
        """One decision per intent over ONE evidence generation; never writes."""

        order_ref = intent.order_ref
        if not order_ref:
            return _SettlePlan("legacy")
        is_pending = intent.status is OrderLifecycle.CANCEL_PENDING
        conflict = _identity_conflict_reason(
            order_ref=order_ref,
            persisted_permanent_id=persisted_permanent_id,
            open_orders=open_orders,
            executions=executions,
        )
        if conflict is not None:
            return _SettlePlan("refuse", conflict)
        working = [
            o
            for o in open_orders
            if _is_owned(o, order_ref=order_ref, persisted_permanent_id=persisted_permanent_id)
        ]
        fills = [
            e
            for e in executions
            if _is_owned(e, order_ref=order_ref, persisted_permanent_id=persisted_permanent_id)
        ]
        if len(working) > 1:
            return _SettlePlan("refuse", "multiple broker orders share the owned order reference")
        if not is_pending and working:
            return _SettlePlan("legacy")  # a partly filled order the venue still holds
        signature = _owned_signature(working, fills)
        identity_ok = (
            all(
                o.broker_order_id == persisted_broker_order_id
                for o in working
                if persisted_broker_order_id is not None
            )
            and (
                not working
                or _working_order_matches_intent(
                    intent,
                    working[0],
                    current_account_id=current_account_id,
                    expected_broker_client_id=self._expected_broker_client_id,
                    expected_limit_price=expected_limit_price,
                )
            )
            and all(
                persisted_broker_order_id is None or e.broker_order_id == persisted_broker_order_id
                for e in fills
            )
            and _executions_match_intent(
                intent,
                tuple(fills),
                current_account_id=current_account_id,
                expected_broker_client_id=self._expected_broker_client_id,
                working_order=working[0] if working else None,
            )
        )
        if not identity_ok:
            return _SettlePlan(
                "refuse",
                _unresolved_evidence_reason(
                    intent,
                    open_orders=open_orders,
                    executions=executions,
                    current_account_id=current_account_id,
                    expected_limit_price=expected_limit_price,
                    expected_broker_client_id=self._expected_broker_client_id,
                    persisted_broker_order_id=persisted_broker_order_id,
                    persisted_permanent_id=persisted_permanent_id,
                ),
            )
        figures = self._tracker.fill_figures(
            intent.intent_id, current_account_id=current_account_id
        )
        lower = self._witnessed_lower_bound(figures, fills, now)
        quantity = intent.quantity

        def settle(
            record: BrokerOrder | None,
            figure: Decimal,
            target: OrderLifecycle,
            source: str,
            witnessed: Decimal = lower,
        ) -> _SettlePlan:
            if figure < witnessed:
                return _SettlePlan(
                    "refuse",
                    f"venue fill figure {figure} is below the witnessed lower bound {witnessed}",
                )
            try:
                OrderLifecycleMachine(intent.status).apply(target)
            except OrderLifecycleError as error:
                return _SettlePlan("refuse", str(error))
            return _SettlePlan(
                "write",
                f"settled {target.value} from {source}",
                to_status=target,
                event_key=f"{intent.intent_id}:settle:{target.value}",
                filled=figure,
                remaining=max(quantity - figure, Decimal("0")),
                broker_order_id=record.broker_order_id
                if record and record.broker_order_id
                else persisted_broker_order_id,
                permanent_id=persisted_permanent_id,
                client_id=self._expected_broker_client_id,
                evidence=(
                    ("settled_from", source),
                    ("venue_fill_figure", _plain(figure)),
                    ("witnessed_lower_bound", _plain(witnessed)),
                    ("unwitnessed_upper_bound", _plain(figure - witnessed)),
                ),
                owned_open=signature[0],
                owned_fills=signature[1],
                record=record,
            )

        def bound_exactly(record: BrokerOrder) -> bool:
            return _completed_record_bound(
                intent,
                record,
                current_account_id=current_account_id,
                expected_broker_client_id=self._expected_broker_client_id,
                expected_limit_price=expected_limit_price,
                persisted_broker_order_id=persisted_broker_order_id,
                persisted_permanent_id=persisted_permanent_id,
                persisted_client_id=persisted_client_id,
            )

        def from_terminal(record: BrokerOrder, source: str) -> _SettlePlan:
            figure = record.filled_quantity
            if record.lifecycle is OrderLifecycle.CANCELLED:
                return settle(record, figure, OrderLifecycle.CANCELLED, source)
            if record.lifecycle is OrderLifecycle.FILLED or figure >= quantity:
                if figure < quantity:
                    return _SettlePlan(
                        "refuse",
                        f"terminal record does not fill the order ({figure} of {quantity})",
                    )
                return settle(record, figure, OrderLifecycle.FILLED, source)
            return _SettlePlan(
                "refuse",
                f"unexpected terminal status for a cancel: {record.lifecycle.value}",
            )

        if working:
            order = working[0]
            terminal = order.lifecycle in _TERMINAL_VENUE or order.filled_quantity >= quantity
            if not terminal and order.lifecycle is not OrderLifecycle.CANCEL_PENDING:
                records, _ = read_completed()
                bound = [
                    r
                    for r in records or ()
                    if r.order_ref == order_ref and r.lifecycle in _TERMINAL_VENUE
                ]
                if bound:
                    return _SettlePlan("refuse", "the working order and a terminal record disagree")
            if terminal:
                # a terminal open-book row is a bound T only under the same exact identity
                if not bound_exactly(order):
                    return _SettlePlan(
                        "refuse", "terminal open-book row is not bound to this intent"
                    )
                return from_terminal(order, "open_order")
            figure = order.filled_quantity
            if figure < lower:
                return _SettlePlan(
                    "refuse",
                    f"venue fill figure {figure} is below the witnessed lower bound {lower}",
                )
            recorded = max((f for _, f in figures), default=Decimal("0"))
            if figure > recorded:
                return _SettlePlan(
                    "write",
                    "fill progress recorded in place",
                    to_status=intent.status,
                    event_key=f"{intent.intent_id}:fill_progress:{figure}",
                    filled=figure,
                    remaining=max(quantity - figure, Decimal("0")),
                    broker_order_id=order.broker_order_id,
                    permanent_id=persisted_permanent_id,
                    client_id=self._expected_broker_client_id,
                    evidence=(
                        ("settled_from", "open_order"),
                        ("venue_fill_figure", _plain(figure)),
                        ("witnessed_lower_bound", _plain(lower)),
                    ),
                    owned_open=signature[0],
                    owned_fills=signature[1],
                )
            return _SettlePlan("hold", "cancel pending; order still working at the venue")

        records, why = read_completed()
        if records is None:
            return _SettlePlan("refuse", why) if is_pending else _SettlePlan("legacy")
        candidates = [r for r in records if r.order_ref == order_ref]
        if not candidates:
            if not is_pending:
                return _SettlePlan("legacy")
            # C10a: no bound record and no working order, so fills are NEVER combined across
            # sessions (design §3.2 "No-record case"; Daybreak/fable U-A r1 P2-1): only THIS
            # session's owned executions can prove the order filled. (Their total cannot exceed
            # the order size here: _executions_match_intent refused that above.)
            s0 = _session_start(now, self._market_tz)
            e_today = sum((e.quantity for e in fills if e.timestamp >= s0), Decimal("0"))
            if e_today >= quantity:
                return settle(None, quantity, OrderLifecycle.FILLED, "executions", e_today)
            if fills:
                return _SettlePlan(
                    "refuse", "cancel pending; order absent; fills present; no terminal record"
                )
            return _SettlePlan("refuse", "cancel pending; order absent; no terminal record")
        if len(candidates) > 1:
            return _SettlePlan(
                "refuse",
                f"completed orders are ambiguous: {len(candidates)} records share the reference",
            )
        record = candidates[0]
        if not bound_exactly(record):
            return _SettlePlan("refuse", "completed order record is not bound to this intent")
        return from_terminal(record, "completed_order")

    def _act_on_settle_plans(
        self,
        plans: list[tuple[OrderIntentRecord, _SettlePlan, Callable[..., _SettlePlan]]],
        *,
        current_account_id: str,
        now: datetime,
        proven: list[RestartOrderObservation],
        unresolved: list[RestartOrderObservation],
        applied: list[OrderStatusUpdate],
    ) -> None:
        """Hold/refuse report as-is; every write is CONFIRMED, then one CAS (design §3.1).

        Confirming = a SECOND complete snapshot -- open orders, executions AND a fresh
        completed-orders read -- and a full re-plan of the intent over it; the write happens only
        if the two plans are equal (decision, figure, ids, evidence, and the whole owned rows and
        bound record they rest on). Anything else -- a record that turned Filled, vanished, or
        changed a figure or an identity field; a failing second read; a new or changed
        execution -- writes nothing (Daybreak U-A r1 P1-2): a fill is never discarded.
        """

        fresh: tuple[tuple[BrokerOrder, ...], tuple[BrokerExecution, ...], _CompletedRead] | None
        fresh = None
        if any(plan.kind == "write" for _, plan, _ in plans):
            fresh = (
                self._connection.run(self._connection.broker.open_orders()),
                self._connection.run(self._connection.broker.executions()),
                self._completed_reader(),
            )
        for intent, plan, planner in plans:
            observed = RestartOrderObservation(
                intent_id=intent.intent_id,
                local_status=intent.status,
                broker_status=None,
                transition_applied=False,
                reason=plan.reason,
            )
            if plan.kind == "refuse":
                unresolved.append(observed)
                continue
            if plan.kind == "hold":
                proven.append(observed)
                continue
            assert fresh is not None and plan.to_status is not None
            again = planner(open_orders=fresh[0], executions=fresh[1], read_completed=fresh[2])
            if again != plan:
                unresolved.append(
                    RestartOrderObservation(
                        intent_id=intent.intent_id,
                        local_status=intent.status,
                        broker_status=None,
                        transition_applied=False,
                        reason="evidence changed between snapshots; nothing was written",
                    )
                )
                continue
            won = self._tracker.record_planned_transition(
                intent_id=intent.intent_id,
                current_account_id=current_account_id,
                from_status=intent.status,
                to_status=plan.to_status,
                event_key=plan.event_key,
                source="FILL_PROGRESS" if plan.to_status is intent.status else "RECONCILE",
                broker_order_id=plan.broker_order_id,
                permanent_id=plan.permanent_id,
                client_id=plan.client_id,
                filled_quantity=plan.filled,
                remaining_quantity=plan.remaining,
                evidence=dict(plan.evidence),
                now=now,
            )
            changed = won and plan.to_status is not intent.status
            if changed:
                applied.append(
                    OrderStatusUpdate(
                        intent_id=intent.intent_id,
                        broker_order_id=plan.broker_order_id,
                        permanent_id=plan.permanent_id,
                        client_id=plan.client_id,
                        lifecycle=plan.to_status,
                        filled_quantity=plan.filled,
                        remaining_quantity=plan.remaining,
                        source="RECONCILE",
                        occurred_at=now,
                    )
                )
            proven.append(
                RestartOrderObservation(
                    intent_id=intent.intent_id,
                    local_status=intent.status,
                    broker_status=plan.to_status,
                    transition_applied=changed,
                    reason=plan.reason
                    if won
                    else "lifecycle moved before the settle write; left to the writer that won",
                )
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
        # Contradictory persisted identity fails closed with THIS method's own refusal type
        # (BP-2-F1, the restart loop's catch at reconcile_report made typed): both accessors
        # are read here, nothing is written, the intent stays SUBMISSION_UNKNOWN, and the
        # operator sees the reason through the same ValueError → 409 path as every other refusal.
        try:
            persisted_permanent_id = self._tracker.permanent_id(
                intent.intent_id,
                current_account_id=current_account_id,
            )
            self._tracker.client_id(intent.intent_id, current_account_id=current_account_id)
        except OrderIdentityConflict as conflict:
            raise ValueError(f"persisted order identity is contradictory: {conflict}") from conflict
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
            persisted_permanent_id=persisted_permanent_id,
            recorded_filled_quantity=self._tracker.recorded_filled_quantity(
                intent.intent_id,
                current_account_id=current_account_id,
            ),
            now=now,
        )
        if update is not None:
            # The broker DOES know this order — apply truth, refuse the manual
            # rejection (the operator was about to resolve against evidence).
            # LEGACY-5 r1: the maximum read above may be stale by now; the terminal write re-checks
            # it atomically and a RecordedFillsAdvanced (a ValueError) refuses, writing nothing.
            self._tracker.ingest(update, current_account_id=current_account_id, fills_floor=True)
            return False
        matching_executions = tuple(
            execution for execution in executions if execution.order_ref == intent.order_ref
        )
        if matching_orders or matching_executions:
            raise ValueError(
                "broker evidence does not settle this intent (the order's identity, lifecycle "
                "or fills conflict with the local record); it stays SUBMISSION_UNKNOWN until "
                "the order is verified manually in TWS"
            )
        raise ValueError(
            "broker absence cannot safely prove rejection; no terminal transition was written. "
            "Keep the intent SUBMISSION_UNKNOWN until positive broker evidence is available."
        )
