"""Two-page Streamlit shell backed by the configured broker boundary."""

from __future__ import annotations

import logging

import streamlit as st

from chronos.broker.base import BrokerDataError, BrokerError
from chronos.broker.demo import DemoBroker
from chronos.services.reconciliation import ReconciliationResult
from chronos.ui.charts import quote_ladder_figure
from chronos.ui.components import (
    format_money,
    render_reconciliation_status,
    render_runtime_status,
    render_safety_notice,
)
from chronos.ui.session import AppRuntime


def render_dashboard(runtime: AppRuntime) -> None:
    st.title("Chronos")
    st.caption("Local-first Wheel Strategy workspace")
    render_safety_notice()
    try:
        page = st.sidebar.radio(
            "Workspace",
            ("Portfolio Dashboard", "Symbol Detail & Order Workspace"),
        )
        if page == "Portfolio Dashboard":
            _render_portfolio(runtime.reconciliation.reconcile())
        else:
            status = runtime.connection.run(runtime.broker.connection_status())
            render_runtime_status(status, runtime.settings)
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


def _render_portfolio(result: ReconciliationResult) -> None:
    st.header("Portfolio Dashboard")
    render_reconciliation_status(result)
    st.error(
        "Opening actions are locked. This milestone publishes read-only reconciliation "
        "evidence; it does not activate candidates or orders."
    )
    if result.reasons:
        st.warning("\n".join(f"- {reason}" for reason in result.reasons))

    snapshot = result.snapshot
    if snapshot is None:
        st.error("Chronos withheld account values and exposures because no stable snapshot passed.")
        _render_symbol_reconciliation(result)
        _render_near_term_focus()
        return

    account = snapshot.account
    columns = st.columns(5)
    columns[0].metric(
        "Net liquidation",
        format_money(account.net_liquidation, account.currency),
    )
    columns[1].metric("Cash", format_money(account.total_cash, account.currency))
    columns[2].metric("Buying power", format_money(account.buying_power, account.currency))
    columns[3].metric("Open account orders", str(len(snapshot.open_orders)))
    columns[4].metric("Observed executions", str(snapshot.execution_count))

    st.subheader("Broker positions")
    rows = [
        {
            "Symbol": position.contract.symbol,
            "Type": position.contract.security_type.value,
            "Quantity": str(position.quantity),
            "Broker average cost": format_money(
                position.average_cost,
                position.contract.currency,
            ),
            "Market price": format_money(
                position.market_price,
                position.contract.currency,
            ),
            "Unrealized P&L": format_money(
                position.unrealized_pnl,
                position.contract.currency,
            ),
        }
        for position in snapshot.positions
    ]
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.caption("No broker positions were present in the stable observation.")

    st.subheader("Open broker orders")
    order_rows = [
        {
            "Broker order": order.broker_order_id,
            "Symbol": order.contract.symbol,
            "Type": order.contract.security_type.value,
            "Side": order.side.value,
            "Quantity": str(order.quantity),
            "Filled": str(order.filled_quantity),
            "Remaining": str(order.remaining_quantity),
            "Limit": format_money(order.limit_price, order.contract.currency),
            "Lifecycle": order.lifecycle.value,
            "Transmit": "YES" if order.transmit else "NO",
        }
        for order in snapshot.open_orders
    ]
    if order_rows:
        st.dataframe(order_rows, use_container_width=True, hide_index=True)
    else:
        st.caption("No open broker orders were present in the stable observation.")

    _render_symbol_reconciliation(result)
    _render_near_term_focus()


def _render_symbol_reconciliation(result: ReconciliationResult) -> None:
    st.subheader("Wheel reconciliation")
    st.dataframe(
        [
            {
                "Symbol": symbol.symbol,
                "Status": symbol.status.value,
                "Wheel stage": symbol.stage.value,
                "Stock shares": str(symbol.stock_shares),
                "Short puts": str(symbol.short_put_contracts),
                "Short calls": str(symbol.short_call_contracts),
                "Pending puts": str(symbol.pending_put_contracts),
                "Pending calls": str(symbol.pending_call_contracts),
                "Manual review": "YES" if symbol.manual_review_required else "NO",
                "Opening actions": "LOCKED",
            }
            for symbol in result.symbols
        ],
        use_container_width=True,
        hide_index=True,
    )
    for symbol in result.symbols:
        with st.expander(f"{symbol.symbol} reconciliation reasoning"):
            for reason in symbol.reasons:
                st.write(f"- {reason}")


def _render_near_term_focus() -> None:
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
    columns[0].metric("Last", format_money(quote.last, quote.contract.currency))
    columns[1].metric("Bid", format_money(quote.bid, quote.contract.currency))
    columns[2].metric("Ask", format_money(quote.ask, quote.contract.currency))
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
