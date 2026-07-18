"""Backend-driven Streamlit entrypoint (Milestone 4 cutover).

This is the default UI: a thin client over the loopback FastAPI backend. It
imports no broker adapter, no runtime, and no strategy service — only the
:class:`chronos.ui.api_client.ApiClient` and page renderers. The legacy
in-process app remains at ``chronos/app.py`` until Milestone 5 removes it.
"""

from __future__ import annotations

from collections.abc import Callable

import streamlit as st

from chronos.config.settings import get_settings
from chronos.ui.api_client import ApiClient, ApiClientError
from chronos.ui.pages import activity, portfolio, settings_page, symbol_detail


def _build_client() -> ApiClient:
    return ApiClient.from_settings(get_settings())


# Cached once per UI process so Streamlit reruns reuse one client.
_get_client: Callable[[], ApiClient] = st.cache_resource(
    show_spinner="Connecting to the Chronos backend…"
)(_build_client)


def main() -> None:
    st.set_page_config(page_title="Chronos", page_icon="⏳", layout="wide")
    try:
        client = _get_client()
    except ApiClientError as error:
        st.error(str(error))
        st.stop()  # NoReturn: halts the script run
    # order_workspace is imported lazily so a partial page module can never
    # break the whole app at import time.
    from chronos.ui.pages import order_workspace

    pages = [
        st.Page(lambda: portfolio.render(client), title="Portfolio", url_path="portfolio"),
        st.Page(lambda: symbol_detail.render(client), title="Symbol Detail", url_path="symbol"),
        st.Page(
            lambda: order_workspace.render(client),
            title="Order Workspace",
            url_path="orders",
        ),
        st.Page(lambda: activity.render(client), title="Activity", url_path="activity"),
        st.Page(lambda: settings_page.render(client), title="Settings", url_path="settings"),
    ]
    st.navigation(pages).run()


if __name__ == "__main__":
    main()
