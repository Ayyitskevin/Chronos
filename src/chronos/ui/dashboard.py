"""Two-page Streamlit shell backed by the configured broker boundary."""

from __future__ import annotations

import logging

import streamlit as st

from chronos.broker.base import BrokerDataError, BrokerError
from chronos.broker.demo import DemoBroker
from chronos.ui.charts import quote_ladder_figure
from chronos.ui.components import format_money, render_runtime_status, render_safety_notice
from chronos.ui.session import AppRuntime


def render_dashboard(runtime: AppRuntime) -> None:
    st.title("Chronos")
    st.caption("Local-first Wheel Strategy workspace")
    render_safety_notice()
    try:
        status = runtime.connection.run(runtime.broker.connection_status())
        render_runtime_status(status, runtime.settings)
        page = st.sidebar.radio(
            "Workspace",
            ("Portfolio Dashboard", "Symbol Detail & Order Workspace"),
        )
        if page == "Portfolio Dashboard":
            _render_portfolio(runtime)
        else:
            _render_symbol_detail(runtime)
    except BrokerError as error:
        logging.getLogger("chronos.ui.dashboard").exception(
            "Broker operation failed",
            extra={"event": "broker_ui_operation_failed"},
        )
        st.error(str(error))
    except Exception:
        logging.getLogger("chronos.ui.dashboard").exception(
            "Unexpected dashboard operation failure",
            extra={"event": "dashboard_operation_failed"},
        )
        st.error("The dashboard could not complete this request safely. See the local log.")


def _render_portfolio(runtime: AppRuntime) -> None:
    account = runtime.connection.run(runtime.broker.account_summary())
    positions = runtime.connection.run(runtime.broker.positions())
    open_orders = runtime.connection.run(runtime.broker.open_orders())

    st.header("Portfolio Dashboard")
    columns = st.columns(4)
    columns[0].metric("Net liquidation", format_money(account.net_liquidation))
    columns[1].metric("Cash", format_money(account.total_cash))
    columns[2].metric("Buying power", format_money(account.buying_power))
    columns[3].metric("Open account orders", str(len(open_orders)))

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

    if isinstance(runtime.broker, DemoBroker):
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
    if isinstance(runtime.broker, DemoBroker):
        symbols = [fixture.symbol for fixture in runtime.broker.fixture_cases]
        label = "Demo symbol"
    else:
        symbols = list(runtime.settings.symbol_allowlist)
        label = "Allowlisted symbol"
    symbol = st.selectbox(label, symbols)
    underlying = runtime.connection.run(runtime.broker.qualify_underlying(symbol))
    managed_quote = runtime.connection.run(runtime.market_data.underlying_quote(underlying))
    quote = managed_quote.quote
    chain_snapshot = runtime.connection.run(runtime.market_data.option_chain_parameters(underlying))
    if not chain_snapshot.parameters:
        raise BrokerDataError(f"No option-chain metadata is available for {symbol}")
    chain = chain_snapshot.parameters[0]

    st.subheader(symbol)
    columns = st.columns(4)
    columns[0].metric("Last", format_money(quote.last))
    columns[1].metric("Bid", format_money(quote.bid))
    columns[2].metric("Ask", format_money(quote.ask))
    columns[3].metric("Data quality", managed_quote.effective_data_quality.value)
    st.caption(
        f"Quote age: {managed_quote.source_age_seconds:.1f} seconds · "
        f"{'cached' if managed_quote.from_cache else 'broker'} · "
        f"{'temporally fresh' if managed_quote.fresh else 'stale'}"
    )
    if not isinstance(runtime.broker, DemoBroker) and not managed_quote.transmission_eligible:
        st.warning("Order transmission is locked: this quote is not a fresh, valid market.")
    st.plotly_chart(quote_ladder_figure(quote), use_container_width=True)

    if isinstance(runtime.broker, DemoBroker):
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
    st.warning(
        "Locked: the Milestone 3 resolver is not available here until the reconciliation "
        "coordinator supplies account-scoped evidence."
    )
    st.subheader("Scenario analysis")
    st.warning(
        "Locked: the tested scenario engine is not wired to reconciled positions and candidates."
    )
    st.subheader("Order preview")
    st.error(
        "Locked: no order can be submitted from this milestone. "
        "DemoBroker rejects submission at its boundary."
    )
