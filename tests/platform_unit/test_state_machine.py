"""Order-intent state machine behaviour: legality, idempotency, fail-loud."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from chronos.execution.intents import IntentStatus
from chronos.execution.state_machine import OrderStateMachine, OrderTransitionError

NOW = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)


def machine_at(*statuses: IntentStatus) -> OrderStateMachine:
    machine = OrderStateMachine(intent_id="intent-1")
    for status in statuses:
        machine.apply(status, at_utc=NOW, evidence="setup")
    return machine


HAPPY_PATH = (
    IntentStatus.RISK_APPROVED,
    IntentStatus.PENDING_SUBMISSION,
    IntentStatus.SUBMITTED,
    IntentStatus.ACKNOWLEDGED,
    IntentStatus.PARTIALLY_FILLED,
    IntentStatus.FILLED,
)


class TestHappyPath:
    def test_full_lifecycle_is_legal_and_recorded(self) -> None:
        machine = OrderStateMachine(intent_id="intent-1")
        assert machine.status is IntentStatus.CREATED
        for status in HAPPY_PATH:
            assert machine.apply(status, at_utc=NOW, evidence=f"to {status}") is True
            assert machine.status is status
        assert machine.is_terminal
        assert not machine.is_working
        # History records every hop in order, from CREATED through FILLED.
        assert [t.from_status for t in machine.history] == [
            IntentStatus.CREATED,
            *HAPPY_PATH[:-1],
        ]
        assert [t.to_status for t in machine.history] == list(HAPPY_PATH)

    def test_working_statuses(self) -> None:
        machine = machine_at(
            IntentStatus.RISK_APPROVED,
            IntentStatus.PENDING_SUBMISSION,
            IntentStatus.SUBMITTED,
        )
        assert machine.is_working
        assert not machine.is_terminal


class TestIdempotency:
    def test_duplicate_event_is_a_noop_not_an_error(self) -> None:
        machine = machine_at(
            IntentStatus.RISK_APPROVED,
            IntentStatus.PENDING_SUBMISSION,
            IntentStatus.SUBMITTED,
            IntentStatus.ACKNOWLEDGED,
        )
        history_length = len(machine.history)
        assert machine.apply(IntentStatus.ACKNOWLEDGED, at_utc=NOW, evidence="dup") is False
        assert machine.status is IntentStatus.ACKNOWLEDGED
        assert len(machine.history) == history_length

    def test_duplicate_terminal_event_returns_false(self) -> None:
        machine = machine_at(*HAPPY_PATH)
        assert machine.apply(IntentStatus.FILLED, at_utc=NOW, evidence="dup") is False
        assert machine.status is IntentStatus.FILLED

    def test_partially_filled_reapplies(self) -> None:
        # PARTIALLY_FILLED -> PARTIALLY_FILLED is a real transition (a further
        # partial execution), not a duplicate.
        machine = machine_at(
            IntentStatus.RISK_APPROVED,
            IntentStatus.PENDING_SUBMISSION,
            IntentStatus.SUBMITTED,
            IntentStatus.PARTIALLY_FILLED,
        )
        history_length = len(machine.history)
        assert machine.apply(IntentStatus.PARTIALLY_FILLED, at_utc=NOW, evidence="more") is True
        assert len(machine.history) == history_length + 1


class TestIllegalTransitions:
    def test_created_cannot_jump_to_filled(self) -> None:
        machine = OrderStateMachine(intent_id="intent-1")
        with pytest.raises(OrderTransitionError):
            machine.apply(IntentStatus.FILLED, at_utc=NOW, evidence="jump")

    def test_created_cannot_jump_to_submitted(self) -> None:
        machine = OrderStateMachine(intent_id="intent-1")
        with pytest.raises(OrderTransitionError):
            machine.apply(IntentStatus.SUBMITTED, at_utc=NOW, evidence="jump")

    @pytest.mark.parametrize(
        "target",
        [
            IntentStatus.CANCELLED,
            IntentStatus.SUBMITTED,
            IntentStatus.PARTIALLY_FILLED,
            IntentStatus.RISK_APPROVED,
        ],
    )
    def test_terminal_filled_rejects_everything_else(self, target: IntentStatus) -> None:
        machine = machine_at(*HAPPY_PATH)
        with pytest.raises(OrderTransitionError):
            machine.apply(target, at_utc=NOW, evidence="post-terminal")

    def test_terminal_cancelled_rejects_fill(self) -> None:
        machine = machine_at(
            IntentStatus.RISK_APPROVED,
            IntentStatus.PENDING_SUBMISSION,
            IntentStatus.SUBMITTED,
            IntentStatus.CANCELLED,
        )
        with pytest.raises(OrderTransitionError):
            machine.apply(IntentStatus.FILLED, at_utc=NOW, evidence="late fill")

    def test_rejected_is_terminal(self) -> None:
        machine = machine_at(
            IntentStatus.RISK_APPROVED,
            IntentStatus.PENDING_SUBMISSION,
            IntentStatus.SUBMITTED,
            IntentStatus.REJECTED,
        )
        assert machine.is_terminal
        with pytest.raises(OrderTransitionError):
            machine.apply(IntentStatus.SUBMITTED, at_utc=NOW, evidence="retry")


class TestFillAccounting:
    def test_decreasing_cumulative_fill_raises(self) -> None:
        machine = machine_at(
            IntentStatus.RISK_APPROVED,
            IntentStatus.PENDING_SUBMISSION,
            IntentStatus.SUBMITTED,
        )
        machine.record_partial_fill(3, at_utc=NOW)
        assert machine.filled_quantity == 3
        with pytest.raises(OrderTransitionError):
            machine.record_partial_fill(2, at_utc=NOW)

    def test_increasing_cumulative_fill_accumulates(self) -> None:
        machine = machine_at(
            IntentStatus.RISK_APPROVED,
            IntentStatus.PENDING_SUBMISSION,
            IntentStatus.SUBMITTED,
        )
        machine.record_partial_fill(2, at_utc=NOW)
        machine.record_partial_fill(5, at_utc=NOW)
        assert machine.filled_quantity == 5
        assert machine.status is IntentStatus.PARTIALLY_FILLED

    def test_partial_fill_on_terminal_machine_leaves_quantity_unchanged(self) -> None:
        # A partial fill arriving after the order is terminal (FILLED) is an
        # illegal transition; it must raise AND must not smear filled_quantity
        # to a value the machine never accepted. The passed count (12) is >= the
        # current fill (10), so it clears the monotonic guard and reaches apply().
        machine = machine_at(
            IntentStatus.RISK_APPROVED,
            IntentStatus.PENDING_SUBMISSION,
            IntentStatus.SUBMITTED,
        )
        machine.record_partial_fill(10, at_utc=NOW)
        machine.apply(IntentStatus.FILLED, at_utc=NOW, evidence="fully filled")
        assert machine.filled_quantity == 10
        with pytest.raises(OrderTransitionError):
            machine.record_partial_fill(12, at_utc=NOW)
        assert machine.filled_quantity == 10  # unchanged despite the raise


class TestTimestampsAndUnknown:
    def test_naive_datetime_raises(self) -> None:
        machine = OrderStateMachine(intent_id="intent-1")
        with pytest.raises(OrderTransitionError):
            machine.apply(
                IntentStatus.RISK_APPROVED,
                at_utc=datetime(2024, 6, 3, 21, 0),  # noqa: DTZ001 - naive on purpose
                evidence="naive",
            )
        assert machine.status is IntentStatus.CREATED

    def test_unknown_only_allows_reconciliation_required(self) -> None:
        machine = machine_at(
            IntentStatus.RISK_APPROVED,
            IntentStatus.PENDING_SUBMISSION,
            IntentStatus.SUBMITTED,
            IntentStatus.UNKNOWN,
        )
        for target in (
            IntentStatus.ACKNOWLEDGED,
            IntentStatus.FILLED,
            IntentStatus.CANCELLED,
            IntentStatus.SUBMITTED,
        ):
            with pytest.raises(OrderTransitionError):
                machine.apply(target, at_utc=NOW, evidence="from unknown")
        assert (
            machine.apply(IntentStatus.RECONCILIATION_REQUIRED, at_utc=NOW, evidence="ok") is True
        )
        assert machine.status is IntentStatus.RECONCILIATION_REQUIRED
