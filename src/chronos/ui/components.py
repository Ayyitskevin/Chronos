"""Small accessible Streamlit components shared by both pages."""

from __future__ import annotations

from decimal import Decimal

import streamlit as st

from chronos.domain.enums import BrokerMode, DisplayEnvironment
from chronos.services.reconciliation import ReconciliationResult
from chronos.ui.runtime_scope import RuntimeScopeView
from chronos.utils.time import as_market_time


def format_money(value: Decimal | None, currency: str) -> str:
    if value is None:
        return "Unavailable"
    normalized_currency = currency.strip().upper()
    if not normalized_currency:
        raise ValueError("currency must not be blank")
    return f"{value:,.2f} {normalized_currency}"


def render_runtime_status(scope: RuntimeScopeView) -> None:
    """Render immutable startup identity facts without refreshing broker state."""

    st.subheader("Startup runtime scope")
    columns = st.columns(6)
    columns[0].metric("Broker source", scope.broker_mode.value.upper())
    columns[1].metric("Startup environment", scope.environment.value)
    columns[2].metric("Startup data", scope.data_quality.value)
    columns[3].metric("Startup broker", scope.startup_connection_state.value)
    columns[4].metric("Startup masked account", scope.masked_account_id)
    columns[5].metric("Order path", "CODE LOCKED")
    observed_at = as_market_time(scope.account_observed_at)
    profile = (
        f" · deterministic DEMO profile: {scope.demo_profile.value}"
        if scope.demo_profile is not None
        else ""
    )
    st.caption(
        f"Startup scope observed {observed_at:%Y-%m-%d %H:%M:%S %Z}{profile}. "
        "Historical startup identity only—not current broker health, fresh reconciliation, "
        "current market-data evidence, or order authority."
    )


def render_runtime_scope_unavailable() -> None:
    """Fail closed when a process runtime carries malformed startup identity state."""

    st.info("Decision support only. Live-money transmission is hard-disabled.")
    st.subheader("Startup runtime scope")
    columns = st.columns(6)
    columns[0].metric("Broker source", "UNAVAILABLE")
    columns[1].metric("Startup environment", "UNAVAILABLE")
    columns[2].metric("Startup data", "UNAVAILABLE")
    columns[3].metric("Startup broker", "UNAVAILABLE")
    columns[4].metric("Startup masked account", "UNAVAILABLE")
    columns[5].metric("Order path", "CODE LOCKED")
    st.error("Startup runtime scope could not be verified. Interactive workspace withheld.")


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


def runtime_safety_notice(scope: RuntimeScopeView) -> str:
    """Return mode-accurate global safety copy for one validated startup scope."""

    if scope.broker_mode is BrokerMode.DEMO:
        source = "Startup source is deterministic synthetic DEMO data."
    elif scope.environment is DisplayEnvironment.PAPER:
        source = "Startup source was a read-only IBKR PAPER session."
    else:
        source = "Startup source was a read-only IBKR LIVE session."
    return (
        f"Decision support only. {source} Live-money transmission is hard-disabled. "
        "Startup scope is historical and never substitutes for fresh reconciliation or "
        "market evidence."
    )


def render_safety_notice(scope: RuntimeScopeView) -> None:
    st.info(runtime_safety_notice(scope))
