"""Portfolio page: backend health, account summary, positions, reconciliation."""

from __future__ import annotations

from typing import Any

import streamlit as st

from chronos.config.settings import get_settings
from chronos.ui.api_client import ApiClient, ApiClientError
from chronos.ui.pages import render_reasons, value_or_dash


def render(client: ApiClient) -> None:
    st.title("Chronos")
    st.header("Portfolio")
    try:
        health = client.health()
    except ApiClientError as error:
        st.error(str(error))
        return
    _render_health_banner(health)
    _render_account_summary(client)
    _render_positions(client)
    _render_reconciliation(client)
    _render_product_families()


def _render_health_banner(health: dict[str, Any]) -> None:
    read_only = bool(health.get("read_only", True))
    lease_held = bool(health.get("writer_lease_held", False))
    readiness = health.get("service_readiness")
    readiness_data = readiness if isinstance(readiness, dict) else {}
    readiness_state = value_or_dash(readiness_data.get("state"))
    if readiness_state == "READY":
        st.success("Backend service readiness is READY.")
    elif readiness_state == "NOT_READY":
        st.error("Backend service readiness is NOT READY.")
    else:
        st.warning(f"Backend service readiness is {readiness_state}.")
    render_reasons(
        readiness_data.get("reasons"),
        empty_message="Service readiness reported no blocking reasons.",
    )
    columns = st.columns(4)
    columns[0].metric("Broker mode", value_or_dash(health.get("broker_mode")).upper())
    columns[1].metric("Environment", value_or_dash(health.get("environment")).upper())
    columns[2].metric("Backend access", "READ-ONLY" if read_only else "READ-WRITE")
    columns[3].metric("Writer lease", "HELD" if lease_held else "NOT HELD")
    if read_only:
        st.warning(
            "This backend is running read-only: another Chronos backend holds the "
            "single-writer lease for this database. Inspection works; mutation refuses."
        )


def _render_account_summary(client: ApiClient) -> None:
    st.subheader("Account summary")
    try:
        summary = client.account_summary()
    except ApiClientError as error:
        st.error(str(error))
        return
    currency = value_or_dash(summary.get("currency"))
    columns = st.columns(4)
    columns[0].metric("Account", value_or_dash(summary.get("account_id")))
    columns[1].metric(
        "Net liquidation", f"{value_or_dash(summary.get('net_liquidation'))} {currency}"
    )
    columns[2].metric("Total cash", f"{value_or_dash(summary.get('total_cash'))} {currency}")
    columns[3].metric("Buying power", f"{value_or_dash(summary.get('buying_power'))} {currency}")
    as_of = summary.get("as_of")
    if as_of is not None:
        st.caption(f"As of {as_of}")


def _render_positions(client: ApiClient) -> None:
    st.subheader("Positions")
    try:
        positions = client.positions()
    except ApiClientError as error:
        st.error(str(error))
        return
    if not positions:
        st.info("The backend reported no positions.")
        return
    rows = [_position_row(position) for position in positions]
    st.dataframe(rows, hide_index=True, width="stretch")


def _position_row(position: dict[str, Any]) -> dict[str, str]:
    contract = position.get("contract")
    contract_data: dict[str, Any] = contract if isinstance(contract, dict) else {}
    return {
        "Account": value_or_dash(position.get("account_id")),
        "Symbol": value_or_dash(contract_data.get("symbol")),
        "Type": value_or_dash(contract_data.get("security_type")),
        "Quantity": value_or_dash(position.get("quantity")),
        "Average cost": value_or_dash(position.get("average_cost")),
        "Market price": value_or_dash(position.get("market_price")),
        "Unrealized PnL": value_or_dash(position.get("unrealized_pnl")),
    }


def _render_reconciliation(client: ApiClient) -> None:
    st.subheader("Reconciliation")
    try:
        result = client.reconciliation()
    except ApiClientError as error:
        st.error(str(error))
        return
    columns = st.columns(2)
    columns[0].metric("Reconciliation", value_or_dash(result.get("status")))
    columns[1].metric("Opening actions", "LOCKED")
    render_reasons(
        result.get("reasons"),
        empty_message="Reconciliation reported no blocking reasons.",
    )


def _render_product_families() -> None:
    settings = get_settings()
    st.subheader("Product families")
    option_symbols = ", ".join(settings.symbol_allowlist)
    st.markdown(f"**OPTION** — enabled · symbol allowlist: {option_symbols}")
    st.markdown(
        f"**STOCK** — allowlisted ({option_symbols}); order support arrives with Milestone 5"
    )
    if settings.crypto_allowlist:
        crypto_symbols = ", ".join(settings.crypto_allowlist)
        st.markdown(f"**CRYPTO** — enabled · crypto allowlist: {crypto_symbols}")
    else:
        st.markdown("**CRYPTO** — disabled (CRYPTO_ALLOWLIST is empty; deny-by-default)")
