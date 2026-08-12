"""The webhook listener, and the single POST it is allowed to make (ADR-0026).

This is the only network-facing surface in the package. It accepts a TradingView
alert, refuses it for one of a bounded set of reasons, or forwards the translated
candidate to the loopback proposal ingress.

## The refusals, in the order they are applied

1. **Rate limit.** A fixed window ceiling on accepted alerts. TradingView can
   fire an alert on every bar close of every chart the owner has open; a signal
   source that suddenly becomes chatty must not become a proposal flood, and the
   supervisor's own ``proposals_per_tick`` bound is too far downstream to be the
   only answer.
2. **Size and structure.** Delegated to :mod:`chronos.bridge.alert`, which is
   written as though the sender were hostile because the sender may be.
3. **The shared secret**, in constant time, before any other field is judged.
4. **Freshness.** An alert older than the configured age is refused. A webhook
   body replayed hours later by someone who captured it is exactly the attack
   this bound exists for, and TradingView's own ``{{timenow}}`` makes it cheap.
5. **Replay.** An ``alert_id`` seen inside the replay window is refused. This is
   the near-duplicate defence; the far one is the supervisor's own economic-content
   ``decision_id``, which recognizes the same trade proposed twice even when the
   wrapper differs.
6. **Translation.** Allowlists and the contract's coherence rules.

Only an alert that survives all six is forwarded — and only when the owner has
turned forwarding on. In the default posture the bridge does everything except
the POST, and says so in the response.

## What the response body may say

Very little, deliberately. It names the stage and a refusal code, and it never
echoes alert content back to the caller. The response is written into
TradingView's alert log, which is outside Chronos's trust boundary, so it must
not become a channel for reflecting attacker-chosen text or for reporting what
the backend thinks about the owner's account.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import httpx
from fastapi import APIRouter, FastAPI, Request, Response, status
from pydantic import BaseModel

from chronos.bridge.alert import (
    MAX_ALERT_BYTES,
    AlertRejected,
    AlertUnauthorized,
    parse_alert,
)
from chronos.bridge.config import BridgeConfig
from chronos.bridge.translate import TranslationRefused, build_proposal
from chronos.utils.time import utc_now

_logger = logging.getLogger("chronos.bridge.tradingview")

#: The backend's local API token header. Restated rather than imported from
#: ``chronos.api.auth``: importing the backend's auth module would pull the
#: whole app plane into a process whose value is that it holds nothing.
#: ``tests/safety/test_tradingview_bridge_isolation.py`` pins the two equal.
TOKEN_HEADER: Final[str] = "X-Chronos-Token"

#: The proposer-credential header (ADR-0023), sent when the bridge is
#: registered. Restated for the same reason as ``TOKEN_HEADER``, and pinned
#: equal by the same isolation suite.
PROPOSER_HEADER: Final[str] = "X-Chronos-Proposer-Token"

#: How long the outbound proposal POST may take. Short: the backend is on
#: loopback and only enqueues, so a slow answer means something is wrong rather
#: than something is busy.
FORWARD_TIMEOUT_SECONDS: Final[float] = 10.0

#: A bound on the replay cache regardless of window length, so a flood of
#: distinct ids cannot grow it without limit.
MAX_REMEMBERED_ALERTS: Final[int] = 4096

Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class ForwardResult:
    """What the proposal ingress said."""

    forwarded: bool
    status_code: int
    refusal: str = ""
    detail: str = ""


Forwarder = Callable[[bytes], Awaitable[ForwardResult]]


class WebhookResult(BaseModel):
    """The narrow answer TradingView receives.

    No account identifier, no broker state, no evidence content, and nothing the
    caller sent. ``accepted`` means "handed to the proposal queue", which is
    received rather than authorized — nothing has been judged or sent.
    """

    accepted: bool
    stage: str
    refusal: str = ""
    detail: str = ""


class _RateLimiter:
    """A fixed-window ceiling on accepted alerts, per process."""

    def __init__(self, *, per_minute: int) -> None:
        self._per_minute = per_minute
        self._events: deque[datetime] = deque()

    def allow(self, now: datetime) -> bool:
        cutoff = now - timedelta(minutes=1)
        while self._events and self._events[0] < cutoff:
            self._events.popleft()
        if len(self._events) >= self._per_minute:
            return False
        self._events.append(now)
        return True


class _ReplayCache:
    """Remembers alert ids for a bounded window, and refuses a repeat."""

    def __init__(self, *, window_seconds: int) -> None:
        self._window = timedelta(seconds=window_seconds)
        self._seen: dict[str, datetime] = {}

    def remember(self, alert_id: str, now: datetime) -> bool:
        """True when this id is new. False means it is a replay."""

        self._evict(now)
        if alert_id in self._seen:
            return False
        if len(self._seen) >= MAX_REMEMBERED_ALERTS:
            oldest = min(self._seen, key=lambda key: self._seen[key])
            del self._seen[oldest]
        self._seen[alert_id] = now
        return True

    def _evict(self, now: datetime) -> None:
        cutoff = now - self._window
        stale = [key for key, seen_at in self._seen.items() if seen_at < cutoff]
        for key in stale:
            del self._seen[key]


def create_app(
    config: BridgeConfig,
    *,
    forwarder: Forwarder | None = None,
    clock: Clock = utc_now,
) -> FastAPI:
    """Build the bridge's ASGI app.

    ``forwarder`` is injectable so tests exercise the whole path — including the
    refusals — without a network or a running backend. The default forwarder is
    the only code in this package that opens a socket.
    """

    app = FastAPI(
        title="Chronos TradingView bridge",
        description=(
            "Translates TradingView alerts into candidate proposals for the Chronos "
            "autonomy ingress. Holds no trading authority."
        ),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    send = forwarder if forwarder is not None else _http_forwarder(config)
    limiter = _RateLimiter(per_minute=config.max_alerts_per_minute)
    replays = _ReplayCache(window_seconds=config.replay_window_seconds)
    router = APIRouter()

    @router.get("/health")
    def health() -> dict[str, str]:
        """Liveness only. Says nothing about the backend or the account."""

        return {"status": "ok", "forwarding": "on" if config.forward else "off"}

    @router.post(
        "/tradingview/webhook",
        response_model=WebhookResult,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def webhook(request: Request, response: Response) -> WebhookResult:
        now = clock()

        if not limiter.allow(now):
            _logger.warning(
                "TradingView alert refused: rate limit of %d/min reached",
                config.max_alerts_per_minute,
                extra={"event": "bridge_rate_limited"},
            )
            response.status_code = status.HTTP_429_TOO_MANY_REQUESTS
            return WebhookResult(
                accepted=False,
                stage="RATE_LIMIT",
                refusal="RATE_LIMITED",
                detail=f"at most {config.max_alerts_per_minute} alerts per minute are accepted",
            )

        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_ALERT_BYTES:
            response.status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            return WebhookResult(
                accepted=False,
                stage="PARSE",
                refusal="PAYLOAD_TOO_LARGE",
                detail=f"an alert must be under {MAX_ALERT_BYTES} bytes",
            )

        body = await request.body()
        try:
            alert = parse_alert(body, expected_secret=config.secret)
        except AlertUnauthorized as error:
            _logger.warning(
                "TradingView alert refused: unauthorized",
                extra={"event": "bridge_unauthorized"},
            )
            response.status_code = status.HTTP_401_UNAUTHORIZED
            return WebhookResult(
                accepted=False, stage="AUTH", refusal="UNAUTHORIZED", detail=str(error)
            )
        except AlertRejected as error:
            response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
            return WebhookResult(
                accepted=False, stage="PARSE", refusal="MALFORMED_ALERT", detail=str(error)
            )

        age = (now - alert.sent_at).total_seconds()
        if age > config.max_alert_age_seconds:
            _refused(alert.alert_id, "STALE_ALERT")
            response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
            return WebhookResult(
                accepted=False,
                stage="FRESHNESS",
                refusal="STALE_ALERT",
                detail=(
                    f"the alert is {int(age)}s old, over the {config.max_alert_age_seconds}s limit"
                ),
            )
        if age < -config.max_alert_age_seconds:
            _refused(alert.alert_id, "ALERT_FROM_THE_FUTURE")
            response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
            return WebhookResult(
                accepted=False,
                stage="FRESHNESS",
                refusal="ALERT_FROM_THE_FUTURE",
                detail="the alert is timestamped further ahead than the clock skew allowance",
            )

        if not replays.remember(alert.alert_id, now):
            _refused(alert.alert_id, "DUPLICATE_ALERT")
            response.status_code = status.HTTP_409_CONFLICT
            return WebhookResult(
                accepted=False,
                stage="REPLAY",
                refusal="DUPLICATE_ALERT",
                detail="this alert_id was already accepted inside the replay window",
            )

        try:
            proposal = build_proposal(alert, config)
        except TranslationRefused as error:
            # The specifics go here, inside the trust boundary, and never into
            # the response: alert_id and symbol are both bounded to safe
            # alphabets by the parser, so a log line cannot be forged with them.
            _logger.warning(
                "TradingView alert %s (%s %s) refused: NOT_TRANSLATABLE — %s",
                alert.alert_id,
                alert.action,
                alert.symbol,
                error,
                extra={"event": "bridge_refused", "alert_id": alert.alert_id},
            )
            response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
            return WebhookResult(
                accepted=False, stage="TRANSLATE", refusal="NOT_TRANSLATABLE", detail=str(error)
            )

        payload = json.dumps(proposal, separators=(",", ":")).encode("utf-8")

        if not config.forward:
            _logger.info(
                "TradingView alert %s translated; forwarding is off so nothing was sent",
                alert.alert_id,
                extra={"event": "bridge_dry_run", "alert_id": alert.alert_id},
            )
            return WebhookResult(
                accepted=False,
                stage="DRY_RUN",
                refusal="FORWARDING_DISABLED",
                detail=(
                    "the alert translated cleanly and was NOT sent; set "
                    "CHRONOS_TV_BRIDGE_FORWARD=true to hand alerts to the proposal queue"
                ),
            )

        result = await send(payload)
        if not result.forwarded:
            _logger.error(
                "TradingView alert %s was not accepted by the ingress: %s",
                alert.alert_id,
                result.refusal,
                extra={"event": "bridge_forward_refused", "alert_id": alert.alert_id},
            )
            response.status_code = (
                status.HTTP_502_BAD_GATEWAY
                if result.status_code == 0
                else status.HTTP_422_UNPROCESSABLE_ENTITY
            )
            return WebhookResult(
                accepted=False,
                stage="INGRESS",
                refusal=result.refusal or "INGRESS_REFUSED",
                detail=result.detail,
            )

        _logger.info(
            "TradingView alert %s queued as a proposal",
            alert.alert_id,
            extra={"event": "bridge_forwarded", "alert_id": alert.alert_id},
        )
        return WebhookResult(
            accepted=True,
            stage="QUEUED",
            detail=(
                "the proposal was handed to the Chronos queue. Queued is received, not "
                "authorized — the supervisor judges it on its own tick and may refuse it"
            ),
        )

    app.include_router(router)
    return app


def _refused(alert_id: str, refusal: str) -> None:
    _logger.warning(
        "TradingView alert %s refused: %s",
        alert_id,
        refusal,
        extra={"event": "bridge_refused", "alert_id": alert_id},
    )


def _http_forwarder(config: BridgeConfig) -> Forwarder:
    """The one outbound call this package makes: POST to the loopback ingress.

    A client per request is wasteful and correct at this volume — the rate limit
    is single-digit alerts per minute — and it keeps the package free of
    process-lifetime state that a test would have to unwind.
    """

    async def _forward(payload: bytes) -> ForwardResult:
        # Both credentials ride along when both exist: the local token
        # satisfies a pre-registry backend, and a registry-on backend judges
        # the proposer credential alone (ADR-0023) — the bridge works under
        # either posture without knowing which one the backend is in.
        headers = {TOKEN_HEADER: config.api_token, "Content-Type": "application/json"}
        if config.proposer_token:
            headers[PROPOSER_HEADER] = config.proposer_token
        try:
            async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    config.ingress_url,
                    content=payload,
                    headers=headers,
                )
        except httpx.HTTPError:
            _logger.exception("Could not reach the Chronos proposal ingress")
            return ForwardResult(
                forwarded=False,
                status_code=0,
                refusal="INGRESS_UNREACHABLE",
                detail="the Chronos backend did not answer on loopback",
            )

        accepted = False
        refusal = ""
        detail = ""
        try:
            document = response.json()
        except ValueError:
            document = {}
        if isinstance(document, dict):
            accepted = bool(document.get("accepted", False))
            refusal = str(document.get("refusal", ""))
            detail = str(document.get("detail", ""))
        return ForwardResult(
            forwarded=accepted,
            status_code=response.status_code,
            refusal=refusal or (f"INGRESS_HTTP_{response.status_code}" if not accepted else ""),
            detail=detail,
        )

    return _forward
