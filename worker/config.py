"""Fail-closed configuration for the model worker (ADR-0027).

Same discipline as the TradingView bridge: :func:`load_config` reads the
environment once and either returns a complete configuration or raises. There
is no partial start, because a decision worker that boots with an empty
allowlist "until it is configured" is the inert-control shape this repository
was burned by four times (R-24..R-27).

The two credentials this process holds are the most dangerous things in it:

- ``ANTHROPIC_API_KEY`` — never logged, never sent anywhere but
  ``api.anthropic.com``, never included in a proposal or an evidence digest.
- ``CHRONOS_WORKER_API_TOKEN`` — the backend's local API token. The backend URL
  is checked to be loopback so a misconfigured worker cannot hand it to a
  remote host.

``CHRONOS_WORKER_FORWARD`` defaults to false: out of the box the worker
gathers, thinks, and logs — and proposes nothing. Turning forwarding on is a
separate deliberate act, the same shape as ``AUTONOMY_MANDATE_FILE`` being
unset meaning autonomy is inert.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from worker.vocabulary import DECISION_KINDS, SYMBOL_ALPHABET

_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: The current default per the Claude API reference (2026-08). Overridable via
#: CHRONOS_WORKER_MODEL; the string is passed through verbatim, never guessed at.
_DEFAULT_MODEL: Final[str] = "claude-opus-5"

_DEFAULT_BACKEND_URL: Final[str] = "http://127.0.0.1:8000"
_DEFAULT_INTERVAL_SECONDS: Final[int] = 300
_DEFAULT_POLICY_FILE: Final[str] = "worker/policy.md"
_DEFAULT_LOOKBACK_DAYS: Final[int] = 30

_TRUE_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES: Final[frozenset[str]] = frozenset({"0", "false", "no", "off", ""})

_LIST_SPLIT = re.compile(r"[,\s]+")


class WorkerConfigError(ValueError):
    """The environment does not describe a worker that may safely start.

    The message names the variable and the rule it broke, and never echoes a
    credential value.
    """


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Everything the worker needs, all of it validated."""

    #: Anthropic API key. Lives only in this process's environment.
    anthropic_api_key: str
    #: The Claude model id used for decisions.
    model: str
    #: The backend's local API token, presented on every read and every POST.
    api_token: str
    #: Loopback-only backend base URL, no trailing slash.
    backend_url: str
    #: Symbols the worker may reason about and propose on. Never empty.
    symbols: tuple[str, ...]
    #: Decision kinds the worker may emit. Never empty.
    kinds: frozenset[str]
    #: The owner's editable trading policy, already read from disk.
    policy: str
    #: Seconds between cycles in loop mode.
    interval_seconds: int
    #: Daily bars of history fetched per symbol for the evidence snapshot.
    lookback_days: int
    #: False (the default) means think and log but never POST a proposal.
    forward: bool


def load_config(environ: dict[str, str]) -> WorkerConfig:
    """Build a :class:`WorkerConfig`, or raise :class:`WorkerConfigError`."""

    api_key = environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise WorkerConfigError(
            "ANTHROPIC_API_KEY must be set: the worker is the one process that talks to "
            "the model, and it cannot think without a key. The key belongs in THIS "
            "process's environment only — never the backend's"
        )

    token = environ.get("CHRONOS_WORKER_API_TOKEN", "").strip()
    if not token:
        raise WorkerConfigError(
            "CHRONOS_WORKER_API_TOKEN must be set to the backend's local API token; "
            "without it every evidence read and every proposal POST is refused with 401"
        )

    backend_url = environ.get("CHRONOS_WORKER_BACKEND_URL", "").strip() or _DEFAULT_BACKEND_URL
    backend_url = backend_url.rstrip("/")
    _require_loopback_url(backend_url)

    symbols = _parse_list(
        environ.get("CHRONOS_WORKER_SYMBOLS", ""),
        variable="CHRONOS_WORKER_SYMBOLS",
        explanation="the watchlist the worker reasons about",
    )
    for symbol in symbols:
        if not set(symbol) <= SYMBOL_ALPHABET:
            raise WorkerConfigError(
                f"CHRONOS_WORKER_SYMBOLS contains {symbol!r}, which is not a symbol the "
                "decision contract accepts (A-Z, 0-9, '.', '-')"
            )

    kinds = frozenset(
        _parse_list(
            environ.get("CHRONOS_WORKER_KINDS", ""),
            variable="CHRONOS_WORKER_KINDS",
            explanation="which decision kinds the worker may emit",
        )
    )
    unknown = sorted(kinds - DECISION_KINDS)
    if unknown:
        raise WorkerConfigError(
            f"CHRONOS_WORKER_KINDS names {unknown}, which are not decision kinds; "
            f"valid kinds are {sorted(DECISION_KINDS)}"
        )

    policy_path = Path(
        environ.get("CHRONOS_WORKER_POLICY_FILE", "").strip() or _DEFAULT_POLICY_FILE
    )
    try:
        policy = policy_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise WorkerConfigError(
            f"the policy file {policy_path} is unreadable: {error}. The policy is the "
            "owner's trading strategy in prose; the worker refuses to think without one"
        ) from error
    if not policy:
        raise WorkerConfigError(
            f"the policy file {policy_path} is empty; an empty policy is not a strategy"
        )

    return WorkerConfig(
        anthropic_api_key=api_key,
        model=environ.get("CHRONOS_WORKER_MODEL", "").strip() or _DEFAULT_MODEL,
        api_token=token,
        backend_url=backend_url,
        symbols=symbols,
        kinds=kinds,
        policy=policy,
        interval_seconds=_positive_int(
            environ, "CHRONOS_WORKER_INTERVAL_SECONDS", _DEFAULT_INTERVAL_SECONDS
        ),
        lookback_days=_positive_int(
            environ, "CHRONOS_WORKER_LOOKBACK_DAYS", _DEFAULT_LOOKBACK_DAYS
        ),
        forward=_parse_bool(environ, "CHRONOS_WORKER_FORWARD", default=False),
    )


def _require_loopback_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise WorkerConfigError(
            f"CHRONOS_WORKER_BACKEND_URL must be an http(s) URL, got scheme {parts.scheme!r}"
        )
    host = (parts.hostname or "").strip().lower()
    if host not in _LOOPBACK_HOSTS:
        raise WorkerConfigError(
            f"CHRONOS_WORKER_BACKEND_URL points at {host!r}, which is not loopback. The "
            "worker carries the backend's API token; it may only ever hand that to a "
            "backend on this machine"
        )


def _parse_list(raw: str, *, variable: str, explanation: str) -> tuple[str, ...]:
    entries = tuple(
        dict.fromkeys(item.strip().upper() for item in _LIST_SPLIT.split(raw) if item.strip())
    )
    if not entries:
        raise WorkerConfigError(
            f"{variable} must be set and non-empty: it declares {explanation}. Empty means "
            "nothing is proposable, and the worker refuses to start rather than guess"
        )
    return entries


def _positive_int(environ: dict[str, str], name: str, default: int) -> int:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise WorkerConfigError(f"{name} must be an integer, got {raw!r}") from error
    if value <= 0:
        raise WorkerConfigError(f"{name} must be positive, got {value}")
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
    raise WorkerConfigError(f"{name} must be one of {permitted}, got {raw!r}")
