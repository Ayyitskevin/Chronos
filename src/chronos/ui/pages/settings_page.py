"""Settings page: read-only display of the loaded configuration.

Editing happens only through the environment / .env file; this page shows
what the backend process validated at startup so the operator can confirm
allowlists, risk limits, and family enablement at a glance.
"""

from __future__ import annotations

import streamlit as st

from chronos.config.settings import get_settings
from chronos.ui.api_client import ApiClient


def render(client: ApiClient) -> None:
    del client  # settings are process configuration, not backend state
    settings = get_settings()
    st.title("Chronos")
    st.header("Settings")
    st.caption(
        "Read-only view of the validated configuration. Edit via the environment / .env only."
    )

    st.subheader("Product families")
    option_symbols = ", ".join(settings.symbol_allowlist) or "(none)"
    st.markdown(f"**OPTION / STOCK** — symbol allowlist: {option_symbols}")
    if settings.crypto_allowlist:
        st.markdown(f"**CRYPTO** — enabled · allowlist: {', '.join(settings.crypto_allowlist)}")
    else:
        st.markdown("**CRYPTO** — disabled (CRYPTO_ALLOWLIST is empty; deny-by-default)")

    st.subheader("Backend")
    st.write(
        {
            "broker_mode": settings.broker_mode.value,
            "broker_adapter": settings.broker_adapter.value,
            "ib_environment": settings.ib_environment.value,
            "backend_host": settings.backend_host,
            "backend_port": settings.backend_port,
        }
    )

    st.subheader("Risk limits")
    st.write(
        {
            "max_contracts_per_order": settings.max_contracts_per_order,
            "max_open_short_option_contracts": settings.max_open_short_option_contracts,
            "max_opening_orders_per_day": settings.max_opening_orders_per_day,
            "max_gross_assignment_usd": str(settings.max_gross_assignment_usd),
            "max_symbol_allocation_pct": str(settings.max_symbol_allocation_pct),
            "max_total_wheel_allocation_pct": str(settings.max_total_wheel_allocation_pct),
            "min_cash_buffer_usd": str(settings.min_cash_buffer_usd),
            "min_cash_buffer_pct": str(settings.min_cash_buffer_pct),
            "min_excess_liquidity_usd": str(settings.min_excess_liquidity_usd),
            "max_session_drawdown_usd": str(settings.max_session_drawdown_usd),
            "max_session_drawdown_pct": str(settings.max_session_drawdown_pct),
        }
    )

    st.subheader("Live-arming policy")
    st.write(
        {
            "require_live_arming": settings.require_live_arming,
            "live_arm_ttl_minutes": settings.live_arm_ttl_minutes,
            "require_typed_confirmation": settings.require_typed_confirmation,
            "order_confirmation_ttl_seconds": settings.order_confirmation_ttl_seconds,
        }
    )
    st.info(
        "Live trading is a committed deliverable. The Milestone 6 live safety layer now "
        "exists — arming (with TTL), the kill switch, the session-drawdown breaker, and "
        "the ten-gate live stack — and the /live endpoints manage it. What remains is "
        "Milestone 7: wiring transmit=True for the LIVE branch at the single submission "
        "boundary. Until then the policy above is displayed and the gates are built and "
        "tested, but no live order is transmitted."
    )
