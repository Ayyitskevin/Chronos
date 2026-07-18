"""ApiClient: token header, method routing, and HTTP error mapping (no network)."""

from __future__ import annotations

import httpx
import pytest

from chronos.ui.api_client import TOKEN_HEADER, ApiClient, ApiClientError


def _client(handler: object, *, timeout: float = 5.0) -> ApiClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return ApiClient("http://127.0.0.1:8765", "test-token", timeout, transport=transport)


def test_token_header_is_sent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get(TOKEN_HEADER, "")
        return httpx.Response(200, json={"status": "ok"})

    client = _client(handler)
    client.health()
    assert seen["token"] == "test-token"


def test_get_and_post_paths_and_verbs() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/account/positions":
            return httpx.Response(200, json=[{"account_id": "DU1"}])
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    client.health()
    client.account_summary()
    client.positions()
    client.reconciliation()
    client.regime("SPY")
    client.evaluate_candidates("AAPL")
    client.risk_preview("AAPL", 5, "0.65")
    client.demo_what_if("AAPL", 5, "0.65", "2.15")
    client.demo_approval({"symbol": "AAPL"})
    assert ("GET", "/health") in calls
    assert ("GET", "/account/positions") in calls
    assert ("GET", "/strategy/regime/SPY") in calls
    assert ("POST", "/strategy/candidates/AAPL") in calls
    assert ("POST", "/strategy/risk-preview") in calls
    assert ("POST", "/strategy/demo-what-if") in calls
    assert ("POST", "/strategy/demo-approval") in calls


def test_post_body_carries_fields() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    client.risk_preview("AAPL", 42, "0.65")
    assert bodies[0] == {
        "symbol": "AAPL",
        "selected_contract_id": 42,
        "total_commission_estimate": "0.65",
    }


def test_401_maps_to_token_rejected() -> None:
    client = _client(lambda request: httpx.Response(401, json={"detail": "bad token"}))
    with pytest.raises(ApiClientError, match="token rejected"):
        client.health()


def test_403_maps_with_detail() -> None:
    client = _client(lambda request: httpx.Response(403, json={"detail": "not allowlisted"}))
    with pytest.raises(ApiClientError, match="not allowlisted"):
        client.regime("TSLA")


def test_409_maps_to_read_only() -> None:
    client = _client(lambda request: httpx.Response(409, json={"detail": "read-only"}))
    with pytest.raises(ApiClientError, match="read-only"):
        client.demo_approval({"symbol": "AAPL"})


def test_generic_error_status() -> None:
    client = _client(lambda request: httpx.Response(500, json={"detail": "boom"}))
    with pytest.raises(ApiClientError, match=r"failed \(500\)"):
        client.health()


def test_connect_error_maps_to_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)
    with pytest.raises(ApiClientError, match="not reachable"):
        client.health()


def test_non_json_body_raises() -> None:
    client = _client(lambda request: httpx.Response(200, text="not json"))
    with pytest.raises(ApiClientError, match="non-JSON"):
        client.health()


def test_positions_requires_array() -> None:
    client = _client(lambda request: httpx.Response(200, json={"not": "an array"}))
    with pytest.raises(ApiClientError, match="non-array"):
        client.positions()


def test_from_settings_missing_token_file(tmp_path: object) -> None:
    from chronos.config.settings import Settings

    settings = Settings(_env_file=None, backend_token_file=f"{tmp_path}/absent")  # type: ignore[arg-type]
    with pytest.raises(ApiClientError, match="token file not found"):
        ApiClient.from_settings(settings)
