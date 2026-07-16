"""Plotly figures for broker-neutral market data."""

from __future__ import annotations

from decimal import Decimal
from math import isfinite

import plotly.graph_objects as go

from chronos.domain.models import MarketQuote
from chronos.services.short_put_risk_preview import ShortPutRiskPreview


def quote_ladder_figure(quote: MarketQuote) -> go.Figure:
    currency = quote.contract.currency.strip().upper()
    labels: list[str] = []
    values: list[float] = []
    colors: list[str] = []
    for label, value, color in (
        ("Bid", quote.bid, "#d62728"),
        ("Last", quote.last, "#4c78a8"),
        ("Ask", quote.ask, "#2ca02c"),
    ):
        if value is not None:
            labels.append(label)
            values.append(float(value))
            colors.append(color)
    figure = go.Figure(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker_color=colors,
            text=[f"{value:,.2f} {currency}" for value in values],
            textposition="auto",
            hovertemplate=f"%{{y}}: %{{x:.2f}} {currency}<extra></extra>",
        )
    )
    figure.update_layout(
        title=f"{quote.contract.symbol} quote ladder",
        xaxis_title=f"Price ({currency})",
        yaxis_title="Quote field",
        height=300,
        margin={"l": 20, "r": 20, "t": 50, "b": 20},
    )
    return figure


def short_put_expiration_risk_figure(preview: ShortPutRiskPreview) -> go.Figure:
    """Render the bounded expiration payoff points without implying a price forecast."""

    currency = preview.currency
    points = sorted(preview.scenario_points, key=lambda point: point.underlying_price)
    prices = [_finite_plot_value(point.underlying_price) for point in points]
    pnl_values = [_finite_plot_value(point.expiration_pnl) for point in points]
    labels = [
        " / ".join(label.value.replace("_", " ").title() for label in point.labels)
        for point in points
    ]
    figure = go.Figure(
        go.Scatter(
            x=prices,
            y=pnl_values,
            mode="lines+markers",
            line={"color": "#4c78a8", "width": 3},
            marker={"color": "#4c78a8", "size": 9},
            customdata=labels,
            hovertemplate=(
                f"%{{customdata}}<br>Underlying / share: %{{x:.2f}} {currency}"
                f"<br>Total expiration P&L: %{{y:.2f}} {currency}<extra></extra>"
            ),
        )
    )
    figure.add_hline(y=0, line_dash="dash", line_color="#666666")
    figure.update_layout(
        title=f"{preview.symbol} short-put expiration payoff points",
        xaxis_title=f"Underlying price / share at expiration ({currency})",
        yaxis_title=f"Total modeled expiration P&L ({currency})",
        height=380,
        margin={"l": 20, "r": 20, "t": 50, "b": 20},
    )
    return figure


def _finite_plot_value(value: Decimal) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Risk-preview chart coordinates must be finite") from error
    if not isfinite(converted):
        raise ValueError("Risk-preview chart coordinates must be finite")
    return converted
