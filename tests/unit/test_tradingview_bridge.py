"""The bridge's configuration and transport behaviour.

The contract-agreement proofs live in
``tests/safety/test_tradingview_bridge_exercised.py``; this file covers the two
halves that are the bridge's own: a configuration layer that refuses to start
rather than run with a gap, and a listener whose refusals (rate, freshness,
replay, dry run) fire before anything is forwarded.

Every test here runs against an injected forwarder, so the suite never opens a
socket and never needs a backend — which is also the property that lets the
forwarding path be asserted at all.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from chronos.bridge.app import AttestResult, ForwardResult, create_app
from chronos.bridge.config import BridgeConfig, BridgeConfigError, load_config

SECRET = "s" * 40
NOW = datetime(2026, 8, 12, 14, 30, tzinfo=UTC)


def _env(**overrides: str) -> dict[str, str]:
    environ = {
        "CHRONOS_TV_BRIDGE_SECRET": SECRET,
        "CHRONOS_TV_BRIDGE_API_TOKEN": "abc123",
        "CHRONOS_TV_BRIDGE_SYMBOLS": "SPY,IWM",
        "CHRONOS_TV_BRIDGE_KINDS": "OPEN,CLOSE",
    }
    environ.update(overrides)
    return {name: value for name, value in environ.items() if value != ""}


def _config(**overrides: object) -> BridgeConfig:
    base: dict[str, object] = {
        "secret": SECRET,
        "api_token": "abc123",
        "proposer_token": "",
        "ingress_url": "http://127.0.0.1:8000/autonomy/proposals",
        "evidence_url": "http://127.0.0.1:8000/autonomy/evidence",
        "host": "127.0.0.1",
        "port": 8109,
        "symbols": frozenset({"SPY"}),
        "kinds": frozenset({"OPEN", "HOLD"}),
        "max_alerts_per_minute": 10,
        "max_alert_age_seconds": 120,
        "replay_window_seconds": 3600,
        "forward": True,
    }
    base.update(overrides)
    return BridgeConfig(**base)  # type: ignore[arg-type]


def _alert(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "secret": SECRET,
        "alert_id": "spy-001",
        "sent_at": NOW.isoformat(),
        "action": "OPEN",
        "symbol": "SPY",
        "direction": "LONG",
        "quantity": "5",
        "strategy": "LONG_EQUITY",
        "invalidation": ["close below the 20-day moving average"],
    }
    payload.update(overrides)
    return payload


class _Recorder:
    """An injected forwarder that records instead of sending."""

    def __init__(self, result: ForwardResult | None = None) -> None:
        self.sent: list[bytes] = []
        self._result = result or ForwardResult(forwarded=True, status_code=202)

    async def __call__(self, payload: bytes) -> ForwardResult:
        self.sent.append(payload)
        return self._result


class _Attester:
    """A backend whose evidence binding is off (ADR-0028's unset posture).

    Injected by default so every test in this file keeps its original subject:
    with binding disabled the bridge cites exactly what it cited before ADR-0028,
    and the attestation path changes nothing else. A test that is *about*
    attestation passes its own. Without an injected attester the real HTTP one
    would run and try to open a socket, which is precisely what these tests exist
    not to do.
    """

    def __init__(self, result: AttestResult | None = None) -> None:
        self.digests: list[str] = []
        self._result = result or AttestResult(binding_disabled=True)

    async def __call__(self, digest: str) -> AttestResult:
        self.digests.append(digest)
        return self._result


def _client(
    config: BridgeConfig | None = None,
    *,
    forwarder: _Recorder | None = None,
    attester: _Attester | None = None,
    now: datetime = NOW,
) -> tuple[TestClient, _Recorder]:
    recorder = forwarder or _Recorder()
    app = create_app(
        config or _config(),
        forwarder=recorder,
        attester=attester or _Attester(),
        clock=lambda: now,
    )
    return TestClient(app), recorder


# ------------------------------------------------------------------- configuration


def test_a_missing_secret_refuses_to_start() -> None:
    with pytest.raises(BridgeConfigError, match="CHRONOS_TV_BRIDGE_SECRET"):
        load_config(_env(CHRONOS_TV_BRIDGE_SECRET=""))


def test_a_short_secret_refuses_to_start() -> None:
    with pytest.raises(BridgeConfigError, match="at least"):
        load_config(_env(CHRONOS_TV_BRIDGE_SECRET="short"))


def test_a_missing_api_token_refuses_to_start() -> None:
    with pytest.raises(BridgeConfigError, match="API token"):
        load_config(_env(CHRONOS_TV_BRIDGE_API_TOKEN=""))


def test_an_empty_symbol_allowlist_refuses_to_start() -> None:
    """Deny by default: no symbols must mean nothing, never everything."""

    with pytest.raises(BridgeConfigError, match="CHRONOS_TV_BRIDGE_SYMBOLS"):
        load_config(_env(CHRONOS_TV_BRIDGE_SYMBOLS=""))


def test_an_empty_kind_allowlist_refuses_to_start() -> None:
    with pytest.raises(BridgeConfigError, match="CHRONOS_TV_BRIDGE_KINDS"):
        load_config(_env(CHRONOS_TV_BRIDGE_KINDS=""))


def test_an_unknown_kind_refuses_to_start() -> None:
    with pytest.raises(BridgeConfigError, match="not decision kinds"):
        load_config(_env(CHRONOS_TV_BRIDGE_KINDS="OPEN,LIQUIDATE_EVERYTHING"))


def test_a_non_loopback_ingress_url_refuses_to_start() -> None:
    """The bridge posts the backend's own API token; it may only hand it locally."""

    with pytest.raises(BridgeConfigError, match="not loopback"):
        load_config(_env(CHRONOS_TV_BRIDGE_INGRESS_URL="https://example.com/autonomy/proposals"))


def test_forwarding_is_off_unless_the_owner_turns_it_on() -> None:
    assert load_config(_env()).forward is False
    assert load_config(_env(CHRONOS_TV_BRIDGE_FORWARD="true")).forward is True


def test_the_listener_binds_loopback_unless_the_owner_changes_it() -> None:
    assert load_config(_env()).host == "127.0.0.1"


# ------------------------------------------------------------------------ transport


def test_a_valid_alert_is_forwarded_to_the_ingress() -> None:
    client, recorder = _client()

    response = client.post("/tradingview/webhook", json=_alert())

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert len(recorder.sent) == 1
    assert b"SPY" in recorder.sent[0]


def test_the_forwarded_payload_never_carries_the_secret() -> None:
    client, recorder = _client()

    client.post("/tradingview/webhook", json=_alert())

    assert SECRET.encode() not in recorder.sent[0]


def test_dry_run_translates_and_sends_nothing() -> None:
    """The shipped default: everything except the POST."""

    client, recorder = _client(_config(forward=False))

    response = client.post("/tradingview/webhook", json=_alert())

    assert response.status_code == 202
    assert response.json()["refusal"] == "FORWARDING_DISABLED"
    assert recorder.sent == []


def test_a_wrong_secret_is_401_and_forwards_nothing() -> None:
    client, recorder = _client()

    response = client.post("/tradingview/webhook", json=_alert(secret="w" * 40))

    assert response.status_code == 401
    assert recorder.sent == []


def test_a_duplicate_alert_id_is_refused_and_forwarded_once() -> None:
    client, recorder = _client()

    first = client.post("/tradingview/webhook", json=_alert())
    second = client.post("/tradingview/webhook", json=_alert())

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["refusal"] == "DUPLICATE_ALERT"
    assert len(recorder.sent) == 1


def test_a_stale_alert_is_refused_and_forwards_nothing() -> None:
    """A body captured and replayed later must not become a proposal."""

    client, recorder = _client(now=NOW + timedelta(minutes=30))

    response = client.post("/tradingview/webhook", json=_alert())

    assert response.status_code == 422
    assert response.json()["refusal"] == "STALE_ALERT"
    assert recorder.sent == []


def test_an_alert_timestamped_in_the_future_is_refused() -> None:
    client, recorder = _client(now=NOW - timedelta(minutes=30))

    response = client.post("/tradingview/webhook", json=_alert())

    assert response.status_code == 422
    assert response.json()["refusal"] == "ALERT_FROM_THE_FUTURE"
    assert recorder.sent == []


def test_the_rate_limit_fires_and_stops_forwarding() -> None:
    client, recorder = _client(_config(max_alerts_per_minute=2))

    codes = [
        client.post("/tradingview/webhook", json=_alert(alert_id=f"spy-{index}")).status_code
        for index in range(4)
    ]

    assert codes == [202, 202, 429, 429]
    assert len(recorder.sent) == 2


def test_a_symbol_outside_the_allowlist_is_refused_before_forwarding() -> None:
    client, recorder = _client()

    response = client.post("/tradingview/webhook", json=_alert(symbol="TSLA"))

    assert response.status_code == 422
    assert response.json()["refusal"] == "NOT_TRANSLATABLE"
    assert recorder.sent == []


def test_a_kind_outside_the_allowlist_is_refused_before_forwarding() -> None:
    client, recorder = _client()

    response = client.post(
        "/tradingview/webhook",
        json=_alert(action="CLOSE", strategy=None, invalidation=None),
    )

    assert response.status_code == 422
    assert recorder.sent == []


def test_an_ingress_refusal_is_reported_rather_than_swallowed() -> None:
    """The owner has to be able to see that the backend said no."""

    recorder = _Recorder(
        ForwardResult(
            forwarded=False,
            status_code=422,
            refusal="MALFORMED_PROPOSAL",
            detail="rejected field(s): symbol",
        )
    )
    client, _ = _client(forwarder=recorder)

    response = client.post("/tradingview/webhook", json=_alert())

    assert response.status_code == 422
    assert response.json()["refusal"] == "MALFORMED_PROPOSAL"


def test_an_unreachable_backend_is_a_bad_gateway_not_an_acceptance() -> None:
    recorder = _Recorder(
        ForwardResult(forwarded=False, status_code=0, refusal="INGRESS_UNREACHABLE")
    )
    client, _ = _client(forwarder=recorder)

    response = client.post("/tradingview/webhook", json=_alert())

    assert response.status_code == 502
    assert response.json()["accepted"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("alert_id", "REFLECT-ME-PLEASE"),
        ("symbol", "TSLA"),
        ("thesis", "REFLECT-ME-PLEASE"),
    ],
)
def test_the_response_never_echoes_alert_content(field: str, value: str) -> None:
    """The response is written into TradingView's log, outside the trust boundary.

    The invariant is the strict one — the response quotes nothing the sender
    supplied, not even a value drawn from Chronos's own closed vocabulary —
    because that is the version a test can enforce without a judgement call at
    every new message. The specifics an owner needs go to the bridge's log.
    """

    client, _ = _client()

    # Always an out-of-allowlist symbol, so the request is refused and any
    # echo would land in the refusal detail.
    overrides: dict[str, object] = {"symbol": "TSLA"}
    overrides[field] = value
    response = client.post("/tradingview/webhook", json=_alert(**overrides))

    assert value not in response.text
    assert "TSLA" not in response.text


def test_health_reports_posture_without_leaking_configuration() -> None:
    client, _ = _client(_config(forward=False))

    body = client.get("/health").json()

    assert body == {"status": "ok", "forwarding": "off"}


def test_the_bridge_exposes_no_route_beyond_the_webhook_and_health() -> None:
    """A listener on the public internet gets exactly two doors.

    Asserted through the client rather than by walking ``app.routes``: the route
    table's internal shape is a FastAPI implementation detail that has already
    changed between versions, and what matters is what answers a request. In
    particular the interactive docs are off, so the bridge does not publish its
    own schema to whoever finds the URL.
    """

    client, _ = _client()

    assert client.get("/health").status_code == 200
    assert client.post("/tradingview/webhook", json=_alert()).status_code == 202
    for path in ("/docs", "/redoc", "/openapi.json", "/autonomy/proposals", "/"):
        assert client.get(path).status_code == 404, f"{path} must not exist on the bridge"


def _forward_once(
    monkeypatch: pytest.MonkeyPatch, config: BridgeConfig
) -> tuple[ForwardResult, httpx.Request]:
    """Run the REAL ``_http_forwarder`` against a mock transport, once.

    Every other test injects a recorder in place of the forwarder, which is
    what keeps this suite socketless — and also what left the real forwarder's
    header assembly with zero coverage. This drives the genuine code path;
    only the network is replaced.
    """

    from chronos.bridge import app as app_module

    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(202, json={"accepted": True, "refusal": "", "detail": ""})

    real_client = httpx.AsyncClient

    def _mocked_client(**kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_client(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(app_module.httpx, "AsyncClient", _mocked_client)
    result = asyncio.run(app_module._http_forwarder(config)(b"{}"))
    assert len(captured) == 1
    return result, captured[0]


def test_the_real_forwarder_sends_the_token_and_no_proposer_header_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, request = _forward_once(monkeypatch, _config())

    assert result.forwarded
    assert request.headers["X-Chronos-Token"] == "abc123"
    # Pre-registry posture: no credential configured, none invented.
    assert "X-Chronos-Proposer-Token" not in request.headers


def test_the_real_forwarder_carries_a_configured_proposer_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0023: both credentials ride along, so the bridge works under either
    backend posture without knowing which one the backend is in."""

    result, request = _forward_once(monkeypatch, _config(proposer_token="proposer-secret"))

    assert result.forwarded
    assert request.headers["X-Chronos-Proposer-Token"] == "proposer-secret"
    assert request.headers["X-Chronos-Token"] == "abc123"
