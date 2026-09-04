"""Every order is a positive-price limit — bound to tests, not only to the models.

README's Safety posture lists this as **[enforced]**: "Every order is a positive-price
limit — including 'market' orders." The guards are real at runtime — ``Field(gt=0)`` on
``BrokerOrder.limit_price`` and ``OrderRequest.limit_price`` in ``chronos.domain.models``,
and ``WheelOrderIntent.validate_limit_price`` in ``chronos.orders.intent`` — but until this
file no test died when any of them was deleted (team review, 2026-09-03: both ``gt=0``
constraints removed and the intent validator neutered, 4,500+ tests stayed green). A guard
on the price field of a live order that no test binds is an asserted guard, not an
enforced one. Each test below fails when exactly its guard is removed.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from chronos.domain.enums import OptionRight, OrderIntent, OrderLifecycle, OrderSide
from chronos.domain.models import BrokerOrder, OptionContract, OrderRequest
from chronos.orders.intent import WheelOrderIntent, build_option_intent

_ACCOUNT_ID = "DU1234567"

NON_POSITIVE_PRICES = (Decimal("0"), Decimal("-0.01"), Decimal("-3.20"))


def _put_contract() -> OptionContract:
    return OptionContract(
        con_id=2001,
        symbol="AAPL",
        underlying_con_id=1001,
        expiration=date(2026, 2, 20),
        strike=Decimal("180"),
        right=OptionRight.PUT,
        multiplier=Decimal("100"),
        trading_class="AAPL",
        local_symbol="AAPL 260220P00180000",
        deliverable_shares=Decimal("100"),
        deliverable_verified=True,
    )


def _broker_order(limit_price: Decimal) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=7001,
        permanent_id=9001,
        client_id=17,
        account_id=_ACCOUNT_ID,
        order_ref="CHR-AAPL-PUT",
        contract=_put_contract(),
        side=OrderSide.SELL,
        quantity=Decimal("1"),
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal("1"),
        limit_price=limit_price,
        lifecycle=OrderLifecycle.SUBMITTED,
    )


def _order_request(limit_price: Decimal) -> OrderRequest:
    return OrderRequest(
        correlation_id="CHR-TEST-001",
        account_id=_ACCOUNT_ID,
        contract=_put_contract(),
        intent=OrderIntent.OPEN_SHORT_PUT,
        side=OrderSide.SELL,
        quantity=1,
        limit_price=limit_price,
        order_ref="CHR-TEST-001",
    )


def _intent(limit_price: Decimal) -> WheelOrderIntent:
    return build_option_intent(
        account_id=_ACCOUNT_ID,
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=_put_contract(),
        quantity=1,
        limit_price=limit_price,
    )


@pytest.mark.parametrize("limit_price", NON_POSITIVE_PRICES, ids=str)
def test_a_broker_order_cannot_carry_a_non_positive_limit_price(limit_price: Decimal) -> None:
    with pytest.raises(ValidationError, match="limit_price"):
        _broker_order(limit_price)


@pytest.mark.parametrize("limit_price", NON_POSITIVE_PRICES, ids=str)
def test_an_order_request_cannot_carry_a_non_positive_limit_price(limit_price: Decimal) -> None:
    with pytest.raises(ValidationError, match="limit_price"):
        _order_request(limit_price)


@pytest.mark.parametrize("limit_price", NON_POSITIVE_PRICES, ids=str)
def test_an_intent_cannot_carry_a_non_positive_limit_price(limit_price: Decimal) -> None:
    with pytest.raises(ValidationError, match="market orders are impossible"):
        _intent(limit_price)


def test_a_positive_limit_price_is_the_only_shape_all_three_accept() -> None:
    assert _broker_order(Decimal("2.50")).limit_price == Decimal("2.50")
    assert _order_request(Decimal("3.20")).limit_price == Decimal("3.20")
    assert _intent(Decimal("3.20")).limit_price == Decimal("3.20")
