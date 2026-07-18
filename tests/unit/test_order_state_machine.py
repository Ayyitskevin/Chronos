"""The pure order-lifecycle state machine (Milestone 5)."""

from __future__ import annotations

import pytest

from chronos.domain.enums import OrderLifecycle
from chronos.orders.state_machine import (
    OrderLifecycleError,
    OrderLifecycleMachine,
    is_terminal,
    transition_allowed,
)


def test_happy_path_transitions_advance() -> None:
    machine = OrderLifecycleMachine(OrderLifecycle.DRAFT)
    assert machine.apply(OrderLifecycle.VALIDATED) is True
    assert machine.apply(OrderLifecycle.WHAT_IF_PREVIEWED) is True
    assert machine.apply(OrderLifecycle.USER_CONFIRMED) is True
    assert machine.apply(OrderLifecycle.SUBMITTED) is True
    assert machine.apply(OrderLifecycle.PARTIALLY_FILLED) is True
    assert machine.apply(OrderLifecycle.FILLED) is True
    assert machine.status is OrderLifecycle.FILLED


def test_illegal_transition_raises() -> None:
    machine = OrderLifecycleMachine(OrderLifecycle.DRAFT)
    with pytest.raises(OrderLifecycleError):
        machine.apply(OrderLifecycle.FILLED)


def test_duplicate_same_status_is_idempotent_noop() -> None:
    machine = OrderLifecycleMachine(OrderLifecycle.SUBMITTED)
    assert machine.apply(OrderLifecycle.SUBMITTED) is False
    assert machine.status is OrderLifecycle.SUBMITTED


def test_partial_fill_may_follow_itself() -> None:
    machine = OrderLifecycleMachine(OrderLifecycle.PARTIALLY_FILLED)
    assert machine.apply(OrderLifecycle.PARTIALLY_FILLED) is True


def test_terminal_absorbs_same_and_late_but_rejects_contradiction() -> None:
    filled = OrderLifecycleMachine(OrderLifecycle.FILLED)
    assert filled.apply(OrderLifecycle.FILLED) is False  # duplicate terminal
    assert filled.apply(OrderLifecycle.PARTIALLY_FILLED) is False  # benign late callback
    with pytest.raises(OrderLifecycleError):
        filled.apply(OrderLifecycle.CANCELLED)  # contradictory terminal


def test_submission_unknown_resolves_only_forward() -> None:
    machine = OrderLifecycleMachine(OrderLifecycle.SUBMISSION_UNKNOWN)
    assert transition_allowed(OrderLifecycle.SUBMISSION_UNKNOWN, OrderLifecycle.SUBMITTED)
    assert machine.apply(OrderLifecycle.FILLED) is True


def test_cancel_pending_can_still_fill() -> None:
    machine = OrderLifecycleMachine(OrderLifecycle.CANCEL_PENDING)
    assert machine.apply(OrderLifecycle.FILLED) is True


def test_terminal_helpers() -> None:
    assert is_terminal(OrderLifecycle.FILLED)
    assert is_terminal(OrderLifecycle.CANCELLED)
    assert is_terminal(OrderLifecycle.REJECTED)
    assert not is_terminal(OrderLifecycle.SUBMITTED)
