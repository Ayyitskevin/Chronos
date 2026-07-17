"""Broker-event application guards (M5 remediation).

Two contradiction classes were being under-defended relative to the symmetric
paths:

- a FILLED event reporting a *lower* cumulative fill than already seen was
  silently accepted (fill count rewound), where the partial-fill path halts;
- two halt paths (unknown-order, illegal-transition-from-terminal) left
  ``reconciliation_passed`` True, so a rearm-without-restart could resume
  trading on evidence that was dropped.

These tests pin the fixed behaviour.
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
from chronos.risk.engine import RiskEngine
from chronos.risk.policy import RiskPolicy

NOW = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)


class _OneEventBroker:
    def __init__(self, event: BrokerEvent) -> None:
        self._event = event
        self._drained = False

    def submit(self, intent: OrderIntent):  # pragma: no cover - unused
        raise AssertionError("not used")

    def request_cancel(self, intent_id: str) -> None:  # pragma: no cover
        pass

    def drain_events(self) -> tuple[BrokerEvent, ...]:
        if self._drained:
            return ()
        self._drained = True
        return (self._event,)


def _sim_lock():
    return resolve_mode_lock(
        requested_mode=TradingMode.BACKTEST,
        paper_account_allowlist=(),
        broker_reported_account_id=None,
        broker_reported_environment_is_paper=None,
        order_transmission_enabled=False,
    )


def _armed(tmp_path: Path) -> HaltStore:
    store = HaltStore(tmp_path / "halt.json")
    store.rearm("armed")
    return store


def _intent() -> OrderIntent:
    return OrderIntent(
        strategy_id="s",
        strategy_version="1",
        symbol="SPY",
        side=OrderSide.BUY,
        quantity=10,
        limit_price=Decimal("500.00"),
        stop_price=None,
        time_in_force=TimeInForce.DAY,
        decision_timestamp_utc=NOW,
        source_bar_sequence_id="test:SPY:1d:2024-06-03",
        proposal_reason="test",
    )


def _engine(tmp_path: Path, event: BrokerEvent) -> ExecutionEngine:
    return ExecutionEngine(
        broker=_OneEventBroker(event),
        risk_engine=RiskEngine(RiskPolicy()),
        ledger=MemoryLedger(),
        halt_store=_armed(tmp_path),
        mode_lock=_sim_lock(),
        reconciliation_passed=True,
    )


def _event(kind: BrokerEventKind, intent_id: str, cumulative: int) -> BrokerEvent:
    return BrokerEvent(
        kind=kind,
        intent_id=intent_id,
        at_utc=NOW,
        cumulative_filled=cumulative,
        average_fill_price=Decimal("500.00"),
        commission_usd=Decimal("1.00"),
        detail="test event",
    )


def test_filled_with_decreasing_cumulative_halts(tmp_path: Path) -> None:
    intent = _intent()
    engine = _engine(tmp_path, _event(BrokerEventKind.FILLED, intent.intent_id, cumulative=5))
    # Order is already partially filled to 8; a FILLED reporting only 5 is a
    # monotonic-fill contradiction and must halt, not silently rewind.
    engine.hydrate_from_ledger({intent.intent_id: (IntentStatus.PARTIALLY_FILLED, 8)})
    engine.pump_events(now_utc=NOW)
    assert engine.halt_store.read().reason is HaltReason.STATE_CORRUPTION
    assert engine.reconciliation_passed is False


def test_normal_filled_sets_quantity(tmp_path: Path) -> None:
    intent = _intent()
    engine = _engine(tmp_path, _event(BrokerEventKind.FILLED, intent.intent_id, cumulative=10))
    engine.hydrate_from_ledger({intent.intent_id: (IntentStatus.ACKNOWLEDGED, 0)})
    engine.pump_events(now_utc=NOW)
    assert engine.order_status(intent.intent_id) is IntentStatus.FILLED
    assert not engine.halt_store.read().trading_blocked


def test_unknown_order_event_clears_reconciliation(tmp_path: Path) -> None:
    engine = _engine(tmp_path, _event(BrokerEventKind.FILLED, "no-such-intent", cumulative=10))
    # No hydration: the event references an order the engine never saw.
    engine.pump_events(now_utc=NOW)
    assert engine.halt_store.read().reason is HaltReason.UNKNOWN_ORDER
    assert engine.reconciliation_passed is False


def test_illegal_transition_from_terminal_clears_reconciliation(tmp_path: Path) -> None:
    intent = _intent()
    # An ACKNOWLEDGED event arriving for an order already FILLED (terminal) is
    # illegal; the recovery apply() is itself illegal from terminal, so the
    # reconciliation flag must be cleared regardless.
    engine = _engine(
        tmp_path, _event(BrokerEventKind.ACKNOWLEDGED, intent.intent_id, cumulative=10)
    )
    engine.hydrate_from_ledger({intent.intent_id: (IntentStatus.FILLED, 10)})
    engine.pump_events(now_utc=NOW)
    assert engine.halt_store.read().reason is HaltReason.STATE_CORRUPTION
    assert engine.reconciliation_passed is False
