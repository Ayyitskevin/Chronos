"""The one place the worker talks to a local model server — raw HTTP, forced tool, no SDK.

Mirrors :mod:`worker.model_xai` deliberately: the same OpenAI-compatible Chat
Completions shape, the same deny-by-default extract, the same isolation story —
no provider SDK, no ``chronos`` import, the key (if there is one) appears only
in the Authorization header, and prose is never parsed into a decision.

Three things differ from a hosted provider, and each is a safety statement,
because a local server is *configured* where Anthropic and xAI are pinned:

- **The endpoint is configuration, not a source constant.** A local server's
  port belongs to the operator — Ollama's ``11434``, a gateway's own — so the
  URL cannot be a constant here. That matters because this request body
  carries the entire evidence snapshot: the account's cash, buying power,
  positions, and open orders. :func:`worker.config.load_config` therefore
  refuses a non-loopback ``CHRONOS_WORKER_LOCAL_BASE_URL`` exactly as it
  refuses a non-loopback backend. A model server on another host is reached
  through a local port-forward; that is the intended shape, not a gap.
- **The key is optional.** Ollama authenticates nothing, so an unset
  ``CHRONOS_WORKER_LOCAL_API_KEY`` sends no ``Authorization`` header at all
  rather than an empty bearer. A gateway that wants one reads it from that
  variable and nowhere else.
- **There is no default model.** ``CHRONOS_WORKER_MODEL`` is required for this
  provider and startup fails loud without it — a guessed roster tag is either
  absent or a different model than the operator believes is thinking.

The forced ``tool_choice`` is a **request**; the extract below is the
**guarantee**. Small local models honour tool forcing unevenly and some
OpenAI-compatible servers ignore ``tool_choice`` outright, so expect a
materially higher refusal rate than a frontier model. That costs decisions,
never safety: only a completed ``propose_decision`` call yields a candidate,
and everything else — prose, a wrong tool name, unparsable or non-object
arguments, a truncation, a non-200, a non-JSON body — yields ``None`` and the
cycle records ``NO_DECISION``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final

import httpx

from worker.budget import DailyTokenBudget
from worker.config import WorkerConfig
from worker.evidence import EvidenceSnapshot
from worker.model import (
    FRAMING,
    MAX_TOKENS,
    PROPOSE_DECISION_TOOL,
    REQUEST_TIMEOUT,
    charged_tokens,
)

_logger = logging.getLogger("chronos.worker.model_local")

#: Appended to the configured base URL. Both Ollama's ``/v1`` surface and a
#: gateway with the same shape serve the decision at this route.
CHAT_COMPLETIONS_PATH: Final[str] = "/chat/completions"

#: How much server-supplied error text may reach a log line. The type and the
#: status are what an operator acts on; the rest is the server's to be verbose
#: or hostile with.
ERROR_SUMMARY_LIMIT: Final[int] = 200


def endpoint(config: WorkerConfig) -> str:
    """The full Chat Completions URL for this worker's configured server."""

    return f"{config.local_base_url}{CHAT_COMPLETIONS_PATH}"


def think(
    config: WorkerConfig,
    snapshot: EvidenceSnapshot,
    client: httpx.Client,
    budget: DailyTokenBudget | None = None,
) -> dict[str, Any] | None:
    """One decision from the local model, as validated tool input — or None."""

    url = endpoint(config)
    request = build_request(config, snapshot)
    try:
        response = client.post(
            url,
            content=json.dumps(request).encode("utf-8"),
            headers=_headers(config),
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.HTTPError as error:
        _logger.error("local model server at %s unreachable: %s", url, type(error).__name__)
        return None

    if response.status_code != 200:
        _logger.error(
            "local model server returned HTTP %d: %s",
            response.status_code,
            _error_summary(response, secret=config.local_api_key),
        )
        return None

    try:
        body = response.json()
    except ValueError:
        _logger.error("local model server returned a non-JSON body")
        return None
    if not isinstance(body, dict):
        # A JSON array, string, or number parses fine and then has no ``.get``.
        # Refusing here rather than in ``_extract_decision`` keeps that function
        # byte-faithful to the xAI one, which the drift test pins.
        _logger.error(
            "local model server returned a JSON %s rather than an object; none proposed",
            type(body).__name__,
        )
        return None
    if budget is not None:
        usage = body.get("usage") or {}
        budget.spend(charged_tokens(usage.get("prompt_tokens"), usage.get("completion_tokens")))
    return _extract_decision(body)


def _headers(config: WorkerConfig) -> dict[str, str]:
    """JSON, plus a bearer only when the operator configured one.

    An empty ``Bearer`` would be a credential-shaped lie in the access log of
    whatever is listening; no header at all is the honest description of a
    server that authenticates nothing.
    """

    headers = {"content-type": "application/json"}
    if config.local_api_key:
        headers["Authorization"] = f"Bearer {config.local_api_key}"
    return headers


def build_request(config: WorkerConfig, snapshot: EvidenceSnapshot) -> dict[str, Any]:
    """The Chat Completions request body, deterministic given config + snapshot.

    Byte-for-byte the shape the xAI path sends, down to the framing and the
    tool schema: the provider changes, the contract the model is held to does
    not. No sampling parameters are sent — a local server's own defaults are
    the operator's business, and a temperature chosen here would be a silent
    second policy.
    """

    user_text = (
        "Evidence snapshot from your Chronos backend (canonical JSON; its SHA-256 is "
        f"{snapshot.digest}, taken {snapshot.as_of}):\n\n"
        f"{snapshot.canonical}\n\n"
        f"Watchlist: {', '.join(config.symbols)}. "
        f"Decision kinds permitted this session: {', '.join(sorted(config.kinds))}. "
        "Apply the policy and propose exactly one decision via the propose_decision tool."
    )
    schema = PROPOSE_DECISION_TOOL["input_schema"]
    return {
        "model": config.model,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": FRAMING + "\n" + config.policy},
            {"role": "user", "content": user_text},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": PROPOSE_DECISION_TOOL["name"],
                    "description": PROPOSE_DECISION_TOOL["description"],
                    "parameters": schema,
                },
            }
        ],
        "tool_choice": {
            "type": "function",
            "function": {"name": "propose_decision"},
        },
    }


def _extract_decision(body: dict[str, Any]) -> dict[str, Any] | None:
    """Deny-by-default reading of a Chat Completions response.

    ``finish_reason`` is read for the truncation case only. Local servers
    report it inconsistently — ``"stop"`` alongside a perfectly good
    ``tool_calls`` array is common — so a tool call is accepted on its own
    evidence rather than on the server's label for the turn.
    """

    usage = body.get("usage") or {}
    choices = body.get("choices") or []
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    finish = first.get("finish_reason")
    _logger.info(
        "the local model responded: finish_reason=%s prompt_tokens=%s completion_tokens=%s",
        finish,
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
    )

    if finish == "length":
        _logger.warning(
            "the local model hit max_tokens before completing a decision; none proposed"
        )
        return None

    raw_message = first.get("message")
    message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        raw_function = call.get("function")
        function: dict[str, Any] = raw_function if isinstance(raw_function, dict) else {}
        if function.get("name") != "propose_decision":
            continue
        raw = function.get("arguments")
        parsed = _parse_arguments(raw)
        if parsed is None:
            _logger.warning(
                "the local model's propose_decision arguments were not an object; none proposed"
            )
            return None
        return parsed

    _logger.warning(
        "the local model's response carried no propose_decision call (finish_reason=%s); "
        "none proposed. A server that ignores tool_choice looks exactly like this",
        finish,
    )
    return None


def _parse_arguments(raw: object) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _error_summary(response: httpx.Response, *, secret: str) -> str:
    """The server's error type and message — capped, and with the key removed.

    The xAI path can return the server's text as-is: that key is never sent to
    anything but ``api.x.ai``. This one talks to whatever the operator pointed
    it at, and a gateway or debug proxy that echoes the request's own
    ``Authorization`` header back inside its error body is a real failure mode
    of exactly that software. So the configured key is *removed* from the text
    rather than trusted not to appear in it, and the whole summary is capped so
    a verbose or hostile body cannot flood the log.

    What this does not do: a listener that echoes a transformed key — base64,
    a hash, a URL-encoding — defeats a literal match. The cap still bounds it,
    and the type and status are what an operator acts on anyway.
    """

    try:
        payload = response.json()
    except ValueError:
        return "(non-JSON error body)"
    error = payload.get("error", {}) if isinstance(payload, dict) else payload
    if isinstance(error, dict):
        summary = f"{error.get('type', '?')}: {error.get('message', '?')}"
    else:
        summary = str(error)
    return _redact(summary, secret=secret)


def _redact(text: str, *, secret: str) -> str:
    """Remove the configured key, then cap. That order matters.

    Capping first could split the key across the boundary and leave a usable
    prefix in the log; removing it first means there is nothing left to split.
    """

    if secret:
        text = text.replace(secret, "[redacted]")
    if len(text) > ERROR_SUMMARY_LIMIT:
        text = f"{text[:ERROR_SUMMARY_LIMIT]}…(truncated)"
    return text
