"""AppTest smoke of the backend-driven UI against a canned MockTransport client.

The pages run through real ApiClient code; only the HTTP layer is faked, so
the tests exercise the actual rendering/parse paths. Assertions pin the M4
guarantees: the regime disclaimer text renders, and no page exposes any
submit/transmit control.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from chronos.ui.api_client import ApiClient, ApiClientError

_APP = Path(__file__).resolve().parents[2] / "src/chronos/ui/backend_app.py"

_REGIME_NOTE = (
    "Pine-derived heuristic context — not a validated signal, not an assignment "
    "probability; research verdict: zero strategies validated."
)


# Streamlit ``AppTest`` timeout budget (raised 2026-08-08).
#
# The FIRST render in a process that reaches ``st.dataframe`` pays a one-time
# serialization-stack import. Measured 2026-08-08: a monitor render with data
# took 9.07s as the first in its process and 0.28s as the second, while the same
# app with no data rendered in 1.13s cold. The domain logic is not the cost —
# ``build_snapshot`` returns in under 0.01s in every configuration tested.
#
# That cost lands on whichever AppTest runs first, so a tight timeout is an
# ORDER-DEPENDENT flake rather than a property of any one test. The monitor
# suite's 15s budget gave 1.65x headroom over a 9s cold render and failed once in
# six full-suite runs under CPU contention.
#
# 60s does not make a passing test slower — a warm render finishes in well under
# a second. It is headroom, so that a green suite means "the page rendered" and
# never "the machine happened to be quiet". The timeout exists to stop a hung
# render blocking forever, and 60s serves that as well as 10s did.
_APPTEST_TIMEOUT = 60


def _canned(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/health":
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "broker_mode": "demo",
                "environment": "paper",
                "read_only": False,
                "writer_lease_held": True,
            },
        )
    if path == "/account/summary":
        return httpx.Response(
            200,
            json={
                "account_id": "DEMO123",
                "net_liquidation": "250000.00",
                "total_cash": "100000.00",
                "buying_power": "480000.00",
                "currency": "USD",
                "as_of": "2026-07-17T15:00:00+00:00",
            },
        )
    if path == "/account/positions":
        return httpx.Response(200, json=[])
    if path == "/strategy/reconciliation":
        return httpx.Response(200, json={"status": "MANUAL_REVIEW", "reasons": ["demo"]})
    if path.startswith("/strategy/regime/"):
        return httpx.Response(
            200,
            json={
                "symbol": path.rsplit("/", 1)[-1],
                "available": True,
                "as_of_session": "2026-07-16",
                "bars": 300,
                "trend_state": "UPTREND",
                "ema_fast": "101.5",
                "ema_slow": "100.0",
                "rsi2": "88.0",
                "vol_percentile": "0.42",
                "note": _REGIME_NOTE,
                "reasons": ["trend UPTREND"],
            },
        )
    return httpx.Response(200, json={"status": "WITHHELD", "reasons": []})


def _mock_client() -> ApiClient:
    transport = httpx.MockTransport(_canned)
    return ApiClient("http://127.0.0.1:8765", "test-token", transport=transport)


@pytest.fixture(autouse=True)
def _reset_cache() -> Iterator[None]:
    import streamlit as st

    from chronos.config.settings import get_settings

    st.cache_resource.clear()
    # backend_app._build_client evaluates get_settings() as the argument to the
    # (patched) ApiClient.from_settings, populating the process-wide lru_cache.
    # Clear it so a later test's DATABASE_URL/env monkeypatch is honoured.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _patch_from_settings(monkeypatch: pytest.MonkeyPatch, factory: object) -> None:
    # AppTest execs backend_app.py fresh each run, so patch the shared class
    # method the fresh script calls (ApiClient.from_settings), not the module.
    monkeypatch.setattr(ApiClient, "from_settings", staticmethod(factory))


def _run_symbol_detail() -> None:
    # Fully self-contained: AppTest.from_function execs this in an isolated
    # temp module that sees only what this function defines/imports.
    import httpx

    from chronos.ui.api_client import ApiClient
    from chronos.ui.pages import symbol_detail

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/strategy/regime/"):
            return httpx.Response(
                200,
                json={
                    "symbol": request.url.path.rsplit("/", 1)[-1],
                    "available": True,
                    "as_of_session": "2026-07-16",
                    "bars": 300,
                    "trend_state": "UPTREND",
                    "ema_fast": "101.5",
                    "ema_slow": "100.0",
                    "rsi2": "88.0",
                    "vol_percentile": "0.42",
                    "note": (
                        "Pine-derived heuristic context — not a validated signal, not an "
                        "assignment probability; research verdict: zero strategies validated."
                    ),
                    "reasons": ["trend UPTREND"],
                },
            )
        return httpx.Response(200, json={"status": "WITHHELD", "reasons": []})

    client = ApiClient("http://127.0.0.1:8765", "t", transport=httpx.MockTransport(handler))
    symbol_detail.render(client)


def _run_order_workspace() -> None:
    import httpx

    from chronos.ui.api_client import ApiClient
    from chronos.ui.pages import order_workspace

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "WITHHELD", "reasons": []})

    client = ApiClient("http://127.0.0.1:8765", "t", transport=httpx.MockTransport(handler))
    order_workspace.render(client)


def test_portfolio_renders_account_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercises the full backend_app navigation wiring + the default page.
    _patch_from_settings(monkeypatch, lambda *_a, **_k: _mock_client())
    app = AppTest.from_file(_APP).run(timeout=_APPTEST_TIMEOUT)
    assert not app.exception
    metrics = {m.label: m.value for m in app.metric}
    assert metrics.get("Account") == "DEMO123"
    assert metrics.get("Broker mode") == "DEMO"


def test_symbol_detail_shows_regime_disclaimer() -> None:
    # st.navigation callable pages can't be reached by switch_page, so render
    # the page directly inside a real ScriptRunContext.
    app = AppTest.from_function(_run_symbol_detail).run(timeout=_APPTEST_TIMEOUT)
    assert not app.exception
    captions = " ".join(c.value for c in app.caption)
    assert "not a validated signal" in captions


def test_order_workspace_has_no_submit_control() -> None:
    app = AppTest.from_function(_run_order_workspace).run(timeout=_APPTEST_TIMEOUT)
    assert not app.exception
    for button in app.button:
        label = button.label.lower()
        assert "submit" not in label
        assert "transmit" not in label
    # The rehearsal-only nature is stated to the operator.
    errors = " ".join(e.value.lower() for e in app.error)
    assert "submit" in errors  # e.g. "...cannot ... submit an order"


def test_unreachable_backend_shows_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raising(*_a: object, **_k: object) -> ApiClient:
        raise ApiClientError("backend not reachable at http://127.0.0.1:8765")

    _patch_from_settings(monkeypatch, raising)
    app = AppTest.from_file(_APP).run(timeout=_APPTEST_TIMEOUT)
    assert not app.exception
    assert any("not reachable" in e.value for e in app.error)
