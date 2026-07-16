"""Small accessible Streamlit components shared by both pages."""

from __future__ import annotations

from decimal import Decimal

import streamlit as st

from chronos.config.settings import Settings
from chronos.domain.models import ConnectionStatus
from chronos.services.reconciliation import ReconciliationResult
from chronos.utils.logging import mask_account_id
from chronos.utils.time import as_market_time


def format_money(value: Decimal | None, currency: str) -> str:
    if value is None:
        return "Unavailable"
    normalized_currency = currency.strip().upper()
    if not normalized_currency:
        raise ValueError("currency must not be blank")
    return f"{value:,.2f} {normalized_currency}"


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


def render_reconciliation_status(result: ReconciliationResult) -> None:
    """Render only the presentation-safe status produced by reconciliation."""

    snapshot = result.snapshot
    columns = st.columns(5)
    columns[0].metric(
        "Environment",
        snapshot.environment.value if snapshot is not None else "Unavailable",
    )
    columns[1].metric(
        "Data",
        snapshot.data_quality.value if snapshot is not None else "Unavailable",
    )
    columns[2].metric("Reconciliation", result.status.value)
    columns[3].metric(
        "Account",
        snapshot.account.masked_account_id if snapshot is not None else "Unavailable",
    )
    columns[4].metric("Opening actions", "LOCKED")
    if snapshot is None:
        st.caption("No stable broker observation was published for this reconciliation run.")
        return
    captured_at = as_market_time(snapshot.captured_at)
    st.caption(
        f"Broker observation ended: {captured_at:%Y-%m-%d %H:%M:%S %Z} · "
        f"end-to-end evidence window {snapshot.window_seconds:.3f}s · "
        f"broker clock window {snapshot.server_window_seconds:.3f}s · "
        f"{snapshot.execution_count} execution(s) observed"
    )


def render_safety_notice() -> None:
    st.info(
        "Decision support only. Demo data is synthetic and deterministic. "
        "Live-money transmission is hard-disabled."
    )
