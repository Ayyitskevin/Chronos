"""Streamlit entrypoint for Chronos."""

from __future__ import annotations

import logging

import streamlit as st

from chronos.ui.dashboard import render_dashboard
from chronos.ui.session import get_runtime


def main() -> None:
    st.set_page_config(
        page_title="Chronos",
        page_icon="⏳",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    try:
        runtime = get_runtime()
    except Exception:
        logging.getLogger("chronos.app").exception(
            "Chronos startup failed",
            extra={"event": "application_startup_failed"},
        )
        st.error("Chronos could not start safely. See the local log for technical details.")
        return
    render_dashboard(runtime)


if __name__ == "__main__":
    main()
