"""Tests for the execution-plane reconciliation comparison."""

from __future__ import annotations

from chronos.execution.intents import IntentStatus
from chronos.execution.reconciliation import (
    DiscrepancyKind,
    LifecycleBucket,
    OrderStateView,
    lifecycle_bucket,
    reconcile,
)


def test_clean_reconciliation_passes() -> None:
    report = reconcile(
        broker_open_order_refs=("a", "b"),
        broker_positions={"SPY": 0},
        ledger_working_intent_ids=("a", "b"),
        explained_position_symbols=frozenset(),
    )
    assert report.passed
    assert report.discrepancies == ()


def test_unknown_broker_order_blocks() -> None:
    report = reconcile(
        broker_open_order_refs=("a", "rogue"),
        broker_positions={},
        ledger_working_intent_ids=("a",),
        explained_position_symbols=frozenset(),
    )
    assert not report.passed
    kinds = {d.kind for d in report.discrepancies}
    assert DiscrepancyKind.UNKNOWN_BROKER_ORDER in kinds
    assert any(d.reference == "rogue" for d in report.discrepancies)


def test_missing_broker_order_blocks() -> None:
    report = reconcile(
        broker_open_order_refs=("a",),
        broker_positions={},
        ledger_working_intent_ids=("a", "ghost"),
        explained_position_symbols=frozenset(),
    )
    assert not report.passed
    assert any(
        d.kind is DiscrepancyKind.MISSING_BROKER_ORDER and d.reference == "ghost"
        for d in report.discrepancies
    )


def test_unexplained_position_blocks() -> None:
    report = reconcile(
        broker_open_order_refs=(),
        broker_positions={"AAPL": 100},
        ledger_working_intent_ids=(),
        explained_position_symbols=frozenset(),
    )
    assert not report.passed
    assert any(d.kind is DiscrepancyKind.UNEXPLAINED_POSITION for d in report.discrepancies)


def test_explained_position_ok() -> None:
    report = reconcile(
        broker_open_order_refs=(),
        broker_positions={"AAPL": 100},
        ledger_working_intent_ids=(),
        explained_position_symbols=frozenset({"AAPL"}),
    )
    assert report.passed


def test_zero_position_needs_no_explanation() -> None:
    report = reconcile(
        broker_open_order_refs=(),
        broker_positions={"AAPL": 0},
        ledger_working_intent_ids=(),
        explained_position_symbols=frozenset(),
    )
    assert report.passed


def _view(status: IntentStatus, filled: int) -> OrderStateView:
    return OrderStateView(status=status, filled_quantity=filled)


def test_lifecycle_bucket_coarsening() -> None:
    assert lifecycle_bucket(IntentStatus.FILLED) is LifecycleBucket.FILLED
    assert lifecycle_bucket(IntentStatus.SUBMITTED) is LifecycleBucket.WORKING
    assert lifecycle_bucket(IntentStatus.ACKNOWLEDGED) is LifecycleBucket.WORKING
    assert lifecycle_bucket(IntentStatus.CANCELLED) is LifecycleBucket.CLOSED
    assert lifecycle_bucket(IntentStatus.REJECTED) is LifecycleBucket.CLOSED


def test_state_comparison_absent_by_default() -> None:
    # Without state maps, only id-set/position comparison runs (backward compat).
    report = reconcile(
        broker_open_order_refs=("a",),
        broker_positions={},
        ledger_working_intent_ids=("a",),
        explained_position_symbols=frozenset(),
    )
    assert report.passed


def test_broker_fill_ledger_missed_is_mismatch() -> None:
    # Broker reports FILLED-5; ledger still thinks it is working, filled 3.
    report = reconcile(
        broker_open_order_refs=(),
        broker_positions={},
        ledger_working_intent_ids=("a",),
        explained_position_symbols=frozenset(),
        broker_order_states={"a": _view(IntentStatus.FILLED, 5)},
        ledger_order_states={"a": _view(IntentStatus.PARTIALLY_FILLED, 3)},
    )
    assert not report.passed
    assert any(d.kind is DiscrepancyKind.ORDER_STATE_MISMATCH for d in report.discrepancies)


def test_fill_count_drift_is_mismatch() -> None:
    report = reconcile(
        broker_open_order_refs=("a",),
        broker_positions={},
        ledger_working_intent_ids=("a",),
        explained_position_symbols=frozenset(),
        broker_order_states={"a": _view(IntentStatus.PARTIALLY_FILLED, 4)},
        ledger_order_states={"a": _view(IntentStatus.PARTIALLY_FILLED, 2)},
    )
    assert not report.passed
    assert report.discrepancies[0].kind is DiscrepancyKind.ORDER_STATE_MISMATCH


def test_benign_lag_within_working_bucket_is_not_a_mismatch() -> None:
    # PreSubmitted vs Submitted are both WORKING, filled 0 -> no state mismatch.
    report = reconcile(
        broker_open_order_refs=("a",),
        broker_positions={},
        ledger_working_intent_ids=("a",),
        explained_position_symbols=frozenset(),
        broker_order_states={"a": _view(IntentStatus.PRE_SUBMITTED, 0)},
        ledger_order_states={"a": _view(IntentStatus.SUBMITTED, 0)},
    )
    assert report.passed


def test_state_comparison_only_over_intersection() -> None:
    # An order only in the broker state map (not the ledger's) is not compared
    # here; it surfaces via the id-set checks instead, not ORDER_STATE_MISMATCH.
    report = reconcile(
        broker_open_order_refs=("a",),
        broker_positions={},
        ledger_working_intent_ids=("a",),
        explained_position_symbols=frozenset(),
        broker_order_states={
            "a": _view(IntentStatus.FILLED, 5),
            "b": _view(IntentStatus.FILLED, 1),
        },
        ledger_order_states={"a": _view(IntentStatus.FILLED, 5)},
    )
    assert report.passed
