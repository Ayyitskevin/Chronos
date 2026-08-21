"""The one place the worker talks to xAI — raw HTTP, forced tool, no SDK.

Mirrors :mod:`worker.model` (Anthropic) so the isolation story stays the same:
no provider SDK, no ``chronos`` import, the key appears only in the
Authorization header, and prose is never parsed into a decision.

Request shape (OpenAI-compatible Chat Completions, 2026-08):

- ``POST https://api.x.ai/v1/chat/completions`` with ``Authorization: Bearer``.
- Default model ``grok-4.6``. No sampling parameters are sent.
- Forced tool: ``tool_choice`` names ``propose_decision`` with the same JSON
  schema the Anthropic path uses. xAI does not advertise Anthropic's
  ``strict: true``; local vocabulary validation in :mod:`worker.propose` and
  the gateway still drop illegal fields. That residual is disclosed, not
  papered over.

Deny-by-default extract: only a completed ``propose_decision`` function call
counts. HTTP errors, truncated output, or prose-only replies yield ``None``.
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

_logger = logging.getLogger("chronos.worker.model_xai")

XAI_URL: Final[str] = "https://api.x.ai/v1/chat/completions"


def think(
    config: WorkerConfig,
    snapshot: EvidenceSnapshot,
    client: httpx.Client,
    budget: DailyTokenBudget | None = None,
) -> dict[str, Any] | None:
    """One decision from Grok, as validated tool input — or None."""

    request = build_request(config, snapshot)
    try:
        response = client.post(
            XAI_URL,
            content=json.dumps(request).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {config.xai_api_key}",
                "content-type": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.HTTPError as error:
        _logger.error("xAI API unreachable: %s", type(error).__name__)
        return None

    if response.status_code != 200:
        _logger.error(
            "xAI API returned HTTP %d: %s",
            response.status_code,
            _error_summary(response),
        )
        return None

    try:
        body = response.json()
    except ValueError:
        _logger.error("xAI API returned a non-JSON body")
        return None
    if budget is not None:
        usage = body.get("usage") or {}
        budget.spend(charged_tokens(usage.get("prompt_tokens"), usage.get("completion_tokens")))
    return _extract_decision(body)


def build_request(config: WorkerConfig, snapshot: EvidenceSnapshot) -> dict[str, Any]:
    """The Chat Completions request body, deterministic given config + snapshot."""

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
    """Deny-by-default reading of a Chat Completions response."""

    usage = body.get("usage") or {}
    choices = body.get("choices") or []
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    finish = first.get("finish_reason")
    _logger.info(
        "xAI responded: finish_reason=%s prompt_tokens=%s completion_tokens=%s",
        finish,
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
    )

    if finish == "length":
        _logger.warning("xAI hit max_tokens before completing a decision; none proposed")
        return None

    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        if function.get("name") != "propose_decision":
            continue
        raw = function.get("arguments")
        parsed = _parse_arguments(raw)
        if parsed is None:
            _logger.warning("xAI propose_decision arguments were not an object; none proposed")
            return None
        return parsed

    _logger.warning(
        "xAI's response carried no propose_decision call (finish_reason=%s); none proposed",
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


def _error_summary(response: httpx.Response) -> str:
    try:
        error = response.json().get("error", {})
    except ValueError:
        return "(non-JSON error body)"
    if isinstance(error, dict):
        return f"{error.get('type', '?')}: {error.get('message', '?')}"
    return str(error)
