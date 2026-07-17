"""Order-intent identity: deterministic and collision-resistant.

The intent_id is the duplicate-suppression key, so a collision between two
economically-different intents would be a safety defect. These tests pin the
determinism property and the length-prefixing that defends against embedded
delimiters in the free-text fields.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from chronos.domain.enums import OrderSide
from chronos.execution.intents import OrderIntent, TimeInForce

NOW = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)


def intent(**overrides: object) -> OrderIntent:
    payload: dict[str, object] = {
        "strategy_id": "alpha",
        "strategy_version": "1",
        "symbol": "SPY",
        "side": OrderSide.BUY,
        "quantity": 10,
        "limit_price": Decimal("500.00"),
        "stop_price": Decimal("485.00"),
        "time_in_force": TimeInForce.DAY,
        "decision_timestamp_utc": NOW,
        "source_bar_sequence_id": "src:SPY:1d:2024-06-03",
        "proposal_reason": "test",
    }
    payload.update(overrides)
    return OrderIntent(**payload)  # type: ignore[arg-type]


def test_same_content_same_id() -> None:
    assert intent().intent_id == intent().intent_id


def test_different_quantity_different_id() -> None:
    assert intent(quantity=10).intent_id != intent(quantity=11).intent_id


def test_delimiter_injection_does_not_collide() -> None:
    # Before length-prefixing, ('alpha', 'x|y') and ('alpha|x', 'y') joined to
    # the same string. They must now differ.
    a = intent(strategy_id="alpha", strategy_version="x|y")
    b = intent(strategy_id="alpha|x", strategy_version="y")
    assert a.intent_id != b.intent_id


def test_symbol_bar_delimiter_injection_does_not_collide() -> None:
    a = intent(symbol="SP", source_bar_sequence_id="Y|rest")
    b = intent(symbol="SPY", source_bar_sequence_id="|rest")
    assert a.intent_id != b.intent_id


def test_stop_absent_vs_present_differ() -> None:
    assert intent(stop_price=None).intent_id != intent(stop_price=Decimal("485.00")).intent_id
