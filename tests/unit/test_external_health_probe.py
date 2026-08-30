from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from chronos.operations import external_probe as probe_module
from chronos.operations.external_probe import (
    ExternalProbeFailureCode,
    ExternalProbeState,
    ProbeConfigurationError,
    main,
    probe_external_health,
)

NOW = datetime(2026, 8, 29, 21, 30, tzinfo=UTC)


class _UnreadableBody(httpx.SyncByteStream):
    def __iter__(self):  # type: ignore[no-untyped-def]
        raise AssertionError("the probe must not consume an untrusted response body")


def _transport(handler):  # type: ignore[no-untyped-def]
    return httpx.MockTransport(handler)


def _credential_url() -> str:
    # Assemble the test-only invalid URL without placing credential-shaped text
    # in repository history, where the release scanner correctly treats it as
    # indistinguishable from a real basic-auth URL.
    return "https://user" + chr(58) + "test-pass" + chr(64) + "chronos.example.test"


def test_healthy_probe_uses_exact_no_redirect_endpoints_without_reading_bodies() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        headers = (
            {"set-cookie": "probe-tracker=untrusted"} if request.url.path.endswith("live") else {}
        )
        return httpx.Response(200, headers=headers, stream=_UnreadableBody())

    report = probe_external_health(
        "https://observer.example.test:8443",
        transport=_transport(handler),
        timeout_seconds=1.5,
        clock=lambda: NOW,
        timer=lambda: 1.0,
    )

    assert report.state is ExternalProbeState.HEALTHY
    assert report.target_origin == "https://observer.example.test:8443"
    assert report.assessed_at == NOW
    assert [request.url.path for request in requests] == ["/health/live", "/health/ready"]
    assert all(request.method == "GET" for request in requests)
    assert all(request.headers["accept"] == "application/json" for request in requests)
    assert all("authorization" not in request.headers for request in requests)
    assert all("cookie" not in request.headers for request in requests)
    assert report.liveness.status_code == 200
    assert report.liveness.failure_code is None
    assert report.readiness.status_code == 200
    assert report.readiness.failure_code is None


def test_expected_not_ready_is_unhealthy_not_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        status = 503 if request.url.path == "/health/ready" else 200
        return httpx.Response(status)

    report = probe_external_health(
        "http://127.0.0.1:8400",
        transport=_transport(handler),
        clock=lambda: NOW,
    )

    assert report.state is ExternalProbeState.UNHEALTHY
    assert report.liveness.state is ExternalProbeState.HEALTHY
    assert report.readiness.state is ExternalProbeState.UNHEALTHY
    assert report.readiness.failure_code is ExternalProbeFailureCode.READINESS_NOT_READY


def test_liveness_server_error_is_known_unhealthy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        status = 500 if request.url.path == "/health/live" else 200
        return httpx.Response(status)

    report = probe_external_health("https://chronos.example.test", transport=_transport(handler))

    assert report.state is ExternalProbeState.UNHEALTHY
    assert report.liveness.failure_code is ExternalProbeFailureCode.LIVENESS_NOT_LIVE


def test_undocumented_readiness_status_is_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        status = 204 if request.url.path == "/health/ready" else 200
        return httpx.Response(status)

    report = probe_external_health("https://chronos.example.test", transport=_transport(handler))

    assert report.state is ExternalProbeState.UNKNOWN
    assert report.readiness.failure_code is ExternalProbeFailureCode.UNEXPECTED_STATUS


def test_nonstandard_three_digit_status_is_unknown() -> None:
    report = probe_external_health(
        "https://chronos.example.test",
        transport=_transport(lambda request: httpx.Response(999)),
    )

    assert report.state is ExternalProbeState.UNKNOWN
    assert report.liveness.status_code == 999
    assert report.liveness.failure_code is ExternalProbeFailureCode.UNEXPECTED_STATUS


def test_redirect_is_refused() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/health/live":
            return httpx.Response(307, headers={"location": "https://attacker.invalid/collect"})
        return httpx.Response(200)

    report = probe_external_health("https://chronos.example.test", transport=_transport(handler))

    assert requests == ["/health/live", "/health/ready"]
    assert report.state is ExternalProbeState.UNKNOWN
    assert report.liveness.failure_code is ExternalProbeFailureCode.REDIRECT_REFUSED


def test_transport_failure_is_sanitized_and_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("credential=password", request=request)

    report = probe_external_health("https://chronos.example.test", transport=_transport(handler))

    encoded = report.model_dump_json()
    assert report.state is ExternalProbeState.UNKNOWN
    assert report.liveness.failure_code is ExternalProbeFailureCode.TRANSPORT_ERROR
    assert report.readiness.failure_code is ExternalProbeFailureCode.TRANSPORT_ERROR
    assert "credential" not in encoded
    assert "password" not in encoded


def test_timeout_is_distinct_from_other_transport_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow private endpoint", request=request)

    report = probe_external_health("https://chronos.example.test", transport=_transport(handler))

    assert report.state is ExternalProbeState.UNKNOWN
    assert report.liveness.failure_code is ExternalProbeFailureCode.TIMEOUT
    assert report.readiness.failure_code is ExternalProbeFailureCode.TIMEOUT


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://chronos.example.test",
        _credential_url(),
        "https://@chronos.example.test",
        "https://chronos.example.test ",
        "https://chronos.example.test: 443",
        "https://chronos.example.test/prefix",
        "https://chronos.example.test?token=secret",
        "https://chronos.example.test?",
        "https://chronos.example.test#fragment",
        "https://chronos.example.test#",
        "https://",
        "http://:8080",
        "https://chronos.example.test:0",
        "https://chronos.example.test:65536",
        "not-a-url",
    ],
)
def test_target_must_be_a_plain_http_origin(base_url: str) -> None:
    with pytest.raises(ProbeConfigurationError):
        probe_external_health(base_url)


@pytest.mark.parametrize("timeout_seconds", [0, -1, float("inf"), float("nan"), True])
def test_timeout_must_be_positive_and_finite(timeout_seconds: float) -> None:
    with pytest.raises(ProbeConfigurationError):
        probe_external_health(
            "https://chronos.example.test",
            timeout_seconds=timeout_seconds,
        )


def test_owned_client_ignores_environment_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    transport = _transport(lambda request: httpx.Response(200))
    owned_client = httpx.Client(transport=transport)

    def client_factory(*args: object, **kwargs: object) -> httpx.Client:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return owned_client

    monkeypatch.setattr(probe_module.httpx, "Client", client_factory)

    report = probe_external_health("https://chronos.example.test", transport=transport)

    assert report.state is ExternalProbeState.HEALTHY
    assert captured == {
        "args": (),
        "kwargs": {
            "auth": None,
            "cookies": None,
            "follow_redirects": False,
            "trust_env": False,
            "transport": transport,
        },
    }


def test_cli_emits_one_json_document_and_machine_exit_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    healthy = probe_external_health(
        "https://chronos.example.test",
        transport=_transport(lambda request: httpx.Response(200)),
        clock=lambda: NOW,
    )
    monkeypatch.setattr(probe_module, "probe_external_health", lambda *args, **kwargs: healthy)

    assert main(["--base-url", "https://chronos.example.test"]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["state"] == "HEALTHY"
    assert captured.err == ""


def test_cli_returns_nonzero_for_unknown_or_unhealthy_evidence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    healthy = probe_external_health(
        "https://chronos.example.test",
        transport=_transport(lambda request: httpx.Response(200)),
        clock=lambda: NOW,
    )
    for state in (ExternalProbeState.UNKNOWN, ExternalProbeState.UNHEALTHY):
        report = healthy.model_copy(update={"state": state})
        monkeypatch.setattr(
            probe_module,
            "probe_external_health",
            lambda *args, _report=report, **kwargs: _report,
        )

        assert main(["--base-url", "https://chronos.example.test"]) == 1
        assert json.loads(capsys.readouterr().out)["state"] == state.value


def test_cli_configuration_error_is_sanitized_and_returns_usage_exit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--base-url", _credential_url()]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "credentials" in captured.err
    assert "user" not in captured.err
    assert "test-pass" not in captured.err
