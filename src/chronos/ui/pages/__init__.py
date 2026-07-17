"""Backend-driven Streamlit pages (Milestone 4 cutover).

Every page is a thin renderer over :class:`chronos.ui.api_client.ApiClient`.
Pages never import broker adapters, the runtime, or strategy services; they
display backend JSON verbatim and expose no order-submission control of any
kind (real order endpoints arrive with Milestone 5's order service).
"""

from __future__ import annotations

from typing import Any

import streamlit as st


def value_or_dash(value: Any) -> str:
    """Render a backend value verbatim, or an em dash when the backend sent null."""

    return "—" if value is None else str(value)


def render_reasons(reasons: Any, *, empty_message: str | None = None) -> None:
    """Render backend-provided reason strings verbatim as a bulleted list."""

    if isinstance(reasons, list | tuple) and reasons:
        for reason in reasons:
            st.write(f"- {reason}")
    elif empty_message is not None:
        st.info(empty_message)


def render_rehearsal_status(result: dict[str, Any], label: str) -> None:
    """Render a rehearsal result's status, timestamp, and reasons verbatim."""

    st.metric(label, value_or_dash(result.get("status")))
    evaluated_at = result.get("evaluated_at")
    if evaluated_at is not None:
        st.caption(f"Evaluated at {evaluated_at}")
    render_reasons(result.get("reasons"))
