"""M7C commit 3: crypto evidence gathering + decontamination proof (ADR-0010 §4).

Proves the crypto aggregate helpers compute correctly AND that a held crypto
position never leaks into the stock/wheel aggregates (panel ada-6) — the
change that protects existing option/stock risk math from cross-contamination.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from chronos.domain.enums import OrderLifecycle, OrderSide
from chronos.domain.models import (
    BrokerOrder,
    BrokerPosition,
    CryptoContract,
    UnderlyingContract,
)
from chronos.orders.evidence import (
    _crypto_allocation,
    _crypto_sell_reserved,
    _held_crypto,
    _long_shares,
    _pending_crypto_buy_notional,
    _symbol_allocation,
    _total_allocation,
)

_NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
_ACCT = "U7654321"


def _crypto_pos(symbol: str, qty: str, mark: str | None) -> BrokerPosition:
    return BrokerPosition(
        account_id=_ACCT,
        contract=CryptoContract(con_id=hash(symbol) % 10_000_000 + 1, symbol=symbol),
        quantity=Decimal(qty),
        average_cost=Decimal("60000"),
        market_price=Decimal(mark) if mark is not None else None,
    )


def _stock_pos(symbol: str, qty: str) -> BrokerPosition:
    return BrokerPosition(
        account_id=_ACCT,
        contract=UnderlyingContract(con_id=999, symbol=symbol),
        quantity=Decimal(qty),
        average_cost=Decimal("100"),
        market_price=Decimal("110"),
    )


def _crypto_order(symbol: str, side: OrderSide, remaining: str, price: str) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=1,
        client_id=17,
        account_id=_ACCT,
        contract=CryptoContract(con_id=1, symbol=symbol),
        side=side,
        quantity=Decimal(remaining),
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal(remaining),
        limit_price=Decimal(price),
        lifecycle=OrderLifecycle.SUBMITTED,
    )


def test_held_crypto_sums_only_matching_symbol_longs() -> None:
    positions = (
        _crypto_pos("BTC", "0.5", "64000"),
        _crypto_pos("BTC", "0.3", "64000"),
        _crypto_pos("ETH", "2.0", "3000"),
    )
    assert _held_crypto(positions, "BTC") == Decimal("0.8")


def test_crypto_allocation_marks_all_and_sums_market_value() -> None:
    positions = (
        _crypto_pos("BTC", "0.5", "64000"),  # 32000
        _crypto_pos("ETH", "2.0", "3000"),  # 6000
    )
    value, marked = _crypto_allocation(positions)
    assert value == Decimal("38000")
    assert marked is True


def test_crypto_allocation_unmarked_when_any_holding_lacks_price() -> None:
    positions = (
        _crypto_pos("BTC", "0.5", "64000"),
        _crypto_pos("ETH", "2.0", None),  # no mark
    )
    _value, marked = _crypto_allocation(positions)
    assert marked is False


def test_crypto_allocation_unmarked_when_a_holding_is_marked_non_positive() -> None:
    # Adversarial-review F5: a 0 (or negative) mark is anomalous broker data and
    # must fail UNKNOWN, not silently value the holding at zero and let an
    # over-allocated BUY slip past the allocation cap.
    positions = (
        _crypto_pos("BTC", "0.5", "64000"),
        _crypto_pos("ETH", "2.0", "0"),  # zero mark
    )
    _value, marked = _crypto_allocation(positions)
    assert marked is False


def test_pending_crypto_buy_notional_counts_only_buys() -> None:
    orders = (
        _crypto_order("BTC", OrderSide.BUY, "0.01", "64000"),  # 640
        _crypto_order("BTC", OrderSide.SELL, "0.02", "64000"),  # ignored
    )
    assert _pending_crypto_buy_notional(orders) == Decimal("640.00")


def test_crypto_sell_reserved_counts_only_sells_for_symbol() -> None:
    orders = (
        _crypto_order("BTC", OrderSide.SELL, "0.30", "64000"),
        _crypto_order("BTC", OrderSide.BUY, "0.10", "64000"),  # ignored
        _crypto_order("ETH", OrderSide.SELL, "1.00", "3000"),  # ignored
    )
    assert _crypto_sell_reserved(orders, "BTC") == Decimal("0.30")


# --- decontamination: crypto must NEVER leak into the wheel/stock aggregates


def test_crypto_holding_does_not_count_as_long_shares() -> None:
    positions = (_crypto_pos("BTC", "0.5", "64000"), _stock_pos("BTC", "10"))
    # Only the (nonsensical but adversarial) stock BTC counts as long shares;
    # the crypto BTC holding must not.
    assert _long_shares(positions, "BTC") == Decimal("10")


def test_crypto_holding_does_not_enter_symbol_or_total_allocation() -> None:
    positions = (_crypto_pos("BTC", "0.5", "64000"),)
    assert _symbol_allocation(positions, "BTC") == Decimal("0")
    assert _total_allocation(positions) == Decimal("0")
