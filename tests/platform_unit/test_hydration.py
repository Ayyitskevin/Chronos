"""Startup order-state hydration from the durable ledger (closes R-23).

Before hydration, a broker event arriving after a restart for an order the
fresh in-memory engine never saw triggered an UNKNOWN_ORDER halt and dropped
the event's evidence. Hydration restores in-flight orders from the ledger so
the event applies to a real state machine and is recorded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from chronos.control.halt import HaltReason, HaltStore
from chronos.control.modes import TradingMode, resolve_mode_lock
from chronos.domain.enums import OrderSide
from chronos.execution.brokers.port import BrokerEvent, BrokerEventKind
from chronos.execution.engine import ExecutionEngine
from chronos.execution.intents import IntentStatus, OrderIntent, TimeInForce
from chronos.execution.ledger import MemoryLedger
from chronos.execution.sqlite_ledger import SqliteLedger
from chronos.execution.state_machine import OrderStateMachine
from chronos.risk.engine import RiskEngine
from chronos.risk.policy import RiskPolicy

NOW = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)


def make_intent(intent_id_seed: str = "2024-06-03") -> OrderIntent:
    return OrderIntent(
        strategy_id="s",
        strategy_version="1",
        symbol="SPY",
        side=OrderSide.BUY,
        quantity=10,
        limit_price=Decimal("500.00"),
        stop_price=Decimal("485.00"),
        time_in_force=TimeInForce.DAY,
        decision_timestamp_utc=NOW,
        source_bar_sequence_id=f"test:SPY:1d:{intent_id_seed}",
        proposal_reason="test",
    )


class _OneEventBroker:
    """Minimal ExecutionBrokerPort that emits one preset event then nothing."""

    def __init__(self, event: BrokerEvent) -> None:
        self._event = event
        self._drained = False

    def submit(self, intent: OrderIntent):  # pragma: no cover - unused here
        raise AssertionError("not used")

    def request_cancel(self, intent_id: str) -> None:  # pragma: no cover
        pass

    def drain_events(self) -> tuple[BrokerEvent, ...]:
        if self._drained:
            return ()
        self._drained = True
        return (self._event,)


def sim_lock():
    return resolve_mode_lock(
        requested_mode=TradingMode.BACKTEST,
        paper_account_allowlist=(),
        broker_reported_account_id=None,
        broker_reported_environment_is_paper=None,
        order_transmission_enabled=False,
    )


def armed_halt(tmp_path: Path) -> HaltStore:
    store = HaltStore(tmp_path / "halt.json")
    store.rearm("armed")
    return store


class TestRehydratedConstructor:
    def test_rehydrated_sets_state(self) -> None:
        m = OrderStateMachine.rehydrated("x", status=IntentStatus.ACKNOWLEDGED, filled_quantity=3)
        assert m.status is IntentStatus.ACKNOWLEDGED
        assert m.filled_quantity == 3
        assert m.is_working

    def test_rehydrated_then_legal_transition(self) -> None:
        m = OrderStateMachine.rehydrated("x", status=IntentStatus.ACKNOWLEDGED, filled_quantity=0)
        assert m.apply(IntentStatus.FILLED, at_utc=NOW, evidence="fill")


class TestLedgerSnapshots:
    def test_memory_ledger_snapshot(self) -> None:
        ledger = MemoryLedger()
        intent = make_intent()
        ledger.record_intent(intent, IntentStatus.PENDING_SUBMISSION)
        ledger.record_transition(intent.intent_id, IntentStatus.SUBMITTED, NOW, "sent")
        ledger.record_transition(intent.intent_id, IntentStatus.PARTIALLY_FILLED, NOW, "pf")
        ledger.record_fill(intent.intent_id, 4, Decimal("500"), Decimal("1"), NOW)
        snaps = ledger.working_order_snapshots()
        assert snaps[intent.intent_id] == (IntentStatus.PARTIALLY_FILLED, 4)
        assert ledger.working_intent_ids() == (intent.intent_id,)

    def test_memory_ledger_snapshot_excludes_terminal(self) -> None:
        ledger = MemoryLedger()
        intent = make_intent()
        ledger.record_intent(intent, IntentStatus.PENDING_SUBMISSION)
        ledger.record_transition(intent.intent_id, IntentStatus.SUBMITTED, NOW, "sent")
        ledger.record_transition(intent.intent_id, IntentStatus.FILLED, NOW, "filled")
        assert ledger.working_order_snapshots() == {}

    def test_sqlite_ledger_snapshot(self, tmp_path: Path) -> None:
        ledger = SqliteLedger(tmp_path / "l.db")
        intent = make_intent()
        ledger.record_intent(intent, IntentStatus.PENDING_SUBMISSION)
        ledger.record_transition(intent.intent_id, IntentStatus.SUBMITTED, NOW, "sent")
        ledger.record_transition(intent.intent_id, IntentStatus.PARTIALLY_FILLED, NOW, "pf")
        ledger.record_fill(intent.intent_id, 6, Decimal("500"), Decimal("1"), NOW)
        snaps = ledger.working_order_snapshots()
        ledger.close()
        assert snaps[intent.intent_id] == (IntentStatus.PARTIALLY_FILLED, 6)


class TestRestartHydration:
    def _fill_event(self, intent_id: str) -> BrokerEvent:
        return BrokerEvent(
            kind=BrokerEventKind.FILLED,
            intent_id=intent_id,
            at_utc=NOW,
            cumulative_filled=10,
            average_fill_price=Decimal("500.00"),
            commission_usd=Decimal("1.00"),
            detail="post-restart fill",
        )

    def test_without_hydration_event_halts_unknown_order(self, tmp_path: Path) -> None:
        # Baseline: the documented pre-fix behaviour.
        intent = make_intent()
        ledger = MemoryLedger()
        ledger.record_intent(intent, IntentStatus.PENDING_SUBMISSION)
        ledger.record_transition(intent.intent_id, IntentStatus.SUBMITTED, NOW, "sent")
        ledger.record_transition(intent.intent_id, IntentStatus.ACKNOWLEDGED, NOW, "ack")
        store = armed_halt(tmp_path)
        engine = ExecutionEngine(
            broker=_OneEventBroker(self._fill_event(intent.intent_id)),
            risk_engine=RiskEngine(RiskPolicy()),
            ledger=ledger,
            halt_store=store,
            mode_lock=sim_lock(),
            reconciliation_passed=True,
        )
        engine.pump_events(now_utc=NOW)  # no hydration
        assert store.read().reason is HaltReason.UNKNOWN_ORDER

    def test_with_hydration_event_applies_and_records(self, tmp_path: Path) -> None:
        intent = make_intent()
        ledger = MemoryLedger()
        ledger.record_intent(intent, IntentStatus.PENDING_SUBMISSION)
        ledger.record_transition(intent.intent_id, IntentStatus.SUBMITTED, NOW, "sent")
        ledger.record_transition(intent.intent_id, IntentStatus.ACKNOWLEDGED, NOW, "ack")
        store = armed_halt(tmp_path)
        # Fresh engine (a "restart"): hydrate from the durable ledger first.
        engine = ExecutionEngine(
            broker=_OneEventBroker(self._fill_event(intent.intent_id)),
            risk_engine=RiskEngine(RiskPolicy()),
            ledger=ledger,
            halt_store=store,
            mode_lock=sim_lock(),
            reconciliation_passed=True,
        )
        hydrated = engine.hydrate_from_ledger(ledger.working_order_snapshots())
        assert hydrated == 1
        assert engine.order_status(intent.intent_id) is IntentStatus.ACKNOWLEDGED

        engine.pump_events(now_utc=NOW)

        assert not store.read().halted  # no UNKNOWN_ORDER halt
        assert engine.order_status(intent.intent_id) is IntentStatus.FILLED
        assert any(
            t.intent_id == intent.intent_id and t.status is IntentStatus.FILLED
            for t in ledger.transitions
        )
        assert any(f.intent_id == intent.intent_id for f in ledger.fills)
