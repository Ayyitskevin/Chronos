"""Fail-closed configuration for the TradingView signal bridge (ADR-0026).

:func:`load_config` reads the process environment once at startup and either
returns a complete configuration or raises. There is no partial start. A signal
bridge that boots with an empty allowlist and forwards everything "until it is
configured" is the exact shape of the four inert kernel defects this repository
was burned by (R-24..R-27): present, documented, and enforcing nothing.

Four defaults carry their reasoning here rather than in prose elsewhere:

- **``CHRONOS_TV_BRIDGE_FORWARD`` defaults to false.** Out of the box the bridge
  authenticates, parses, translates, and logs — and sends nothing. Forwarding is
  a separate deliberate act, the same shape as ``AUTONOMY_MANDATE_FILE`` being
  unset meaning "autonomy is inert". The owner should run it in this posture
  first and read what it *would* have proposed.
- **The listener binds loopback by default.** Reaching TradingView requires the
  owner to publish it deliberately (a tunnel, a reverse proxy), which is an
  owner act with its own risk, not something a default quietly does.
- **The ingress URL must be loopback.** Checked, not assumed. Without this the
  bridge is a general-purpose relay that will POST an owner-authenticated
  proposal wherever an environment variable points, which would turn a
  misconfiguration into an exfiltration path for trading intent.
- **The symbol and kind allowlists are required and may not be empty.** Deny by
  default is the house rule, and "no symbols configured" must mean "nothing is
  translatable", never "everything is".

The secret and the API token are read here and never logged, never returned in a
response body, and never included in the digest the bridge writes into the
proposal's evidence citation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from chronos.bridge.vocabulary import DECISION_KINDS, SYMBOL_ALPHABET

#: A shared secret travelling in a webhook body deserves real entropy. 32
#: characters is a low bar for a generated token and a high one for a
#: hand-typed password, which is the intended pressure.
MIN_SECRET_LENGTH: Final[int] = 32

#: Hosts the ingress URL may name. The backend binds loopback; a proposal POST
#: that leaves this machine is a configuration error, not a deployment option.
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

_DEFAULT_INGRESS_URL: Final[str] = "http://127.0.0.1:8000/autonomy/proposals"
_DEFAULT_HOST: Final[str] = "127.0.0.1"
_DEFAULT_PORT: Final[int] = 8109
_DEFAULT_MAX_ALERTS_PER_MINUTE: Final[int] = 10
_DEFAULT_MAX_ALERT_AGE_SECONDS: Final[int] = 120
_DEFAULT_REPLAY_WINDOW_SECONDS: Final[int] = 3600

_TRUE_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES: Final[frozenset[str]] = frozenset({"0", "false", "no", "off", ""})

_SYMBOL_SPLIT = re.compile(r"[,\s]+")


class BridgeConfigError(ValueError):
    """The environment does not describe a bridge that may safely start.

    Raised rather than defaulted. The message names the variable and the rule it
    broke, and never echoes a secret or token value.
    """


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    """Everything the bridge needs, all of it validated."""

    #: Shared secret TradingView must present in the alert body. TradingView
    #: cannot set custom request headers, so the body is the only channel
    #: available; it is compared in constant time and stripped before the alert
    #: is digested, logged, or translated.
    secret: str
    #: The backend's local API token, presented on the outbound proposal POST.
    api_token: str
    #: Where proposals are POSTed. Loopback-only, enforced.
    ingress_url: str
    #: Where the webhook listener binds.
    host: str
    port: int
    #: Symbols the bridge will translate. Never empty. This is defence in depth
    #: in front of the mandate's own scope, not a replacement for it.
    symbols: frozenset[str]
    #: Decision kinds the bridge will emit. Never empty.
    kinds: frozenset[str]
    #: Fixed-window ceiling on accepted alerts.
    max_alerts_per_minute: int
    #: How old an alert's own ``sent_at`` may be before it is refused as stale.
    max_alert_age_seconds: int
    #: How long an ``alert_id`` is remembered for replay refusal.
    replay_window_seconds: int
    #: False (the default) means translate and record but never POST.
    forward: bool


def load_config(environ: dict[str, str]) -> BridgeConfig:
    """Build a :class:`BridgeConfig`, or raise :class:`BridgeConfigError`."""

    secret = environ.get("CHRONOS_TV_BRIDGE_SECRET", "").strip()
    if len(secret) < MIN_SECRET_LENGTH:
        raise BridgeConfigError(
            "CHRONOS_TV_BRIDGE_SECRET must be set to at least "
            f"{MIN_SECRET_LENGTH} characters; the webhook body is the only channel "
            "TradingView can authenticate over, so this secret is the whole gate in "
            "front of the proposal queue"
        )

    api_token = environ.get("CHRONOS_TV_BRIDGE_API_TOKEN", "").strip()
    if not api_token:
        raise BridgeConfigError(
            "CHRONOS_TV_BRIDGE_API_TOKEN must be set to the backend's local API token "
            "(the contents of the api_token file); without it every forwarded proposal "
            "is refused with 401 and the bridge is a silent no-op"
        )

    ingress_url = environ.get("CHRONOS_TV_BRIDGE_INGRESS_URL", "").strip() or _DEFAULT_INGRESS_URL
    _require_loopback_url(ingress_url)

    symbols = _parse_allowlist(
        environ.get("CHRONOS_TV_BRIDGE_SYMBOLS", ""),
        variable="CHRONOS_TV_BRIDGE_SYMBOLS",
        explanation="which underlyings an alert may name",
    )
    for symbol in symbols:
        if not set(symbol) <= SYMBOL_ALPHABET:
            raise BridgeConfigError(
                f"CHRONOS_TV_BRIDGE_SYMBOLS contains {symbol!r}, which is not a symbol the "
                "decision contract accepts (A-Z, 0-9, '.', '-')"
            )

    kinds = _parse_allowlist(
        environ.get("CHRONOS_TV_BRIDGE_KINDS", ""),
        variable="CHRONOS_TV_BRIDGE_KINDS",
        explanation="which decision kinds an alert may produce",
    )
    unknown = sorted(kinds - DECISION_KINDS)
    if unknown:
        raise BridgeConfigError(
            f"CHRONOS_TV_BRIDGE_KINDS names {unknown}, which are not decision kinds; "
            f"valid kinds are {sorted(DECISION_KINDS)}"
        )

    return BridgeConfig(
        secret=secret,
        api_token=api_token,
        ingress_url=ingress_url,
        host=environ.get("CHRONOS_TV_BRIDGE_HOST", "").strip() or _DEFAULT_HOST,
        port=_positive_int(environ, "CHRONOS_TV_BRIDGE_PORT", _DEFAULT_PORT),
        symbols=symbols,
        kinds=kinds,
        max_alerts_per_minute=_positive_int(
            environ, "CHRONOS_TV_BRIDGE_MAX_ALERTS_PER_MINUTE", _DEFAULT_MAX_ALERTS_PER_MINUTE
        ),
        max_alert_age_seconds=_positive_int(
            environ, "CHRONOS_TV_BRIDGE_MAX_ALERT_AGE_SECONDS", _DEFAULT_MAX_ALERT_AGE_SECONDS
        ),
        replay_window_seconds=_positive_int(
            environ, "CHRONOS_TV_BRIDGE_REPLAY_WINDOW_SECONDS", _DEFAULT_REPLAY_WINDOW_SECONDS
        ),
        forward=_parse_bool(environ, "CHRONOS_TV_BRIDGE_FORWARD", default=False),
    )


def _require_loopback_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise BridgeConfigError(
            f"CHRONOS_TV_BRIDGE_INGRESS_URL must be an http(s) URL, got scheme {parts.scheme!r}"
        )
    if not parts.path:
        raise BridgeConfigError(
            "CHRONOS_TV_BRIDGE_INGRESS_URL must include the proposal path, "
            "e.g. http://127.0.0.1:8000/autonomy/proposals"
        )
    host = (parts.hostname or "").strip().lower()
    if host not in _LOOPBACK_HOSTS:
        raise BridgeConfigError(
            f"CHRONOS_TV_BRIDGE_INGRESS_URL points at {host!r}, which is not loopback. The "
            "bridge posts an owner-authenticated proposal carrying the backend's API token; "
            "it may only ever hand that to a backend on this machine"
        )


def _parse_allowlist(raw: str, *, variable: str, explanation: str) -> frozenset[str]:
    entries = frozenset(item.strip().upper() for item in _SYMBOL_SPLIT.split(raw) if item.strip())
    if not entries:
        raise BridgeConfigError(
            f"{variable} must be set and non-empty: it declares {explanation}. An empty "
            "allowlist means nothing is translatable, and the bridge refuses to start rather "
            "than run as an allow-everything relay"
        )
    return entries


def _positive_int(environ: dict[str, str], name: str, default: int) -> int:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise BridgeConfigError(f"{name} must be an integer, got {raw!r}") from error
    if value <= 0:
        raise BridgeConfigError(f"{name} must be positive, got {value}")
    return value


def _parse_bool(environ: dict[str, str], name: str, *, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    permitted = sorted((_TRUE_VALUES | _FALSE_VALUES) - {""})
    raise BridgeConfigError(f"{name} must be one of {permitted}, got {raw!r}")
