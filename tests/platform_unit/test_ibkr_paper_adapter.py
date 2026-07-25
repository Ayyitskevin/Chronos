"""Tests for the IBKR paper execution adapter against a fake IB object.

These exercise the adapter's construction gating, per-submission account
re-verification, the disconnect race between verification and placement, and
the fill-event translation edge cases surfaced by independent review
(status/filled inconsistency must never lose or fabricate fills). No real
gateway or credentials are involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from chronos.broker.base import BrokerSafetyError
from chronos.control.modes import TradingMode, resolve_mode_lock
from chronos.domain.enums import OrderSide
from chronos.execution.brokers.ibkr_paper import IBKRPaperExecutionAdapter
from chronos.execution.brokers.port import BrokerEventKind
from chronos.execution.intents import OrderIntent, TimeInForce

NOW = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)


def paper_lock(account: str = "DU1234567"):
    return resolve_mode_lock(
        requested_mode=TradingMode.PAPER,
        paper_account_allowlist=(account,),
        broker_reported_account_id=account,
        broker_reported_environment_is_paper=True,
        order_transmission_enabled=True,
    )


def make_intent(**overrides: object) -> OrderIntent:
    payload: dict[str, object] = {
        "strategy_id": "strat_a",
        "strategy_version": "1",
        "symbol": "SPY",
        "side": OrderSide.BUY,
        "quantity": 10,
        "limit_price": Decimal("500.00"),
        "stop_price": Decimal("485.00"),
        "time_in_force": TimeInForce.DAY,
        "decision_timestamp_utc": NOW,
        "source_bar_sequence_id": "test:SPY:1d:2024-06-03",
        "proposal_reason": "test",
    }
    payload.update(overrides)
    return OrderIntent(**payload)  # type: ignore[arg-type]


@dataclass
class FakeOrderStatus:
    status: str = "PendingSubmit"
    filled: float = 0.0
    avgFillPrice: float = 0.0


@dataclass
class FakeOrder:
    totalQuantity: int = 10
    account: str = ""
    tif: str = ""
    outsideRth: bool = True
    orderRef: str = ""
    transmit: bool = False


@dataclass
class FakeTrade:
    order: FakeOrder
    orderStatus: FakeOrderStatus = field(default_factory=FakeOrderStatus)
    fills: list[Any] = field(default_factory=list)


@dataclass
class FakeIB:
    connected: bool = True
    accounts: list[str] = field(default_factory=lambda: ["DU1234567"])
    connected_sequence: list[bool] | None = None
    placed: list[FakeTrade] = field(default_factory=list)
    cancelled: list[Any] = field(default_factory=list)

    def isConnected(self) -> bool:
        if self.connected_sequence:
            return self.connected_sequence.pop(0)
        return self.connected

    def managedAccounts(self) -> list[str]:
        return self.accounts

    def placeOrder(self, contract: Any, order: Any) -> FakeTrade:
        trade = FakeTrade(order=order, orderStatus=FakeOrderStatus())
        self.placed.append(trade)
        return trade

    def cancelOrder(self, order: Any) -> None:
        self.cancelled.append(order)

    def waitOnUpdate(self, timeout: float = 0.0) -> bool:
        return True


class TestConstruction:
    def test_requires_paper_submission_lock(self) -> None:
        shadow = resolve_mode_lock(
            requested_mode=TradingMode.SHADOW,
            paper_account_allowlist=(),
            broker_reported_account_id=None,
            broker_reported_environment_is_paper=None,
            order_transmission_enabled=False,
        )
        with pytest.raises(BrokerSafetyError):
            IBKRPaperExecutionAdapter(quarantine_ack=True, ib=FakeIB(), mode_lock=shadow, port=7497)

    def test_rejects_non_paper_port(self) -> None:
        with pytest.raises(BrokerSafetyError):
            IBKRPaperExecutionAdapter(
                quarantine_ack=True, ib=FakeIB(), mode_lock=paper_lock(), port=7496
            )

    def test_accepts_paper_ports(self) -> None:
        for port in (7497, 4002):
            IBKRPaperExecutionAdapter(
                quarantine_ack=True, ib=FakeIB(), mode_lock=paper_lock(), port=port
            )


class TestAccountVerification:
    def test_exact_account_match_required(self) -> None:
        ib = FakeIB(accounts=["DU1234567", "DU9999999"])
        adapter = IBKRPaperExecutionAdapter(
            quarantine_ack=True, ib=ib, mode_lock=paper_lock(), port=7497
        )
        with pytest.raises(BrokerSafetyError):
            adapter.verify_account()

    def test_superstring_account_rejected(self) -> None:
        ib = FakeIB(accounts=["DU12345678"])
        adapter = IBKRPaperExecutionAdapter(
            quarantine_ack=True, ib=ib, mode_lock=paper_lock(), port=7497
        )
        with pytest.raises(BrokerSafetyError):
            adapter.verify_account()

    def test_disconnected_rejected(self) -> None:
        ib = FakeIB(connected=False)
        adapter = IBKRPaperExecutionAdapter(
            quarantine_ack=True, ib=ib, mode_lock=paper_lock(), port=7497
        )
        with pytest.raises(BrokerSafetyError):
            adapter.verify_account()


class TestSubmit:
    def test_submit_sets_order_fields(self) -> None:
        ib = FakeIB()
        adapter = IBKRPaperExecutionAdapter(
            quarantine_ack=True, ib=ib, mode_lock=paper_lock(), port=7497
        )
        intent = make_intent()
        adapter.submit(intent)
        assert len(ib.placed) == 1
        order = ib.placed[0].order
        assert order.tif == "DAY"
        assert order.outsideRth is False
        assert order.orderRef == intent.intent_id
        assert order.account == "DU1234567"

    def test_duplicate_submit_rejected(self) -> None:
        ib = FakeIB()
        adapter = IBKRPaperExecutionAdapter(
            quarantine_ack=True, ib=ib, mode_lock=paper_lock(), port=7497
        )
        intent = make_intent()
        adapter.submit(intent)
        with pytest.raises(BrokerSafetyError):
            adapter.submit(intent)

    def test_disconnect_between_verify_and_place_blocks(self) -> None:
        # verify_account() sees connected=True (first two reads), then the
        # connection drops before placeOrder's own isConnected() check.
        ib = FakeIB(connected_sequence=[True, False])
        adapter = IBKRPaperExecutionAdapter(
            quarantine_ack=True, ib=ib, mode_lock=paper_lock(), port=7497
        )
        with pytest.raises(BrokerSafetyError):
            adapter.submit(make_intent())
        assert ib.placed == []  # order never reached the broker


class TestDrainEvents:
    def _adapter_with_trade(
        self, status: str, filled: float, total: int = 10
    ) -> tuple[IBKRPaperExecutionAdapter, FakeIB]:
        ib = FakeIB()
        adapter = IBKRPaperExecutionAdapter(
            quarantine_ack=True, ib=ib, mode_lock=paper_lock(), port=7497
        )
        adapter.submit(make_intent(quantity=total))
        trade = ib.placed[0]
        trade.order.totalQuantity = total
        trade.orderStatus = FakeOrderStatus(status=status, filled=filled, avgFillPrice=500.0)
        return adapter, ib

    def test_filled_status_with_partial_quantity_is_partial_fill(self) -> None:
        # Status says Filled but only 4 of 10 filled: quantity is authoritative.
        adapter, _ = self._adapter_with_trade("Filled", filled=4, total=10)
        events = [e for e in adapter.drain_events() if e.kind is not BrokerEventKind.ACKNOWLEDGED]
        assert len(events) == 1
        assert events[0].kind is BrokerEventKind.PARTIAL_FILL
        assert events[0].cumulative_filled == 4

    def test_full_quantity_under_ack_status_is_filled(self) -> None:
        # Status still Submitted but filled == total: must report FILLED.
        adapter, _ = self._adapter_with_trade("Submitted", filled=10, total=10)
        events = adapter.drain_events()
        assert len(events) == 1
        assert events[0].kind is BrokerEventKind.FILLED
        assert events[0].cumulative_filled == 10

    def test_clean_filled(self) -> None:
        adapter, _ = self._adapter_with_trade("Filled", filled=10, total=10)
        events = adapter.drain_events()
        assert events[0].kind is BrokerEventKind.FILLED

    def test_ack_with_zero_fill(self) -> None:
        adapter, _ = self._adapter_with_trade("Submitted", filled=0, total=10)
        events = adapter.drain_events()
        assert events[0].kind is BrokerEventKind.ACKNOWLEDGED

    def test_unknown_status_surfaces_error_not_silence(self) -> None:
        adapter, _ = self._adapter_with_trade("SomeNewIBStatus", filled=0, total=10)
        events = adapter.drain_events()
        assert len(events) == 1
        assert events[0].kind is BrokerEventKind.ERROR

    def test_pending_submit_zero_fill_is_silent(self) -> None:
        adapter, _ = self._adapter_with_trade("PendingSubmit", filled=0, total=10)
        assert adapter.drain_events() == ()

    def test_rejected_status(self) -> None:
        adapter, _ = self._adapter_with_trade("Inactive", filled=0, total=10)
        events = adapter.drain_events()
        assert events[0].kind is BrokerEventKind.REJECTED

    def test_cancelled_status(self) -> None:
        adapter, _ = self._adapter_with_trade("Cancelled", filled=0, total=10)
        events = adapter.drain_events()
        assert events[0].kind is BrokerEventKind.CANCELLED

    def test_duplicate_state_deduped(self) -> None:
        adapter, _ib = self._adapter_with_trade("Filled", filled=10, total=10)
        first = adapter.drain_events()
        second = adapter.drain_events()
        assert len(first) == 1
        assert second == ()  # same (status, filled) not re-emitted
