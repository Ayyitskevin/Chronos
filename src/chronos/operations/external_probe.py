"""Timeout-constrained one-shot observation of Chronos's health probes.

This module is intended to run outside the backend process, including from a
different host.  It performs read-only HTTP GETs and reports evidence; it does
not send alerts, retain state, restart services, or grant trading authority.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Literal

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from chronos.utils.time import utc_now


class _ProbeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExternalProbeState(StrEnum):
    HEALTHY = "HEALTHY"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


class ExternalProbeFailureCode(StrEnum):
    READINESS_NOT_READY = "readiness_not_ready"
    LIVENESS_NOT_LIVE = "liveness_not_live"
    REDIRECT_REFUSED = "redirect_refused"
    UNEXPECTED_STATUS = "unexpected_status"
    TIMEOUT = "timeout"
    TRANSPORT_ERROR = "transport_error"


class EndpointProbe(_ProbeModel):
    path: Literal["/health/live", "/health/ready"]
    state: ExternalProbeState
    # HTTP status lines are three digits. Values above the standardized 599
    # ceiling are still peer-controlled observations and must classify UNKNOWN
    # rather than escaping the closed report model.
    status_code: int | None = Field(default=None, ge=100, le=999)
    elapsed_ms: float = Field(ge=0, allow_inf_nan=False)
    failure_code: ExternalProbeFailureCode | None = None


class ExternalHealthReport(_ProbeModel):
    schema_version: Literal[1] = 1
    assessed_at: AwareDatetime
    target_origin: str
    state: ExternalProbeState
    liveness: EndpointProbe
    readiness: EndpointProbe


class ProbeConfigurationError(ValueError):
    """The operator supplied an unsafe or unsupported probe configuration."""


def _normalize_origin(base_url: str) -> httpx.URL:
    try:
        target = httpx.URL(base_url)
    except (TypeError, httpx.InvalidURL) as error:
        raise ProbeConfigurationError("target must be an absolute HTTP(S) origin") from error
    if target.scheme not in {"http", "https"} or not target.host:
        raise ProbeConfigurationError("target must be an absolute HTTP(S) origin")
    if target.port is not None and not 1 <= target.port <= 65535:
        raise ProbeConfigurationError("target origin port must be between 1 and 65535")
    if target.userinfo or "@" in base_url:
        raise ProbeConfigurationError("target origin must not contain credentials")
    # HTTPX normalizes an empty query or fragment to an empty value while
    # retaining its delimiter in the serialized URL. Reject the raw delimiters
    # too so the accepted value is exactly an origin, not an origin-shaped URL.
    if (
        target.path not in {"", "/"}
        or target.query
        or target.fragment
        or "?" in base_url
        or "#" in base_url
        or any(character.isspace() for character in base_url)
    ):
        raise ProbeConfigurationError(
            "target must be a plain origin without path, query, or fragment"
        )
    return target.copy_with(path="/")


def _elapsed_ms(started: float, *, timer: Callable[[], float]) -> float:
    return round(max(0.0, timer() - started) * 1000, 3)


def _failure(
    path: Literal["/health/live", "/health/ready"],
    code: ExternalProbeFailureCode,
    *,
    started: float,
    timer: Callable[[], float],
) -> EndpointProbe:
    return EndpointProbe(
        path=path,
        state=ExternalProbeState.UNKNOWN,
        elapsed_ms=_elapsed_ms(started, timer=timer),
        failure_code=code,
    )


def _classify_response(
    path: Literal["/health/live", "/health/ready"],
    status_code: int,
    *,
    started: float,
    timer: Callable[[], float],
) -> EndpointProbe:
    elapsed = _elapsed_ms(started, timer=timer)
    if 300 <= status_code < 400:
        return EndpointProbe(
            path=path,
            state=ExternalProbeState.UNKNOWN,
            status_code=status_code,
            elapsed_ms=elapsed,
            failure_code=ExternalProbeFailureCode.REDIRECT_REFUSED,
        )
    if status_code == 200:
        return EndpointProbe(
            path=path,
            state=ExternalProbeState.HEALTHY,
            status_code=status_code,
            elapsed_ms=elapsed,
        )
    if path == "/health/ready" and status_code == 503:
        return EndpointProbe(
            path=path,
            state=ExternalProbeState.UNHEALTHY,
            status_code=status_code,
            elapsed_ms=elapsed,
            failure_code=ExternalProbeFailureCode.READINESS_NOT_READY,
        )
    if path == "/health/live" and 400 <= status_code < 600:
        return EndpointProbe(
            path=path,
            state=ExternalProbeState.UNHEALTHY,
            status_code=status_code,
            elapsed_ms=elapsed,
            failure_code=ExternalProbeFailureCode.LIVENESS_NOT_LIVE,
        )
    return EndpointProbe(
        path=path,
        state=ExternalProbeState.UNKNOWN,
        status_code=status_code,
        elapsed_ms=elapsed,
        failure_code=ExternalProbeFailureCode.UNEXPECTED_STATUS,
    )


def _probe_endpoint(
    client: httpx.Client,
    target: httpx.URL,
    path: Literal["/health/live", "/health/ready"],
    *,
    timeout_seconds: float,
    timer: Callable[[], float],
) -> EndpointProbe:
    started = timer()
    try:
        # Stream and close without reading. The health signal is the status code;
        # an untrusted peer cannot make the observer buffer an arbitrary body.
        with client.stream(
            "GET",
            target.join(path),
            headers={"Accept": "application/json", "User-Agent": "Chronos-Health-Probe/1"},
            timeout=timeout_seconds,
        ) as response:
            return _classify_response(
                path,
                response.status_code,
                started=started,
                timer=timer,
            )
    except httpx.TimeoutException:
        return _failure(
            path,
            ExternalProbeFailureCode.TIMEOUT,
            started=started,
            timer=timer,
        )
    except httpx.TransportError:
        return _failure(
            path,
            ExternalProbeFailureCode.TRANSPORT_ERROR,
            started=started,
            timer=timer,
        )


def _overall(*observations: EndpointProbe) -> ExternalProbeState:
    states = {observation.state for observation in observations}
    if ExternalProbeState.UNHEALTHY in states:
        return ExternalProbeState.UNHEALTHY
    if ExternalProbeState.UNKNOWN in states:
        return ExternalProbeState.UNKNOWN
    return ExternalProbeState.HEALTHY


def probe_external_health(
    base_url: str,
    *,
    timeout_seconds: float = 3.0,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], datetime] = utc_now,
    timer: Callable[[], float] = time.monotonic,
) -> ExternalHealthReport:
    """Observe liveness and service readiness without reading response bodies."""

    if (
        isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ProbeConfigurationError("timeout seconds must be a positive finite number")
    target = _normalize_origin(base_url)

    def observe(active_client: httpx.Client) -> ExternalHealthReport:
        liveness = _probe_endpoint(
            active_client,
            target,
            "/health/live",
            timeout_seconds=timeout_seconds,
            timer=timer,
        )
        # A hostile liveness response may set a cookie. Do not reflect that
        # untrusted state into the separate readiness request.
        active_client.cookies.clear()
        readiness = _probe_endpoint(
            active_client,
            target,
            "/health/ready",
            timeout_seconds=timeout_seconds,
            timer=timer,
        )
        return ExternalHealthReport(
            assessed_at=clock(),
            target_origin=str(target).removesuffix("/"),
            state=_overall(liveness, readiness),
            liveness=liveness,
            readiness=readiness,
        )

    # Proxy environment variables are intentionally ignored: a local/off-host
    # operational probe must connect to the named origin, never a silent proxy.
    # Always constructing the client here also prevents inherited auth/cookies.
    with httpx.Client(
        auth=None,
        cookies=None,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as owned_client:
        return observe(owned_client)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronos-health-probe",
        description="Read-only one-shot Chronos liveness/readiness observation",
    )
    parser.add_argument("--base-url", required=True, help="plain HTTP(S) origin of the backend")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=3.0,
        help="HTTPX network-inactivity timeout per endpoint (not a total deadline)",
    )
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = probe_external_health(
            args.base_url,
            timeout_seconds=args.timeout_seconds,
        )
    except ProbeConfigurationError as error:
        print(f"health probe configuration error: {error}", file=sys.stderr)
        return 2
    print(report.model_dump_json(indent=2 if args.pretty else None))
    return 0 if report.state is ExternalProbeState.HEALTHY else 1


if __name__ == "__main__":
    sys.exit(main())
