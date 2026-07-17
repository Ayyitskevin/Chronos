"""Memory and SQLite order ledgers: duplicate safety, persistence, schema."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from chronos.domain.enums import OrderSide
from chronos.execution.intents import IntentStatus, OrderIntent, TimeInForce
from chronos.execution.ledger import MemoryLedger
from chronos.execution.sqlite_ledger import SqliteLedger

NOW = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)


def make_intent(**overrides: object) -> OrderIntent:
    payload: dict[str, object] = {
        "strategy_id": "strat_a",
        "strategy_version": "1",
        "symbol": "SPY",
        "side": OrderSide.BUY,
        "quantity": 5,
        "limit_price": Decimal("500.10"),
        "stop_price": Decimal("485.00"),
        "time_in_force": TimeInForce.DAY,
        "decision_timestamp_utc": NOW,
        "source_bar_sequence_id": "test:SPY:1d:2024-06-03",
        "proposal_reason": "test",
    }
    payload.update(overrides)
    return OrderIntent(**payload)  # type: ignore[arg-type]


class TestMemoryLedger:
    def test_record_and_lookup_intent(self) -> None:
        ledger = MemoryLedger()
        intent = make_intent()
        assert not ledger.has_intent(intent.intent_id)
        ledger.record_intent(intent, IntentStatus.PENDING_SUBMISSION)
        assert ledger.has_intent(intent.intent_id)
        assert ledger.statuses[intent.intent_id] is IntentStatus.PENDING_SUBMISSION

    def test_duplicate_intent_raises(self) -> None:
        ledger = MemoryLedger()
        intent = make_intent()
        ledger.record_intent(intent, IntentStatus.PENDING_SUBMISSION)
        with pytest.raises(ValueError, match="duplicate intent id"):
            ledger.record_intent(intent, IntentStatus.PENDING_SUBMISSION)

    def test_transitions_and_fills_are_recorded_in_order(self) -> None:
        ledger = MemoryLedger()
        intent = make_intent()
        ledger.record_intent(intent, IntentStatus.PENDING_SUBMISSION)
        ledger.record_transition(intent.intent_id, IntentStatus.SUBMITTED, NOW, "sent")
        ledger.record_transition(intent.intent_id, IntentStatus.FILLED, NOW, "filled")
        ledger.record_fill(intent.intent_id, 5, Decimal("500.10"), Decimal("1.00"), NOW)

        assert [t.status for t in ledger.transitions] == [
            IntentStatus.SUBMITTED,
            IntentStatus.FILLED,
        ]
        assert ledger.statuses[intent.intent_id] is IntentStatus.FILLED
        assert len(ledger.fills) == 1
        assert ledger.fills[0].cumulative_quantity == 5
        assert ledger.fills[0].average_price == Decimal("500.10")

    def test_fail_writes_raises_oserror(self) -> None:
        ledger = MemoryLedger(fail_writes=True)
        with pytest.raises(OSError):
            ledger.record_intent(make_intent(), IntentStatus.PENDING_SUBMISSION)
        with pytest.raises(OSError):
            ledger.has_intent("anything")


class TestSqliteLedger:
    def test_record_and_lookup_intent(self, tmp_path: Path) -> None:
        ledger = SqliteLedger(tmp_path / "ledger.db")
        intent = make_intent()
        assert not ledger.has_intent(intent.intent_id)
        ledger.record_intent(intent, IntentStatus.PENDING_SUBMISSION)
        assert ledger.has_intent(intent.intent_id)
        ledger.close()

    def test_duplicate_intent_violates_primary_key(self, tmp_path: Path) -> None:
        ledger = SqliteLedger(tmp_path / "ledger.db")
        intent = make_intent()
        ledger.record_intent(intent, IntentStatus.PENDING_SUBMISSION)
        with pytest.raises(sqlite3.IntegrityError):
            ledger.record_intent(intent, IntentStatus.PENDING_SUBMISSION)
        ledger.close()

    def test_persists_across_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.db"
        intent = make_intent()
        first = SqliteLedger(path)
        first.record_intent(intent, IntentStatus.PENDING_SUBMISSION)
        first.record_transition(intent.intent_id, IntentStatus.SUBMITTED, NOW, "sent")
        first.record_fill(intent.intent_id, 2, Decimal("500.10"), Decimal("1.00"), NOW)
        first.close()

        second = SqliteLedger(path)
        assert second.has_intent(intent.intent_id)
        assert second.working_intent_ids() == (intent.intent_id,)
        second.close()

        # Raw row check through an independent connection.
        connection = sqlite3.connect(str(path))
        try:
            transitions = connection.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
            fills = connection.execute(
                "SELECT cumulative_quantity, average_price FROM fills"
            ).fetchall()
        finally:
            connection.close()
        assert transitions == 1
        assert fills == [(2, "500.10")]

    def test_working_intent_ids_reflects_latest_transition(self, tmp_path: Path) -> None:
        ledger = SqliteLedger(tmp_path / "ledger.db")
        intent = make_intent()
        ledger.record_intent(intent, IntentStatus.PENDING_SUBMISSION)

        ledger.record_transition(intent.intent_id, IntentStatus.SUBMITTED, NOW, "sent")
        assert ledger.working_intent_ids() == (intent.intent_id,)

        ledger.record_transition(intent.intent_id, IntentStatus.PARTIALLY_FILLED, NOW, "pf")
        assert ledger.working_intent_ids() == (intent.intent_id,)

        ledger.record_transition(intent.intent_id, IntentStatus.FILLED, NOW, "done")
        assert ledger.working_intent_ids() == ()
        ledger.close()

    def test_schema_version_mismatch_refuses_to_open(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.db"
        SqliteLedger(path).close()

        connection = sqlite3.connect(str(path))
        connection.execute("UPDATE schema_info SET version = 99")
        connection.commit()
        connection.close()

        with pytest.raises(RuntimeError, match="schema version 99"):
            SqliteLedger(path)
