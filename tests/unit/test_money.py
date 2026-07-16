from decimal import Decimal

import pytest

from chronos.domain.money import money, round_to_increment


def test_money_uses_decimal_half_up_rounding() -> None:
    assert money("1.005") == Decimal("1.01")


@pytest.mark.parametrize(
    ("value", "increment", "expected"),
    [
        ("1.02", "0.05", "1.00"),
        ("1.03", "0.05", "1.05"),
        ("3.125", "0.05", "3.15"),
        ("0.999", "0.01", "1.00"),
    ],
)
def test_minimum_tick_rounding(value: str, increment: str, expected: str) -> None:
    assert round_to_increment(Decimal(value), Decimal(increment)) == Decimal(expected)


def test_minimum_tick_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        round_to_increment(Decimal("1.00"), Decimal("0"))
