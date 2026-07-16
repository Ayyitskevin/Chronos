"""Small accessible Streamlit components shared by both pages."""

from __future__ import annotations

from decimal import Decimal

import streamlit as st

from chronos.config.settings import Settings
from chronos.domain.models import ConnectionStatus
from chronos.utils.logging import mask_account_id
from chronos.utils.time import as_market_time


def format_money(value: Decimal | None) -> str:
    if value is None:
        return "Unavailable"
    return f"${value:,.2f}"


def render_runtime_status(status: ConnectionStatus, settings: Settings) -> None:
    columns = st.columns(5)
    columns[0].metric("Environment", status.environment.value)
    columns[1].metric("Data", status.data_quality.value)
    columns[2].metric("Broker", status.state.value)
    columns[3].metric("Account", mask_account_id(status.account_id or ""))
    columns[4].metric(
        "Paper-order config",
        "CONFIGURED / CODE LOCKED" if settings.transmission_possible else "BLOCKED",
    )
    if status.last_successful_sync is None:
        st.caption("Last successful broker synchronization: never")
    else:
        local_time = as_market_time(status.last_successful_sync)
        st.caption(f"Last successful broker synchronization: {local_time:%Y-%m-%d %H:%M:%S %Z}")


def render_safety_notice() -> None:
    st.info(
        "Decision support only. Demo data is synthetic and deterministic. "
        "Live-money transmission is hard-disabled."
    )
