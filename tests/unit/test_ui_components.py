from decimal import Decimal

import pytest

from chronos.ui.components import format_money


def test_format_money_uses_explicit_iso_currency_without_dollar_assumption() -> None:
    rendered = format_money(Decimal("1234.5"), "eur")

    assert rendered == "1,234.50 EUR"
    assert "$" not in rendered


def test_format_money_preserves_missing_evidence_label() -> None:
    assert format_money(None, "EUR") == "Unavailable"


def test_format_money_rejects_blank_currency() -> None:
    with pytest.raises(ValueError, match="currency must not be blank"):
        format_money(Decimal("1"), "  ")
