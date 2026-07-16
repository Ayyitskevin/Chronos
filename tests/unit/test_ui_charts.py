from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from chronos.domain.enums import DataQuality, OptionRight
from chronos.domain.models import MarketQuote, OptionContract, UnderlyingContract
from chronos.services.short_put_risk_preview import (
    RiskScenarioLabel,
    ShortPutRiskPreview,
    ShortPutRiskScenarioPoint,
)
from chronos.ui.charts import quote_ladder_figure, short_put_expiration_risk_figure


def _risk_preview() -> ShortPutRiskPreview:
    contract = OptionContract(
        con_id=2001,
        symbol="SAP",
        underlying_con_id=1001,
        expiration=date(2026, 8, 21),
        strike=Decimal("180"),
        right=OptionRight.PUT,
        currency="EUR",
        multiplier=Decimal("100"),
        trading_class="SAP",
        local_symbol="SAP 260821P00180000",
        deliverable_shares=Decimal("100"),
        deliverable_verified=True,
    )
    return ShortPutRiskPreview(
        symbol="SAP",
        selected_contract=contract,
        candidate_rank=1,
        evidence_evaluated_at=datetime(2026, 7, 16, 15, 30, tzinfo=UTC),
        underlying_observed_at=datetime(2026, 7, 16, 15, 29, 59, tzinfo=UTC),
        option_data_quality=DataQuality.LIVE,
        option_quote_age_seconds=Decimal("1"),
        currency="EUR",
        observed_spot=Decimal("190"),
        hypothetical_credit_per_share=Decimal("2"),
        total_commission_estimate=Decimal("1"),
        fees_excluded=False,
        gross_premium=Decimal("200"),
        net_premium=Decimal("199"),
        assigned_shares=Decimal("100"),
        gross_assignment_obligation=Decimal("18000"),
        net_cash_deployed_if_assigned=Decimal("17801"),
        effective_entry_price=Decimal("178.01"),
        cash_secured_notional_pct=Decimal("0.18"),
        scenario_points=(
            ShortPutRiskScenarioPoint(
                labels=(RiskScenarioLabel.OBSERVED_SPOT,),
                underlying_price=Decimal("190"),
                put_intrinsic_value_per_share=Decimal("0"),
                expiration_pnl=Decimal("199"),
            ),
            ShortPutRiskScenarioPoint(
                labels=(RiskScenarioLabel.UNDERLYING_ZERO,),
                underlying_price=Decimal("0"),
                put_intrinsic_value_per_share=Decimal("180"),
                expiration_pnl=Decimal("-17801"),
            ),
        ),
        assumptions=("Expiration only.",),
    )


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


def test_expiration_risk_chart_sorts_points_and_labels_iso_currency() -> None:
    preview = _risk_preview()

    figure = short_put_expiration_risk_figure(preview)
    trace = figure.data[0]

    assert list(trace.x) == [0.0, 190.0]
    assert list(trace.y) == [-17801.0, 199.0]
    assert list(trace.customdata) == ["Underlying Zero", "Observed Spot"]
    assert "Underlying / share: %{x:.2f} EUR" in trace.hovertemplate
    assert "Total expiration P&L: %{y:.2f} EUR" in trace.hovertemplate
    assert figure.layout.xaxis.title.text == "Underlying price / share at expiration (EUR)"
    assert figure.layout.yaxis.title.text == "Total modeled expiration P&L (EUR)"
    assert "$" not in figure.to_json()


@pytest.mark.parametrize("coordinate", ["underlying_price", "expiration_pnl"])
def test_expiration_risk_chart_rejects_nonfinite_float_coordinates(
    coordinate: str,
) -> None:
    preview = _risk_preview()
    unsafe_point = preview.scenario_points[0].model_copy(update={coordinate: Decimal("1e10000")})
    unsafe_preview = preview.model_copy(update={"scenario_points": (unsafe_point,)})

    with pytest.raises(ValueError, match="finite"):
        short_put_expiration_risk_figure(unsafe_preview)
