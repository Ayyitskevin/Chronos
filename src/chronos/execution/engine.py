"""Execution engine: the single gate between approved intents and a broker.

Hard rules enforced here (tested in tests/safety/):

- No submission without a ``RiskApproval`` minted by the wired risk engine for
  exactly this intent id (object identity on the engine token).
- No submission when halted, when the mode lock lacks submission capability,
  when reconciliation has not passed, or when the ledger has already seen the
  intent id (duplicate suppression).
- Broker events are the only source of state transitions; illegal transitions
  raise a halt instead of being papered over.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from chronos.control.halt import HaltReason, HaltStore
from chronos.control.modes import ExecutionCapability, ModeLock
from chronos.execution.brokers.port import BrokerEvent, BrokerEventKind, ExecutionBrokerPort
from chronos.execution.intents import IntentStatus, OrderIntent
from chronos.execution.state_machine import OrderStateMachine, OrderTransitionError
from chronos.risk.engine import RiskApproval, RiskEngine


class SubmissionRefused(StrEnum):
    NOT_REFUSED = "NOT_REFUSED"
    NO_APPROVAL = "NO_APPROVAL"
    FOREIGN_APPROVAL = "FOREIGN_APPROVAL"
    APPROVAL_INTENT_MISMATCH = "APPROVAL_INTENT_MISMATCH"
    HALTED = "HALTED"
    MODE_FORBIDS = "MODE_FORBIDS"
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"
    DUPLICATE_INTENT = "DUPLICATE_INTENT"
    LEDGER_UNAVAILABLE = "LEDGER_UNAVAILABLE"
    BROKER_SUBMIT_FAILED = "BROKER_SUBMIT_FAILED"


class LedgerPort(Protocol):
    """Durable intent record. Implementations must be write-through."""

    def record_intent(self, intent: OrderIntent, status: IntentStatus) -> None: ...

    def has_intent(self, intent_id: str) -> bool: ...

    def record_transition(
        self, intent_id: str, status: IntentStatus, at_utc: datetime, evidence: str
    ) -> None: ...

    def record_fill(
        self,
        intent_id: str,
        cumulative_quantity: int,
        average_price: Decimal | None,
        commission_usd: Decimal | None,
        at_utc: datetime,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    submitted: bool
    refusal: SubmissionRefused
    detail: str


@dataclass
class ExecutionEngine:
    broker: ExecutionBrokerPort
    risk_engine: RiskEngine
    ledger: LedgerPort
    halt_store: HaltStore
    mode_lock: ModeLock
    reconciliation_passed: bool = False
    _orders: dict[str, OrderStateMachine] = field(default_factory=dict)

    def submit_approved(
        self,
        intent: OrderIntent,
        approval: RiskApproval | None,
        *,
        now_utc: datetime,
    ) -> SubmissionResult:
        """Submit one risk-approved intent, or refuse with a machine reason."""

        if approval is None:
            return SubmissionResult(False, SubmissionRefused.NO_APPROVAL, "no risk approval")
        if approval.minted_by is not self.risk_engine.token:
            return SubmissionResult(
                False,
                SubmissionRefused.FOREIGN_APPROVAL,
                "approval was not minted by the wired risk engine",
            )
        if approval.intent_id != intent.intent_id:
            return SubmissionResult(
                False,
                SubmissionRefused.APPROVAL_INTENT_MISMATCH,
                "approval does not match this intent",
            )
        halt = self.halt_store.read()
        if halt.trading_blocked:
            return SubmissionResult(
                False,
                SubmissionRefused.HALTED,
                f"halted: {halt.reason.value if halt.reason else 'unknown'}",
            )
        if self.mode_lock.capability not in (
            ExecutionCapability.SIMULATED_ONLY,
            ExecutionCapability.PAPER_SUBMISSION,
        ):
            return SubmissionResult(
                False,
                SubmissionRefused.MODE_FORBIDS,
                f"mode {self.mode_lock.mode} forbids order submission",
            )
        if not self.reconciliation_passed:
            return SubmissionResult(
                False,
                SubmissionRefused.RECONCILIATION_PENDING,
                "reconciliation has not passed since startup/reconnect",
            )
        try:
            if self.ledger.has_intent(intent.intent_id):
                return SubmissionResult(
                    False,
                    SubmissionRefused.DUPLICATE_INTENT,
                    "intent id already recorded in the ledger",
                )
            self.ledger.record_intent(intent, IntentStatus.PENDING_SUBMISSION)
        except Exception as error:
            self.halt_store.halt(
                HaltReason.STORAGE_FAILURE, f"ledger write failed before submission: {error!r}"
            )
            return SubmissionResult(
                False, SubmissionRefused.LEDGER_UNAVAILABLE, "ledger unavailable; halted"
            )

        # Re-read the halt immediately before handing the order to the broker.
        # The earlier read happened before ledger I/O (a real fsync on the
        # SQLite ledger); an operator or automatic halt can land inside that
        # window, and "halts outrank everything" requires catching it here
        # rather than submitting an already-halted order.
        halt = self.halt_store.read()
        if halt.trading_blocked:
            return SubmissionResult(
                False,
                SubmissionRefused.HALTED,
                f"halted before submission: {halt.reason.value if halt.reason else 'unknown'}",
            )

        machine = OrderStateMachine(intent_id=intent.intent_id)
        machine.apply(IntentStatus.RISK_APPROVED, at_utc=now_utc, evidence="risk approval")
        machine.apply(IntentStatus.PENDING_SUBMISSION, at_utc=now_utc, evidence="handed to adapter")
        self._orders[intent.intent_id] = machine
        try:
            self.broker.submit(intent)
        except Exception as error:
            # The call may or may not have reached the broker before raising
            # (e.g. a socket drop mid-request); the true state is UNKNOWN and
            # requires reconciliation, never assumed either submitted or not.
            evidence = f"broker.submit raised: {error!r}"
            machine.apply(IntentStatus.UNKNOWN, at_utc=now_utc, evidence=evidence)
            machine.apply(IntentStatus.RECONCILIATION_REQUIRED, at_utc=now_utc, evidence=evidence)
            self.reconciliation_passed = False
            self.ledger.record_transition(intent.intent_id, machine.status, now_utc, evidence)
            self.halt_store.halt(HaltReason.CONNECTION_UNSTABLE, evidence)
            return SubmissionResult(
                False,
                SubmissionRefused.BROKER_SUBMIT_FAILED,
                "broker submission raised an exception; halted pending reconciliation",
            )
        self._transition(
            machine, IntentStatus.SUBMITTED, at_utc=now_utc, evidence="submission call returned"
        )
        return SubmissionResult(True, SubmissionRefused.NOT_REFUSED, "submitted")

    def pump_events(self, *, now_utc: datetime) -> tuple[BrokerEvent, ...]:
        """Apply pending broker events to order state; return what was applied."""

        events = self.broker.drain_events()
        for event in events:
            machine = self._orders.get(event.intent_id)
            if machine is None:
                self.halt_store.halt(
                    HaltReason.UNKNOWN_ORDER,
                    f"broker event for unknown intent {event.intent_id}",
                )
                # An event for an order we never saw means our model of the
                # broker is incomplete; block trading until reconciliation
                # re-establishes it (a restart must not resume on stale state).
                self.reconciliation_passed = False
                continue
            try:
                self._apply_event(machine, event)
            except OrderTransitionError as error:
                machine_status_now = machine.status
                self.halt_store.halt(
                    HaltReason.STATE_CORRUPTION,
                    f"illegal order transition ({machine_status_now}): {error}",
                )
                # Clear the reconciliation flag unconditionally: the RECONCILE
                # recovery below can itself be an illegal transition (e.g. from a
                # terminal state), and that must not leave trading re-armable.
                self.reconciliation_passed = False
                try:
                    machine.apply(
                        IntentStatus.UNKNOWN,
                        at_utc=event.at_utc,
                        evidence="contradictory broker evidence",
                    )
                    machine.apply(
                        IntentStatus.RECONCILIATION_REQUIRED,
                        at_utc=event.at_utc,
                        evidence="contradictory broker evidence",
                    )
                except OrderTransitionError:
                    pass
        del now_utc
        return events

    def hydrate_from_ledger(self, snapshots: dict[str, tuple[IntentStatus, int]]) -> int:
        """Restore in-flight orders from durable ledger snapshots (startup).

        Rebuilds ``_orders`` so a post-restart broker event for a previously
        working order applies to a real state machine instead of triggering an
        UNKNOWN_ORDER halt (closes the R-23 evidence-loss gap). Only called
        during the non-trading reconciliation phase; never mid-session.
        Returns the number of orders hydrated.
        """

        for intent_id, (status, filled) in snapshots.items():
            self._orders[intent_id] = OrderStateMachine.rehydrated(
                intent_id, status=status, filled_quantity=filled
            )
        return len(snapshots)

    def order_status(self, intent_id: str) -> IntentStatus | None:
        machine = self._orders.get(intent_id)
        return machine.status if machine else None

    def working_intent_ids(self) -> tuple[str, ...]:
        return tuple(intent_id for intent_id, machine in self._orders.items() if machine.is_working)

    def _apply_event(self, machine: OrderStateMachine, event: BrokerEvent) -> None:
        if event.kind is BrokerEventKind.ACKNOWLEDGED:
            applied = machine.apply(
                IntentStatus.ACKNOWLEDGED, at_utc=event.at_utc, evidence=event.detail
            )
        elif event.kind is BrokerEventKind.PARTIAL_FILL:
            if event.cumulative_filled == machine.filled_quantity:
                return  # duplicate partial-fill event: idempotent
            machine.record_partial_fill(event.cumulative_filled, at_utc=event.at_utc)
            applied = True
        elif event.kind is BrokerEventKind.FILLED:
            # Cumulative fills are monotonic; a FILLED reporting fewer shares
            # than we have already seen filled is a broker/model contradiction
            # and must halt, exactly as the partial-fill path does — not be
            # silently accepted (the fill count would otherwise rewind).
            if event.cumulative_filled < machine.filled_quantity:
                raise OrderTransitionError(
                    f"intent {machine.intent_id}: FILLED cumulative fill decreased "
                    f"({machine.filled_quantity} -> {event.cumulative_filled})"
                )
            applied = machine.apply(IntentStatus.FILLED, at_utc=event.at_utc, evidence=event.detail)
            # Only trust the reported count when the transition actually
            # happened; a duplicate FILLED on an already-terminal machine is a
            # no-op and must not overwrite in-memory state off the ledger.
            if applied:
                machine.filled_quantity = event.cumulative_filled
        elif event.kind is BrokerEventKind.CANCELLED:
            applied = machine.apply(
                IntentStatus.CANCELLED, at_utc=event.at_utc, evidence=event.detail
            )
        elif event.kind is BrokerEventKind.REJECTED:
            applied = machine.apply(
                IntentStatus.REJECTED, at_utc=event.at_utc, evidence=event.detail
            )
        else:
            applied = machine.apply(
                IntentStatus.UNKNOWN, at_utc=event.at_utc, evidence=event.detail
            )
            if applied:
                machine.apply(
                    IntentStatus.RECONCILIATION_REQUIRED,
                    at_utc=event.at_utc,
                    evidence="broker error event",
                )
                self.reconciliation_passed = False
        if applied:
            self.ledger.record_transition(
                machine.intent_id, machine.status, event.at_utc, event.detail
            )
            if event.kind in (BrokerEventKind.PARTIAL_FILL, BrokerEventKind.FILLED):
                self.ledger.record_fill(
                    machine.intent_id,
                    event.cumulative_filled,
                    event.average_fill_price,
                    event.commission_usd,
                    event.at_utc,
                )

    def _transition(
        self, machine: OrderStateMachine, status: IntentStatus, *, at_utc: datetime, evidence: str
    ) -> None:
        machine.apply(status, at_utc=at_utc, evidence=evidence)
        self.ledger.record_transition(machine.intent_id, status, at_utc, evidence)
