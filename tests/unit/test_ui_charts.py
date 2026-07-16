from datetime import UTC, datetime
from decimal import Decimal

from chronos.domain.enums import DataQuality
from chronos.domain.models import MarketQuote, UnderlyingContract
from chronos.ui.charts import quote_ladder_figure


def test_quote_ladder_uses_explicit_contract_currency_without_dollar_symbol() -> None:
    quote = MarketQuote(
        contract=UnderlyingContract(con_id=1001, symbol="SAP", currency="EUR"),
        timestamp=datetime(2026, 7, 16, 15, 30, tzinfo=UTC),
        data_quality=DataQuality.LIVE,
        bid=Decimal("190.20"),
        ask=Decimal("190.30"),
        last=Decimal("190.25"),
    )

    figure = quote_ladder_figure(quote)
    trace = figure.data[0]

    assert list(trace.text) == ["190.20 EUR", "190.25 EUR", "190.30 EUR"]
    assert trace.hovertemplate == "%{y}: %{x:.2f} EUR<extra></extra>"
    assert figure.layout.xaxis.title.text == "Price (EUR)"
    assert "$" not in figure.to_json()
