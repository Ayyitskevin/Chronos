"""Plotly figures for broker-neutral market data."""

from __future__ import annotations

import plotly.graph_objects as go

from chronos.domain.models import MarketQuote


def quote_ladder_figure(quote: MarketQuote) -> go.Figure:
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
            text=[f"${value:,.2f}" for value in values],
            textposition="auto",
            hovertemplate="%{y}: $%{x:.2f}<extra></extra>",
        )
    )
    figure.update_layout(
        title=f"{quote.contract.symbol} quote ladder",
        xaxis_title="Price",
        yaxis_title="Quote field",
        height=300,
        margin={"l": 20, "r": 20, "t": 50, "b": 20},
    )
    return figure
