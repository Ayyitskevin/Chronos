"""Order workspace: DEMO rehearsal flow (risk preview → what-if → approval).

Nothing on this page can submit anything. The three backend endpoints it
calls are deterministic DEMO rehearsals that stop at preview/acknowledgment
results; real order endpoints arrive with Milestone 5's order service, and
no submit or transmit control exists here by design.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from chronos.config.settings import get_settings
from chronos.ui.api_client import ApiClient, ApiClientError
from chronos.ui.pages import render_rehearsal_status, value_or_dash

_RISK_STATE_KEY = "backend_risk_preview_result"
_WHAT_IF_STATE_KEY = "backend_demo_what_if_result"
_APPROVAL_STATE_KEY = "backend_demo_approval_result"


def render(client: ApiClient) -> None:
    st.title("Chronos")
    st.header("Order Workspace (DEMO rehearsal)")
    st.error(
        "DEMO rehearsal only. Every action on this page stops at a rehearsal result; "
        "it cannot confirm, persist, transmit, or submit an order."
    )
    st.error(
        "No confirmation or submit control exists in this milestone. Real order "
        "endpoints arrive with Milestone 5's order service, behind the full gate stack."
    )
    settings = get_settings()
    symbol = st.selectbox("Symbol", options=list(settings.symbol_allowlist))
    selected_contract_id = int(
        st.number_input(
            "Selected contract id (conId from candidate evidence)",
            min_value=1,
            step=1,
            value=1,
        )
    )
    commission = st.text_input(
        "Total commission estimate (exact decimal text, e.g. 1.05)",
        value="0",
        help="Passed to the backend verbatim as a decimal string; never a float.",
    ).strip()

    _render_risk_preview_step(client, symbol, selected_contract_id, commission)
    limit_price = _render_what_if_step(client, symbol, selected_contract_id, commission)
    _render_approval_step(client, symbol, selected_contract_id, commission, limit_price)


def _render_risk_preview_step(
    client: ApiClient,
    symbol: str,
    selected_contract_id: int,
    commission: str,
) -> None:
    st.subheader("Step 1 — Risk preview (read-only payoff model)")
    st.caption(
        "An expiration payoff model over fresh candidate evidence — not a broker "
        "what-if, order draft, forecast, or authorization."
    )
    if st.button("Run DEMO risk preview"):
        _store_result(
            _RISK_STATE_KEY,
            lambda: client.risk_preview(symbol, selected_contract_id, commission),
        )
    result = _stored_result(_RISK_STATE_KEY)
    if result is not None:
        render_rehearsal_status(result, "Risk preview")


def _render_what_if_step(
    client: ApiClient,
    symbol: str,
    selected_contract_id: int,
    commission: str,
) -> str:
    st.subheader("Step 2 — What-if rehearsal (DEMO)")
    st.error(
        "DEMO rehearsal only. A successful result stops at WHAT_IF_PREVIEWED; it cannot "
        "confirm, persist, transmit, or submit an order."
    )
    limit_price = st.text_input(
        "Limit credit per share (exact decimal text)",
        value="",
        help="An explicit limit is required; Chronos supplies no default.",
    ).strip()
    if st.button("Run DEMO what-if rehearsal"):
        if not limit_price:
            st.info("Enter an explicit limit credit to enable the deterministic what-if rehearsal.")
        else:
            _store_result(
                _WHAT_IF_STATE_KEY,
                lambda: client.demo_what_if(symbol, selected_contract_id, commission, limit_price),
            )
    result = _stored_result(_WHAT_IF_STATE_KEY)
    if result is not None:
        render_rehearsal_status(result, "DEMO what-if rehearsal")
        _render_what_if_preview(result)
    return limit_price


def _render_what_if_preview(result: dict[str, Any]) -> None:
    preview = result.get("preview")
    if not isinstance(preview, dict):
        return
    columns = st.columns(4)
    columns[0].metric("Quantity", value_or_dash(preview.get("quantity")))
    columns[1].metric("Limit price", value_or_dash(preview.get("limit_price")))
    columns[2].metric("Fresh bid", value_or_dash(preview.get("fresh_bid")))
    columns[3].metric("Fresh ask", value_or_dash(preview.get("fresh_ask")))
    obligation = preview.get("gross_assignment_obligation")
    if obligation is not None:
        st.caption(f"Gross assignment obligation: {obligation}")


def _render_approval_step(
    client: ApiClient,
    symbol: str,
    selected_contract_id: int,
    commission: str,
    limit_price: str,
) -> None:
    st.subheader("Step 3 — Approval rehearsal (DEMO)")
    st.error(
        "DEMO-only acknowledgment rehearsal. It creates no approval authority, "
        "persistence, order lifecycle, transmission, or broker submission."
    )
    gross_obligation = st.text_input(
        "Gross assignment obligation (exact decimal text)",
        value="",
        help="Strike times deliverable shares, exactly as displayed by the what-if rehearsal.",
    ).strip()
    typed_symbol = st.text_input(
        "Type the exact displayed symbol for the DEMO rehearsal",
        value="",
        help="Enter the uppercase symbol exactly as displayed. Chronos supplies no default.",
    )
    acknowledged_quantity = int(
        st.number_input(
            "Acknowledged contract quantity (mirror the displayed one-contract quantity)",
            min_value=1,
            step=1,
            value=1,
        )
    )
    acknowledged_contract_id = int(
        st.number_input(
            "Acknowledged contract id (mirror the selected conId)",
            min_value=1,
            step=1,
            value=1,
        )
    )
    acknowledged_limit_price = st.text_input(
        "Acknowledged limit credit (mirror, exact decimal text)",
        value="",
    ).strip()
    acknowledged_obligation = st.text_input(
        "Acknowledged gross assignment obligation (mirror, exact decimal text)",
        value="",
    ).strip()
    risk_terms_acknowledged = st.checkbox(
        "I acknowledge the exact DEMO rehearsal terms and assignment risk.",
        value=False,
        help=(
            "A deliberate, memoryless rehearsal acknowledgment only. It does not "
            "authorize an order."
        ),
    )
    if st.button("Run DEMO approval rehearsal"):
        missing = [
            label
            for label, value in (
                ("limit credit (step 2)", limit_price),
                ("gross assignment obligation", gross_obligation),
                ("acknowledged limit credit", acknowledged_limit_price),
                ("acknowledged gross assignment obligation", acknowledged_obligation),
            )
            if not value
        ]
        if missing:
            st.info(f"Enter every exact decimal field first: {', '.join(missing)}.")
        else:
            payload: dict[str, Any] = {
                "symbol": symbol,
                "selected_contract_id": selected_contract_id,
                "total_commission_estimate": commission,
                "limit_price": limit_price,
                "gross_assignment_obligation": gross_obligation,
                "typed_symbol": typed_symbol,
                "acknowledged_quantity": acknowledged_quantity,
                "acknowledged_contract_id": acknowledged_contract_id,
                "acknowledged_limit_price": acknowledged_limit_price,
                "acknowledged_gross_assignment_obligation": acknowledged_obligation,
                "risk_terms_acknowledged": risk_terms_acknowledged,
            }
            _store_result(_APPROVAL_STATE_KEY, lambda: client.demo_approval(payload))
    result = _stored_result(_APPROVAL_STATE_KEY)
    if result is not None:
        render_rehearsal_status(result, "DEMO approval rehearsal")
        receipt = result.get("receipt")
        if isinstance(receipt, dict):
            st.caption(
                "Approval rehearsal reference: "
                f"{value_or_dash(receipt.get('approval_reference'))} — a rehearsal "
                "receipt, not an order and not authority to create one."
            )


def _store_result(state_key: str, request: Any) -> None:
    try:
        st.session_state[state_key] = request()
    except ApiClientError as error:
        st.session_state.pop(state_key, None)
        st.error(str(error))


def _stored_result(state_key: str) -> dict[str, Any] | None:
    stored = st.session_state.get(state_key)
    return stored if isinstance(stored, dict) else None
