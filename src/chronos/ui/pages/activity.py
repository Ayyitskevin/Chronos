"""Activity page: reconciliation reasons and a backend health snapshot.

The durable order/event activity timeline is populated by Milestone 5's order
tracker; until then this page surfaces the reconciliation reasons and health
the backend already exposes, and says so plainly.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from chronos.ui.api_client import ApiClient, ApiClientError
from chronos.ui.pages import render_reasons, value_or_dash


def render(client: ApiClient) -> None:
    st.title("Chronos")
    st.header("Activity")
    st.info(
        "The durable order and execution timeline arrives with Milestone 5's order "
        "tracker. This page currently surfaces reconciliation reasons and backend health."
    )
    _render_reconciliation(client)
    _render_health(client)


def _render_reconciliation(client: ApiClient) -> None:
    st.subheader("Reconciliation")
    try:
        result = client.reconciliation()
    except ApiClientError as error:
        st.error(str(error))
        return
    st.metric("Reconciliation", value_or_dash(result.get("status")))
    render_reasons(
        result.get("reasons"),
        empty_message="Reconciliation reported no blocking reasons.",
    )


def _render_health(client: ApiClient) -> None:
    st.subheader("Backend health")
    try:
        health: dict[str, Any] = client.health()
    except ApiClientError as error:
        st.error(str(error))
        return
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
    columns[0].metric("Service", readiness_state)
    columns[1].metric("Broker mode", value_or_dash(health.get("broker_mode")).upper())
    columns[2].metric("Access", "READ-ONLY" if health.get("read_only", True) else "READ-WRITE")
    columns[3].metric("Writer lease", "HELD" if health.get("writer_lease_held") else "NOT HELD")
