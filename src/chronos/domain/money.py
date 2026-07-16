"""Decimal-only money and price helpers."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

MONEY_QUANTUM = Decimal("0.01")


def decimal_from(value: Decimal | int | str) -> Decimal:
    """Convert safe textual/integer input without introducing binary float error."""

    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money(value: Decimal | int | str) -> Decimal:
    """Round a monetary value to cents using conventional half-up rounding."""

    return decimal_from(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def round_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    """Round a limit price to the nearest valid positive minimum increment."""

    if increment <= 0:
        raise ValueError("Minimum price increment must be positive")
    units = (value / increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return units * increment
