"""Deterministic simulated broker: fill model, day-order expiry, faults, fees."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

from chronos.domain.enums import OrderSide
from chronos.execution.brokers.port import BrokerEventKind
from chronos.execution.brokers.simulated import FaultPlan, SimulatedExecutionBroker
from chronos.execution.intents import OrderIntent, TimeInForce
from chronos.marketdata.bars import Bar, BarInterval

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


def make_bar(
    session: date = date(2024, 6, 4),
    open_: float = 505.0,
    high: float = 506.0,
    low: float = 495.0,
    close: float = 500.0,
    symbol: str = "SPY",
) -> Bar:
    return Bar(
        symbol=symbol,
        source="test",
        exchange="ARCA",
        interval=BarInterval.DAY_1,
        session_date=session,
        timestamp_utc=datetime.combine(session, time(21, 0), tzinfo=UTC),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1_000_000.0,
    )


class TestBuyLimitFills:
    def test_fill_at_limit_when_open_above_limit(self) -> None:
        broker = SimulatedExecutionBroker()
        intent = make_intent()
        broker.submit(intent)
        ack = broker.drain_events()
        assert [e.kind for e in ack] == [BrokerEventKind.ACKNOWLEDGED]

        # low 495 <= limit 500.10 -> fill at min(open=505, limit=500.10) = limit
        broker.advance_bar(make_bar(open_=505.0, low=495.0))
        events = broker.drain_events()
        assert [e.kind for e in events] == [BrokerEventKind.FILLED]
        assert events[0].average_fill_price == Decimal("500.10")
        assert events[0].cumulative_filled == 5
        assert broker.open_order_intent_ids() == ()

    def test_fill_at_open_when_open_below_limit(self) -> None:
        broker = SimulatedExecutionBroker()
        broker.submit(make_intent())
        broker.drain_events()
        # open 495 < limit -> fill at min(open, limit) = 495.00
        broker.advance_bar(make_bar(open_=495.0, high=502.0, low=494.0, close=498.0))
        events = broker.drain_events()
        assert events[0].kind is BrokerEventKind.FILLED
        assert events[0].average_fill_price == Decimal("495.00")

    def test_unfilled_day_order_expires_cancelled(self) -> None:
        broker = SimulatedExecutionBroker()
        broker.submit(make_intent())
        broker.drain_events()
        # low 501 stays above limit 500.10 -> no fill -> day order expires
        broker.advance_bar(make_bar(open_=505.0, high=506.0, low=501.0, close=503.0))
        events = broker.drain_events()
        assert [e.kind for e in events] == [BrokerEventKind.CANCELLED]
        assert events[0].cumulative_filled == 0
        assert "expired" in events[0].detail
        assert broker.open_order_intent_ids() == ()

    def test_other_symbol_bar_leaves_order_working(self) -> None:
        broker = SimulatedExecutionBroker()
        intent = make_intent()
        broker.submit(intent)
        broker.drain_events()
        broker.advance_bar(make_bar(symbol="QQQ"))
        assert broker.drain_events() == ()
        assert broker.open_order_intent_ids() == (intent.intent_id,)


class TestSellLimitFills:
    def test_fill_at_open_when_open_above_limit(self) -> None:
        broker = SimulatedExecutionBroker()
        broker.submit(make_intent(side=OrderSide.SELL, stop_price=None))
        broker.drain_events()
        # high 506 >= limit 500.10 -> fill at max(open=505, limit) = 505.00
        broker.advance_bar(make_bar(open_=505.0, high=506.0, low=499.0))
        events = broker.drain_events()
        assert events[0].kind is BrokerEventKind.FILLED
        assert events[0].average_fill_price == Decimal("505.00")

    def test_fill_at_limit_when_open_below_limit(self) -> None:
        broker = SimulatedExecutionBroker()
        broker.submit(make_intent(side=OrderSide.SELL, stop_price=None))
        broker.drain_events()
        # open 498 < limit, high 502 trades through -> fill at limit 500.10
        broker.advance_bar(make_bar(open_=498.0, high=502.0, low=497.0, close=501.0))
        events = broker.drain_events()
        assert events[0].kind is BrokerEventKind.FILLED
        assert events[0].average_fill_price == Decimal("500.10")


class TestDuplicateSubmission:
    def test_duplicate_submit_is_broker_rejected(self) -> None:
        broker = SimulatedExecutionBroker()
        intent = make_intent()
        broker.submit(intent)
        broker.drain_events()
        broker.submit(intent)
        events = broker.drain_events()
        assert [e.kind for e in events] == [BrokerEventKind.REJECTED]
        assert events[0].detail == "duplicate intent id"
        # No second working order was created.
        assert broker.open_order_intent_ids() == (intent.intent_id,)


class TestFaultPlan:
    def test_injected_rejection(self) -> None:
        intent = make_intent()
        broker = SimulatedExecutionBroker(
            faults=FaultPlan(reject_intent_ids=frozenset({intent.intent_id}))
        )
        broker.submit(intent)
        events = broker.drain_events()
        assert [e.kind for e in events] == [BrokerEventKind.REJECTED]
        assert events[0].detail == "injected rejection"
        assert broker.open_order_intent_ids() == ()

    def test_partial_fill_then_completion(self) -> None:
        intent = make_intent(quantity=5)
        broker = SimulatedExecutionBroker(
            faults=FaultPlan(partial_fill_intent_ids=frozenset({intent.intent_id}))
        )
        broker.submit(intent)
        broker.drain_events()

        broker.advance_bar(make_bar())
        first = broker.drain_events()
        # partial = max(1, 5 * 1 // 2) = 2 shares, order remains working
        assert [e.kind for e in first] == [BrokerEventKind.PARTIAL_FILL]
        assert first[0].cumulative_filled == 2
        assert broker.open_order_intent_ids() == (intent.intent_id,)

        broker.advance_bar(make_bar(session=date(2024, 6, 5)))
        second = broker.drain_events()
        assert [e.kind for e in second] == [BrokerEventKind.FILLED]
        assert second[0].cumulative_filled == 5
        assert broker.open_order_intent_ids() == ()

    def test_duplicate_events_fault_doubles_emission(self) -> None:
        intent = make_intent()
        broker = SimulatedExecutionBroker(
            faults=FaultPlan(duplicate_events_intent_ids=frozenset({intent.intent_id}))
        )
        broker.submit(intent)
        acks = broker.drain_events()
        assert [e.kind for e in acks] == [
            BrokerEventKind.ACKNOWLEDGED,
            BrokerEventKind.ACKNOWLEDGED,
        ]
        assert acks[0] == acks[1]

        broker.advance_bar(make_bar())
        fills = broker.drain_events()
        assert [e.kind for e in fills] == [BrokerEventKind.FILLED, BrokerEventKind.FILLED]

    def test_drop_ack_still_fills(self) -> None:
        intent = make_intent()
        broker = SimulatedExecutionBroker(
            faults=FaultPlan(drop_ack_intent_ids=frozenset({intent.intent_id}))
        )
        broker.submit(intent)
        assert broker.drain_events() == ()  # silent acceptance
        broker.advance_bar(make_bar())
        events = broker.drain_events()
        assert [e.kind for e in events] == [BrokerEventKind.FILLED]


class TestCommission:
    def test_minimum_commission_applies_to_small_orders(self) -> None:
        broker = SimulatedExecutionBroker()
        broker.submit(make_intent(quantity=5))
        broker.drain_events()
        broker.advance_bar(make_bar())
        events = broker.drain_events()
        # 5 shares * $0.005 = $0.025 -> below the $1.00 minimum
        assert events[0].commission_usd == Decimal("1.00")

    def test_per_share_commission_above_minimum(self) -> None:
        broker = SimulatedExecutionBroker()
        broker.submit(make_intent(quantity=300))
        broker.drain_events()
        broker.advance_bar(make_bar())
        events = broker.drain_events()
        # 300 shares * $0.005 = $1.50 > $1.00 minimum
        assert events[0].commission_usd == Decimal("1.500")

    def test_custom_commission_schedule(self) -> None:
        broker = SimulatedExecutionBroker(
            commission_per_share=Decimal("0.01"), commission_minimum=Decimal("2.00")
        )
        broker.submit(make_intent(quantity=100))
        broker.drain_events()
        broker.advance_bar(make_bar())
        events = broker.drain_events()
        # 100 * $0.01 = $1.00 -> below the $2.00 minimum
        assert events[0].commission_usd == Decimal("2.00")
