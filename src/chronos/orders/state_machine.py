"""Pure order-lifecycle state machine (docs/LIVE_WHEEL_GAME_PLAN.md M5).

Distinct from the autonomous platform's ``chronos.execution.state_machine`` —
this one governs the human-in-the-loop, eventually-live-capable order path and
is named to avoid any collision with it. It is a pure function of
:class:`~chronos.domain.enums.OrderLifecycle`; nothing here touches a broker,
a database, or the clock.

Legality rules:

- Illegal transitions raise :class:`OrderLifecycleError` (the tracker routes
  that to manual review — never a silent overwrite).
- An idempotent re-application of the current status returns ``False`` and
  changes nothing (duplicate/late broker callbacks are benign).
- Terminal statuses (FILLED, CANCELLED, REJECTED) absorb further same-outcome
  callbacks as no-ops, but a transition to a *different* terminal status is a
  contradiction and raises.
- ``PARTIALLY_FILLED`` may legally follow itself (successive partial fills);
  the per-fill quantity accounting that distinguishes a real fill from a
  duplicate lives in the tracker, keyed by a unique broker event.
"""

from __future__ import annotations

from chronos.domain.enums import OrderLifecycle

TERMINAL_ORDER_LIFECYCLES: frozenset[OrderLifecycle] = frozenset(
    {
        OrderLifecycle.FILLED,
        OrderLifecycle.CANCELLED,
        OrderLifecycle.REJECTED,
    }
)

# Statuses that may legally follow themselves (successive partial fills only).
_SELF_TRANSITION_ALLOWED: frozenset[OrderLifecycle] = frozenset({OrderLifecycle.PARTIALLY_FILLED})

ALLOWED_ORDER_TRANSITIONS: dict[OrderLifecycle, frozenset[OrderLifecycle]] = {
    OrderLifecycle.DRAFT: frozenset({OrderLifecycle.VALIDATED, OrderLifecycle.REJECTED}),
    OrderLifecycle.VALIDATED: frozenset(
        {OrderLifecycle.WHAT_IF_PREVIEWED, OrderLifecycle.REJECTED}
    ),
    OrderLifecycle.WHAT_IF_PREVIEWED: frozenset(
        {
            OrderLifecycle.USER_CONFIRMED,
            OrderLifecycle.VALIDATED,  # evidence refresh forces re-validation
            OrderLifecycle.REJECTED,
        }
    ),
    OrderLifecycle.USER_CONFIRMED: frozenset(
        {
            OrderLifecycle.SUBMITTED,
            OrderLifecycle.SUBMISSION_UNKNOWN,
            OrderLifecycle.REJECTED,
        }
    ),
    OrderLifecycle.SUBMITTED: frozenset(
        {
            OrderLifecycle.PARTIALLY_FILLED,
            OrderLifecycle.FILLED,
            OrderLifecycle.CANCEL_PENDING,
            OrderLifecycle.CANCELLED,
            OrderLifecycle.REJECTED,
            OrderLifecycle.SUBMISSION_UNKNOWN,
        }
    ),
    OrderLifecycle.SUBMISSION_UNKNOWN: frozenset(
        {
            OrderLifecycle.SUBMITTED,
            OrderLifecycle.PARTIALLY_FILLED,
            OrderLifecycle.FILLED,
            OrderLifecycle.CANCELLED,
            OrderLifecycle.REJECTED,
        }
    ),
    OrderLifecycle.PARTIALLY_FILLED: frozenset(
        {
            OrderLifecycle.PARTIALLY_FILLED,
            OrderLifecycle.FILLED,
            OrderLifecycle.CANCEL_PENDING,
            OrderLifecycle.CANCELLED,
            OrderLifecycle.REJECTED,
        }
    ),
    OrderLifecycle.CANCEL_PENDING: frozenset(
        {
            OrderLifecycle.CANCELLED,
            OrderLifecycle.FILLED,
            OrderLifecycle.PARTIALLY_FILLED,
            OrderLifecycle.REJECTED,
        }
    ),
    OrderLifecycle.FILLED: frozenset(),
    OrderLifecycle.CANCELLED: frozenset(),
    OrderLifecycle.REJECTED: frozenset(),
}


class OrderLifecycleError(RuntimeError):
    """An illegal order-lifecycle transition was attempted."""


def is_terminal(status: OrderLifecycle) -> bool:
    return status in TERMINAL_ORDER_LIFECYCLES


def transition_allowed(from_status: OrderLifecycle, to_status: OrderLifecycle) -> bool:
    """Whether ``from_status -> to_status`` is a legal, non-idempotent advance."""

    return to_status in ALLOWED_ORDER_TRANSITIONS.get(from_status, frozenset())


class OrderLifecycleMachine:
    """A mutable current-status holder that enforces the transition table."""

    def __init__(self, status: OrderLifecycle = OrderLifecycle.DRAFT) -> None:
        self._status = status

    @property
    def status(self) -> OrderLifecycle:
        return self._status

    def apply(self, to_status: OrderLifecycle) -> bool:
        """Advance to ``to_status``.

        Returns ``True`` when the status genuinely advanced, ``False`` for an
        idempotent no-op (a duplicate/late callback), and raises
        :class:`OrderLifecycleError` for a contradictory transition.
        """

        current = self._status
        if current in TERMINAL_ORDER_LIFECYCLES:
            if to_status == current:
                return False
            if to_status in TERMINAL_ORDER_LIFECYCLES:
                raise OrderLifecycleError(
                    f"contradictory terminal transition {current.value} -> {to_status.value}"
                )
            # A non-terminal callback arriving after a terminal state is a
            # benign out-of-order broker message; absorb it.
            return False
        if to_status == current and to_status not in _SELF_TRANSITION_ALLOWED:
            return False
        if to_status not in ALLOWED_ORDER_TRANSITIONS[current]:
            raise OrderLifecycleError(
                f"illegal order transition {current.value} -> {to_status.value}"
            )
        self._status = to_status
        return True
