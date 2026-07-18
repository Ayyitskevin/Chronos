"""Symbol detail page: regime context panel and read-only candidate evidence."""

from __future__ import annotations

from typing import Any

import streamlit as st

from chronos.config.settings import get_settings
from chronos.ui.api_client import ApiClient, ApiClientError
from chronos.ui.pages import render_reasons, value_or_dash

_CANDIDATES_STATE_PREFIX = "backend_candidates_"


def render(client: ApiClient) -> None:
    st.title("Chronos")
    st.header("Symbol Detail")
    settings = get_settings()
    symbol = st.selectbox("Symbol", options=list(settings.symbol_allowlist))
    st.error("Opening actions are locked. Candidate evidence cannot authorize an order.")
    _render_regime_context(client, symbol)
    _render_candidates(client, symbol)


def _render_regime_context(client: ApiClient, symbol: str) -> None:
    st.subheader("Regime context")
    try:
        regime = client.regime(symbol)
    except ApiClientError as error:
        st.error(str(error))
        return
    available = bool(regime.get("available", False))
    if available:
        columns = st.columns(3)
        columns[0].metric("Trend state", value_or_dash(regime.get("trend_state")))
        columns[1].metric("RSI(2)", value_or_dash(regime.get("rsi2")))
        columns[2].metric("Vol percentile (0..1)", value_or_dash(regime.get("vol_percentile")))
        detail_columns = st.columns(3)
        detail_columns[0].metric("EMA fast", value_or_dash(regime.get("ema_fast")))
        detail_columns[1].metric("EMA slow", value_or_dash(regime.get("ema_slow")))
        detail_columns[2].metric("Daily bars", value_or_dash(regime.get("bars")))
        as_of_session = regime.get("as_of_session")
        if as_of_session is not None:
            st.caption(f"As of session {as_of_session}")
    else:
        st.info(f"Regime context is unavailable for {value_or_dash(regime.get('symbol'))}.")
        render_reasons(
            regime.get("reasons"),
            empty_message="The backend reported no reasons for the unavailable regime context.",
        )
    note = regime.get("note")
    if note is not None:
        st.caption(str(note))


def _render_candidates(client: ApiClient, symbol: str) -> None:
    st.subheader("Short-put candidates")
    state_key = f"{_CANDIDATES_STATE_PREFIX}{symbol}"
    if st.button("Evaluate candidates (read-only)"):
        try:
            st.session_state[state_key] = client.evaluate_candidates(symbol)
        except ApiClientError as error:
            st.session_state.pop(state_key, None)
            st.error(str(error))
    evaluation = st.session_state.get(state_key)
    if not isinstance(evaluation, dict):
        st.info(
            "No candidate request has been made. Run the explicit read-only evaluation "
            "to capture a fresh, bounded evidence window."
        )
        return
    columns = st.columns(2)
    columns[0].metric("Candidate result", value_or_dash(evaluation.get("status")))
    columns[1].metric("Candidate actions", "LOCKED")
    evaluated_at = evaluation.get("evaluated_at")
    if evaluated_at is not None:
        st.caption(f"Last explicit evaluation at {evaluated_at}")
    render_reasons(evaluation.get("reasons"))
    _render_ranked_candidates(evaluation)


def _render_ranked_candidates(evaluation: dict[str, Any]) -> None:
    resolution = evaluation.get("resolution")
    if not isinstance(resolution, dict):
        return
    candidates = resolution.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        st.info("The evaluation returned no ranked candidates.")
        return
    rows = [
        _candidate_row(rank, candidate)
        for rank, candidate in enumerate(candidates, start=1)
        if isinstance(candidate, dict)
    ]
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")


def _candidate_row(rank: int, candidate: dict[str, Any]) -> dict[str, str]:
    contract = candidate.get("contract")
    contract_data: dict[str, Any] = contract if isinstance(contract, dict) else {}
    return {
        "Rank": str(rank),
        "Contract id": value_or_dash(contract_data.get("con_id")),
        "Expiration": value_or_dash(candidate.get("expiration")),
        "DTE": value_or_dash(candidate.get("dte")),
        "Strike": value_or_dash(candidate.get("strike")),
        "Delta": value_or_dash(candidate.get("delta")),
        "Bid": value_or_dash(candidate.get("bid")),
        "Ask": value_or_dash(candidate.get("ask")),
        "Score": value_or_dash(candidate.get("score")),
    }
