"""Tests for the execution-plane reconciliation comparison."""

from __future__ import annotations

from chronos.execution.reconciliation import DiscrepancyKind, reconcile


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
