"""Two-page Streamlit shell backed entirely by the deterministic broker."""

from __future__ import annotations

from datetime import UTC

import streamlit as st

from chronos.domain.enums import SecurityType
from chronos.ui.charts import quote_ladder_figure
from chronos.ui.components import format_money, render_runtime_status, render_safety_notice
from chronos.ui.session import AppRuntime


def render_dashboard(runtime: AppRuntime) -> None:
    status = runtime.connection.run(runtime.broker.connection_status())
    st.title("Chronos")
    st.caption("Local-first Wheel Strategy workspace")
    render_safety_notice()
    render_runtime_status(status, runtime.settings)

    page = st.sidebar.radio(
        "Workspace",
        ("Portfolio Dashboard", "Symbol Detail & Order Workspace"),
    )
    if page == "Portfolio Dashboard":
        _render_portfolio(runtime)
    else:
        _render_symbol_detail(runtime)


def _render_portfolio(runtime: AppRuntime) -> None:
    account = runtime.connection.run(runtime.broker.account_summary())
    positions = runtime.connection.run(runtime.broker.positions())
    open_orders = runtime.connection.run(runtime.broker.open_orders())

    st.header("Portfolio Dashboard")
    columns = st.columns(4)
    columns[0].metric("Net liquidation", format_money(account.net_liquidation))
    columns[1].metric("Cash", format_money(account.total_cash))
    columns[2].metric("Buying power", format_money(account.buying_power))
    columns[3].metric("Open Chronos orders", str(len(open_orders)))

    st.subheader("Broker positions")
    rows = [
        {
            "Symbol": position.contract.symbol,
            "Type": position.contract.security_type.value,
            "Quantity": str(position.quantity),
            "Broker average cost": format_money(position.average_cost),
            "Market price": format_money(position.market_price),
            "Unrealized P&L": format_money(position.unrealized_pnl),
        }
        for position in positions
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.caption(
        "Wheel stage and Strategy-Adjusted Basis — not tax basis remain locked until "
        "the reconciliation milestone publishes them."
    )

    st.subheader("Deterministic demo cases")
    st.dataframe(
        [
            {
                "Symbol": fixture.symbol,
                "Case": fixture.case.value,
                "Purpose": fixture.explanation,
            }
            for fixture in runtime.broker.fixture_cases
        ],
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("Near-Term / Weekly Focus")
    st.warning(
        "Candidate resolution is not enabled yet. Chronos will use contract metadata before "
        "calling an expiration weekly; until then this section says near-term expiration."
    )


def _render_symbol_detail(runtime: AppRuntime) -> None:
    st.header("Symbol Detail & Order Workspace")
    symbols = [fixture.symbol for fixture in runtime.broker.fixture_cases]
    symbol = st.selectbox("Demo symbol", symbols)
    underlying = runtime.connection.run(runtime.broker.qualify_underlying(symbol))
    quote = runtime.connection.run(runtime.broker.request_underlying_quote(underlying))
    chain = runtime.connection.run(runtime.broker.option_chain_parameters(underlying))[0]
    broker_time = runtime.connection.run(runtime.broker.server_time())

    st.subheader(symbol)
    columns = st.columns(4)
    columns[0].metric("Last", format_money(quote.last))
    columns[1].metric("Bid", format_money(quote.bid))
    columns[2].metric("Ask", format_money(quote.ask))
    columns[3].metric("Data quality", quote.data_quality.value)
    age_seconds = max(
        (broker_time.astimezone(UTC) - quote.timestamp).total_seconds(),
        0,
    )
    st.caption(f"Quote age: {age_seconds:.1f} seconds")
    st.plotly_chart(quote_ladder_figure(quote), use_container_width=True)

    matching_case = next(
        fixture for fixture in runtime.broker.fixture_cases if fixture.symbol == symbol
    )
    st.info(f"{matching_case.case.value}: {matching_case.explanation}")
    st.write(
        {
            "Trading class": chain.trading_class,
            "Multiplier": str(chain.multiplier),
            "Expirations": [expiration.isoformat() for expiration in chain.expirations],
            "Available strikes": [str(strike) for strike in chain.strikes],
        }
    )

    st.subheader("Candidate ranking")
    st.warning("Locked: strike resolution and reconciliation are not enabled in Milestone 1.")
    st.subheader("Scenario analysis")
    st.warning("Locked: scenario calculations are introduced with the tested strategy engine.")
    st.subheader("Order preview")
    st.error(
        "Locked: no order can be submitted from this milestone. "
        "DemoBroker rejects submission at its boundary."
    )
    if underlying.security_type is not SecurityType.STOCK:
        st.error("Unexpected demo contract type; order controls remain locked.")
