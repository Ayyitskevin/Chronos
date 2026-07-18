"""Golden hash/idempotency stability for the M7C quantity type change (ADR-0010 §1).

The values below were captured from the PRE-M7C implementation (integer
``PositiveInt`` quantities, commit 677595c) and are pinned byte-for-byte: the
fractional-quantity widening must never change the idempotency key or the
confirmation summary hash of an existing OPTION or STOCK intent. A drift here
would break duplicate suppression for orders proposed before the change, or
produce a CONFIRMATION_MISMATCH between a confirm recorded before a deploy and
a submit re-derivation after it.

If one of these assertions ever fails, the change that broke it is wrong — do
NOT update the constants.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from chronos.domain.enums import OptionRight, OrderIntent
from chronos.domain.models import OptionContract, UnderlyingContract
from chronos.orders.intent import (
    build_option_intent,
    build_stock_intent,
    order_summary_hash,
)

_GOLDEN_DAY_BUCKET = "2026-07-17"

_GOLDEN_OPTION_IDEMPOTENCY = "dbce8baf54d5545b8692bb4826c5da6c"
_GOLDEN_OPTION_SUMMARY_HASH = "f208637726e0add78fcdd87b8874f2b4eff59ea6b7eb3e4d4698831fb830e7f4"
_GOLDEN_STOCK_IDEMPOTENCY = "10433b465737539f9d00b78ab102ad9a"
_GOLDEN_STOCK_SUMMARY_HASH = "c04f70f27764cd59ea030fdb1233f1b247ee6dfef03c74e0c9637a312e7348fc"


def _golden_option_intent(quantity: object = 1) -> object:
    contract = OptionContract(
        symbol="AAPL",
        underlying_con_id=999,
        expiration=date(2026, 8, 21),
        strike=Decimal("150"),
        right=OptionRight.PUT,
        multiplier=Decimal("100"),
        trading_class="AAPL",
        con_id=111,
        local_symbol="AAPL 260821P00150000",
        deliverable_shares=Decimal("100"),
        deliverable_verified=True,
    )
    return build_option_intent(
        account_id="DU1234567",
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=contract,
        quantity=quantity,  # type: ignore[arg-type]
        limit_price=Decimal("1.20"),
        correlation_id="CHR-ORD-" + "A" * 32,
        intent_id="golden-opt",
    )


def _golden_stock_intent(quantity: object = 25) -> object:
    return build_stock_intent(
        account_id="DU1234567",
        intent=OrderIntent.OPEN_LONG_STOCK,
        contract=UnderlyingContract(con_id=999, symbol="AAPL"),
        quantity=quantity,  # type: ignore[arg-type]
        limit_price=Decimal("189.50"),
        correlation_id="CHR-ORD-" + "B" * 32,
        intent_id="golden-stk",
    )


def test_option_keys_and_hashes_are_byte_stable() -> None:
    intent = _golden_option_intent()
    assert intent.idempotency_key(day_bucket=_GOLDEN_DAY_BUCKET) == _GOLDEN_OPTION_IDEMPOTENCY  # type: ignore[attr-defined]
    assert (
        order_summary_hash(intent, risk_decision_id="RD-GOLDEN-1")  # type: ignore[arg-type]
        == _GOLDEN_OPTION_SUMMARY_HASH
    )


def test_stock_keys_and_hashes_are_byte_stable() -> None:
    intent = _golden_stock_intent()
    assert intent.idempotency_key(day_bucket=_GOLDEN_DAY_BUCKET) == _GOLDEN_STOCK_IDEMPOTENCY  # type: ignore[attr-defined]
    assert (
        order_summary_hash(intent, risk_decision_id="RD-GOLDEN-2")  # type: ignore[arg-type]
        == _GOLDEN_STOCK_SUMMARY_HASH
    )


def test_decimal_exponent_variants_cannot_fork_keys() -> None:
    """After the M7C widening, economically-identical Decimal spellings of an
    integral quantity must produce the SAME key/hash as the golden int form.

    Pre-M7C this test passes trivially (the builders coerce to int); post-M7C
    it is the anti-fork guarantee for Decimal('1') / Decimal('1.0') / 1.
    """

    for quantity in (1, Decimal("1"), Decimal("1.0"), Decimal("1.00")):
        intent = _golden_option_intent(quantity)
        assert (
            intent.idempotency_key(day_bucket=_GOLDEN_DAY_BUCKET)  # type: ignore[attr-defined]
            == _GOLDEN_OPTION_IDEMPOTENCY
        ), f"idempotency key forked for quantity spelling {quantity!r}"
        assert (
            order_summary_hash(intent, risk_decision_id="RD-GOLDEN-1")  # type: ignore[arg-type]
            == _GOLDEN_OPTION_SUMMARY_HASH
        ), f"summary hash forked for quantity spelling {quantity!r}"
