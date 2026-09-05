"""Fail-closed configuration for the model worker (ADR-0027).

Same discipline as the TradingView bridge: :func:`load_config` reads the
environment once and either returns a complete configuration or raises. There
is no partial start, because a decision worker that boots with an empty
allowlist "until it is configured" is the inert-control shape this repository
was burned by four times (R-24..R-27).

The credentials this process holds are the most dangerous things in it:

- ``ANTHROPIC_API_KEY`` / ``XAI_API_KEY`` / ``CHRONOS_WORKER_LOCAL_API_KEY`` —
  never logged, never sent anywhere but the selected provider's API, never
  included in a proposal or an evidence digest. The unused providers' keys are
  not required and are not read. The local one is optional even when selected,
  because Ollama authenticates nothing; unset means no ``Authorization`` header
  is sent rather than an empty bearer.
- ``CHRONOS_WORKER_API_TOKEN`` — the backend's local API token. The backend URL
  is checked to be loopback so a misconfigured worker cannot hand it to a
  remote host.
- ``CHRONOS_WORKER_PROPOSER_TOKEN`` — optional: this worker's registered
  proposer credential (ADR-0023), minted by ``python -m chronos.cli proposer
  mint`` and required once the backend configures ``AUTONOMY_PROPOSERS_FILE``.
  Sent only on the proposal POST, only to the same loopback backend. Unset
  means the pre-registry posture; a registry-on backend will then refuse the
  proposal with a message naming this variable's header.

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

#: Selectable provider ids. ``local`` is any OpenAI-compatible server on this
#: machine — Ollama, or a gateway with the same shape.
_PROVIDERS: Final[frozenset[str]] = frozenset({"anthropic", "local", "xai"})
_DEFAULT_PROVIDER: Final[str] = "anthropic"
#: Provider id → default model. The string is passed through verbatim.
#: ``local`` is deliberately absent: a local roster changes without notice, so
#: there is no tag this code could guess that is not either missing or wrong.
_DEFAULT_MODELS: Final[dict[str, str]] = {
    "anthropic": "claude-opus-5",
    "xai": "grok-4.6",
}
_KEY_VARS: Final[dict[str, str]] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "local": "CHRONOS_WORKER_LOCAL_API_KEY",
    "xai": "XAI_API_KEY",
}
#: Providers that may run without a key. A local server usually authenticates
#: nothing; demanding a credential it does not have would be ceremony, and
#: inventing a placeholder to satisfy the check would be worse.
_KEYLESS_PROVIDERS: Final[frozenset[str]] = frozenset({"local"})

_DEFAULT_BACKEND_URL: Final[str] = "http://127.0.0.1:8765"
_DEFAULT_LOCAL_BASE_URL: Final[str] = "http://127.0.0.1:11434/v1"
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

    #: ``anthropic``, ``xai``, or ``local``. Selects which key binds, and which
    #: default model — ``local`` has none, and requires an explicit tag.
    provider: str
    #: Anthropic API key when ``provider`` is anthropic; empty otherwise.
    anthropic_api_key: str
    #: xAI console API key when ``provider`` is xai; empty otherwise.
    #: Never loaded from ``~/.grok/auth.json`` — that file is a TUI session.
    xai_api_key: str
    #: Bearer for a local gateway that wants one; empty is the ordinary
    #: posture, because Ollama authenticates nothing. Never defaulted in code.
    local_api_key: str
    #: The model id used for decisions. Provider default if unset.
    model: str
    #: The backend's local API token, presented on every read and every POST.
    api_token: str
    #: This worker's registered proposer credential (ADR-0023), presented on
    #: the proposal POST only. Empty is the pre-registry posture.
    proposer_token: str
    #: Loopback-only backend base URL, no trailing slash.
    backend_url: str
    #: Loopback-only base URL of the OpenAI-compatible local model server, no
    #: trailing slash. Loopback for a stronger reason than the backend's: the
    #: request body is the whole evidence snapshot.
    local_base_url: str
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
    #: Max model tokens (input + output) per UTC day. ``None`` means uncapped —
    #: today's behavior, disclosed at startup. At the ceiling cycles log
    #: ``COST_CEILING`` and skip thinking until the day rolls.
    max_daily_tokens: int | None

    @property
    def api_key(self) -> str:
        """The key for the selected provider. Empty if that provider is not bound.

        Empty is a legitimate answer for ``local`` and a broken one for the
        hosted providers, which :func:`load_config` refuses to build.
        """

        keys: dict[str, str] = {
            "anthropic": self.anthropic_api_key,
            "local": self.local_api_key,
            "xai": self.xai_api_key,
        }
        return keys.get(self.provider, "")


def load_config(environ: dict[str, str]) -> WorkerConfig:
    """Build a :class:`WorkerConfig`, or raise :class:`WorkerConfigError`."""

    provider = (environ.get("CHRONOS_WORKER_PROVIDER", "") or _DEFAULT_PROVIDER).strip().lower()
    if provider not in _PROVIDERS:
        raise WorkerConfigError(
            f"CHRONOS_WORKER_PROVIDER must be one of {sorted(_PROVIDERS)}, got {provider!r}"
        )

    key_var = _KEY_VARS[provider]
    api_key = environ.get(key_var, "").strip()
    if not api_key and provider not in _KEYLESS_PROVIDERS:
        raise WorkerConfigError(
            f"{key_var} must be set when CHRONOS_WORKER_PROVIDER={provider}: the worker "
            "is the one process that talks to the model, and it cannot think without a "
            "key. The key belongs in THIS process's environment only — never the "
            "backend's, and never a TUI session file"
        )

    token = environ.get("CHRONOS_WORKER_API_TOKEN", "").strip()
    if not token:
        raise WorkerConfigError(
            "CHRONOS_WORKER_API_TOKEN must be set to the backend's local API token; "
            "without it every evidence read and every proposal POST is refused with 401"
        )

    backend_url = environ.get("CHRONOS_WORKER_BACKEND_URL", "").strip() or _DEFAULT_BACKEND_URL
    backend_url = backend_url.rstrip("/")
    _require_loopback_url(
        backend_url,
        variable="CHRONOS_WORKER_BACKEND_URL",
        carries="the backend's API token",
    )

    local_base_url = (
        environ.get("CHRONOS_WORKER_LOCAL_BASE_URL", "").strip() or _DEFAULT_LOCAL_BASE_URL
    ).rstrip("/")
    _require_loopback_url(
        local_base_url,
        variable="CHRONOS_WORKER_LOCAL_BASE_URL",
        carries="the entire evidence snapshot — cash, buying power, positions, open orders",
    )

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
        provider=provider,
        anthropic_api_key=api_key if provider == "anthropic" else "",
        xai_api_key=api_key if provider == "xai" else "",
        local_api_key=api_key if provider == "local" else "",
        model=_resolve_model(environ, provider),
        api_token=token,
        proposer_token=environ.get("CHRONOS_WORKER_PROPOSER_TOKEN", "").strip(),
        backend_url=backend_url,
        local_base_url=local_base_url,
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
        max_daily_tokens=_optional_positive_int(environ, "CHRONOS_WORKER_MAX_DAILY_TOKENS"),
    )


def _resolve_model(environ: dict[str, str], provider: str) -> str:
    """The model id for this provider — explicit, or that provider's default.

    ``local`` has no default on purpose. A guessed roster tag is either absent,
    and every cycle dies on the call, or it names a *different model than the
    operator believes is thinking — and a decision attributed to the wrong
    model is worse than no decision.
    """

    explicit = environ.get("CHRONOS_WORKER_MODEL", "").strip()
    if explicit:
        return explicit
    default = _DEFAULT_MODELS.get(provider)
    if default is None:
        raise WorkerConfigError(
            f"CHRONOS_WORKER_MODEL must name the model explicitly when "
            f"CHRONOS_WORKER_PROVIDER={provider}: a local roster changes without notice, "
            "so there is no default this worker could guess that is not either missing or "
            "a different model than you think is thinking"
        )
    return default


def _require_loopback_url(url: str, *, variable: str, carries: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise WorkerConfigError(f"{variable} must be an http(s) URL, got scheme {parts.scheme!r}")
    host = (parts.hostname or "").strip().lower()
    if host not in _LOOPBACK_HOSTS:
        raise WorkerConfigError(
            f"{variable} points at {host!r}, which is not loopback. The worker sends "
            f"{carries} there; that may only ever go to a listener on this machine"
        )
    if parts.username or parts.password:
        # Neither value is echoed: this refusal is itself a log line.
        raise WorkerConfigError(
            f"{variable} carries a username or password in the URL. httpx turns URL "
            "userinfo into an Authorization header, and the URL is printed whole by "
            "every line that reports this endpoint — so a credential written here ends "
            "up in the logs. Put it in its own environment variable instead"
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


def _optional_positive_int(environ: dict[str, str], name: str) -> int | None:
    """``None`` when unset or blank; otherwise a positive int or a refusal."""

    if not environ.get(name, "").strip():
        return None
    return _positive_int(environ, name, 0)


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
