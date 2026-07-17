"""Order-intent state machine (Phase 9).

Transitions are driven by risk decisions and *broker evidence* (acks, status
events, execution events), never by the mere return of a submission call.
Illegal transitions raise; the execution engine converts that into a halt,
because an illegal transition means our model of the broker diverged.

Duplicate broker events are idempotent: re-applying the event that produced
the current state is a no-op, not an error. Unknown or contradictory evidence
moves the order to RECONCILIATION_REQUIRED, which blocks conflicting orders
until an operator or the reconciler resolves it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from chronos.execution.intents import IntentStatus

_TERMINAL = frozenset(
    {
        IntentStatus.RISK_REJECTED,
        IntentStatus.FILLED,
        IntentStatus.CANCELLED,
        IntentStatus.REJECTED,
        IntentStatus.INACTIVE,
    }
)

_ALLOWED: dict[IntentStatus, frozenset[IntentStatus]] = {
    IntentStatus.CREATED: frozenset({IntentStatus.RISK_REJECTED, IntentStatus.RISK_APPROVED}),
    IntentStatus.RISK_APPROVED: frozenset({IntentStatus.PENDING_SUBMISSION, IntentStatus.INACTIVE}),
    IntentStatus.PENDING_SUBMISSION: frozenset(
        {
            IntentStatus.SUBMITTED,
            IntentStatus.REJECTED,
            IntentStatus.UNKNOWN,
            IntentStatus.INACTIVE,
        }
    ),
    IntentStatus.SUBMITTED: frozenset(
        {
            IntentStatus.PRE_SUBMITTED,
            IntentStatus.ACKNOWLEDGED,
            IntentStatus.PARTIALLY_FILLED,
            IntentStatus.FILLED,
            IntentStatus.PENDING_CANCEL,
            IntentStatus.CANCELLED,
            IntentStatus.REJECTED,
            IntentStatus.UNKNOWN,
        }
    ),
    IntentStatus.PRE_SUBMITTED: frozenset(
        {
            IntentStatus.ACKNOWLEDGED,
            IntentStatus.PARTIALLY_FILLED,
            IntentStatus.FILLED,
            IntentStatus.PENDING_CANCEL,
            IntentStatus.CANCELLED,
            IntentStatus.REJECTED,
            IntentStatus.UNKNOWN,
        }
    ),
    IntentStatus.ACKNOWLEDGED: frozenset(
        {
            IntentStatus.PARTIALLY_FILLED,
            IntentStatus.FILLED,
            IntentStatus.PENDING_CANCEL,
            IntentStatus.CANCELLED,
            IntentStatus.REJECTED,
            IntentStatus.UNKNOWN,
        }
    ),
    IntentStatus.PARTIALLY_FILLED: frozenset(
        {
            IntentStatus.PARTIALLY_FILLED,
            IntentStatus.FILLED,
            IntentStatus.PENDING_CANCEL,
            IntentStatus.CANCELLED,
            IntentStatus.UNKNOWN,
        }
    ),
    IntentStatus.PENDING_CANCEL: frozenset(
        {
            IntentStatus.PARTIALLY_FILLED,
            IntentStatus.FILLED,
            IntentStatus.CANCELLED,
            IntentStatus.REJECTED,
            IntentStatus.UNKNOWN,
        }
    ),
    IntentStatus.UNKNOWN: frozenset({IntentStatus.RECONCILIATION_REQUIRED}),
    IntentStatus.RECONCILIATION_REQUIRED: frozenset(
        {
            IntentStatus.ACKNOWLEDGED,
            IntentStatus.PARTIALLY_FILLED,
            IntentStatus.FILLED,
            IntentStatus.CANCELLED,
            IntentStatus.REJECTED,
            IntentStatus.INACTIVE,
        }
    ),
}


class OrderTransitionError(RuntimeError):
    """An event implied a transition the state machine forbids."""


@dataclass(frozen=True, slots=True)
class Transition:
    at_utc: datetime
    from_status: IntentStatus
    to_status: IntentStatus
    evidence: str


@dataclass
class OrderStateMachine:
    """Tracks one intent's lifecycle with an append-only transition history."""

    intent_id: str
    status: IntentStatus = IntentStatus.CREATED
    filled_quantity: int = 0
    history: list[Transition] = field(default_factory=list)

    @classmethod
    def rehydrated(
        cls, intent_id: str, *, status: IntentStatus, filled_quantity: int
    ) -> OrderStateMachine:
        """Reconstruct a machine at a persisted status without replaying history.

        Used at startup to restore in-flight orders from the durable ledger
        (which is the authoritative history), so a post-restart broker event
        applies to a real machine instead of triggering an UNKNOWN_ORDER halt.
        The persisted state is trusted; subsequent transitions are still
        validated against the legal transition table.
        """

        if filled_quantity < 0:
            raise OrderTransitionError("rehydrated filled_quantity cannot be negative")
        return cls(intent_id=intent_id, status=status, filled_quantity=filled_quantity)

    def apply(self, to_status: IntentStatus, *, at_utc: datetime, evidence: str) -> bool:
        """Apply a transition. Returns False for an idempotent duplicate.

        Raises ``OrderTransitionError`` for an illegal transition.
        """

        if at_utc.tzinfo is None:
            raise OrderTransitionError("transition timestamps must be timezone-aware")
        if to_status is self.status and to_status is not IntentStatus.PARTIALLY_FILLED:
            return False
        if self.status in _TERMINAL:
            if to_status is self.status:
                return False
            raise OrderTransitionError(
                f"intent {self.intent_id}: illegal transition from terminal "
                f"{self.status} to {to_status} ({evidence})"
            )
        allowed = _ALLOWED.get(self.status, frozenset())
        if to_status not in allowed:
            raise OrderTransitionError(
                f"intent {self.intent_id}: illegal transition {self.status} -> {to_status} "
                f"({evidence})"
            )
        self.history.append(
            Transition(
                at_utc=at_utc, from_status=self.status, to_status=to_status, evidence=evidence
            )
        )
        self.status = to_status
        return True

    def record_partial_fill(self, cumulative_quantity: int, *, at_utc: datetime) -> None:
        if cumulative_quantity < self.filled_quantity:
            raise OrderTransitionError(
                f"intent {self.intent_id}: cumulative fill decreased "
                f"({self.filled_quantity} -> {cumulative_quantity})"
            )
        self.filled_quantity = cumulative_quantity
        self.apply(
            IntentStatus.PARTIALLY_FILLED,
            at_utc=at_utc,
            evidence=f"cumulative fill {cumulative_quantity}",
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    @property
    def is_working(self) -> bool:
        return self.status in (
            IntentStatus.SUBMITTED,
            IntentStatus.PRE_SUBMITTED,
            IntentStatus.ACKNOWLEDGED,
            IntentStatus.PARTIALLY_FILLED,
            IntentStatus.PENDING_CANCEL,
        )
