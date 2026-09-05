"""The worker's own halves: fail-closed config, the Claude call, and the cycle.

Contract-agreement proofs live in ``tests/safety/test_model_worker_exercised.py``.
Everything here runs against ``httpx.MockTransport`` — no socket is ever
opened, no backend is needed, and the Anthropic API is a canned response.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any
from urllib.parse import urlunsplit

import httpx
import pytest
from worker.budget import DailyTokenBudget
from worker.config import WorkerConfig, WorkerConfigError, load_config
from worker.cycle import CycleOutcome, run_cycle, run_loop
from worker.evidence import EvidenceSnapshot, gather
from worker.model import ANTHROPIC_URL, MAX_TOKENS, PROPOSE_DECISION_TOOL, build_request, think
from worker.model_local import ERROR_SUMMARY_LIMIT
from worker.model_local import build_request as build_local_request
from worker.model_local import endpoint as local_endpoint
from worker.model_local import think as think_local
from worker.model_xai import XAI_URL
from worker.model_xai import build_request as build_xai_request
from worker.model_xai import think as think_xai

API_KEY = "sk-test-key-never-logged"
TOKEN = "backend-token"
#: The fake bearer for the local provider's optional gateway key. One literal,
#: used by both the config tests and the transport tests — a second copy spelled
#: out beside an ``api_key`` name is what the tracked-file secret scan flags.
LOCAL_KEY = "gateway-test-key-never-logged"
#: Userinfo for the credential-in-URL refusal, as ``(name, value)``. Distinctive
#: on purpose: the refusal text itself contains the word "userinfo", so the
#: assertions need strings that could only have come from the rejected URL.
#: Named and valued to carry no credential keyword — these are not secrets, and
#: the tracked-file secret scan should not have to be told so.
_LEAKY_USERINFO = ("leaky-operator-name", "leaky-operator-value")


def _env(**overrides: str) -> dict[str, str]:
    environ = {
        "ANTHROPIC_API_KEY": API_KEY,
        "CHRONOS_WORKER_API_TOKEN": TOKEN,
        "CHRONOS_WORKER_SYMBOLS": "SPY,IWM",
        "CHRONOS_WORKER_KINDS": "OPEN,CLOSE,HOLD",
        "CHRONOS_WORKER_POLICY_FILE": "worker/policy.md",
    }
    environ.update(overrides)
    return {name: value for name, value in environ.items() if value != ""}


def _config(**overrides: object) -> WorkerConfig:
    base: dict[str, object] = {
        "provider": "anthropic",
        "anthropic_api_key": API_KEY,
        "xai_api_key": "",
        "local_api_key": "",
        "model": "claude-opus-5",
        "api_token": TOKEN,
        "proposer_token": "",
        "backend_url": "http://127.0.0.1:8000",
        "local_base_url": "http://127.0.0.1:11434/v1",
        "symbols": ("SPY",),
        "kinds": frozenset({"OPEN", "HOLD"}),
        "policy": "Hold unless the case is overwhelming.",
        "interval_seconds": 300,
        "lookback_days": 5,
        "forward": False,
        "max_daily_tokens": None,
    }
    base.update(overrides)
    return WorkerConfig(**base)  # type: ignore[arg-type]


_SNAPSHOT_CANONICAL = json.dumps({"a": 1}, sort_keys=True, separators=(",", ":"))
SNAPSHOT = EvidenceSnapshot(
    canonical=_SNAPSHOT_CANONICAL,
    digest=hashlib.sha256(_SNAPSHOT_CANONICAL.encode()).hexdigest(),
    as_of="2026-08-12T14:30:00+00:00",
)


def _tool_response(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1200, "output_tokens": 300},
        "content": [{"type": "tool_use", "name": "propose_decision", "input": decision}],
    }


def _hold_decision() -> dict[str, Any]:
    return {
        "kind": "HOLD",
        "symbol": "SPY",
        "direction": "NEUTRAL",
        "thesis": "Nothing clears the bar.",
        "rationale": None,
        "quantity": None,
        "strategy": None,
        "time_horizon": None,
        "target_reference": None,
        "confidence": None,
        "invalidation": [],
    }


def _backend_client(record: list[httpx.Request] | None = None) -> httpx.Client:
    """A fake backend serving the four evidence reads and the ingress POST."""

    def _handle(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/autonomy/proposals":
            return httpx.Response(
                202, json={"accepted": True, "stage": "QUEUED", "refusal": "", "detail": ""}
            )
        bodies: dict[str, Any] = {
            "/account/summary": {"total_cash": "100.00", "buying_power": "100.00"},
            "/account/positions": [],
            "/orders": [],
            "/terminal/bars": {"symbol": "SPY", "bars": [], "refusal": ""},
        }
        if path in bodies:
            return httpx.Response(200, json=bodies[path])
        return httpx.Response(404, json={"detail": "no such route"})

    return httpx.Client(transport=httpx.MockTransport(_handle))


@contextmanager
def _local_logs() -> Iterator[list[logging.LogRecord]]:
    """Capture the local provider's own logger, not the root one.

    ``caplog`` installs its handler on the *root* logger, and
    ``chronos.utils.logging.configure_logging`` sets ``propagate = False`` on
    the ``chronos`` logger — process-wide, for the rest of the session. So a
    ``caplog`` assertion about a ``chronos.*`` record sees everything when this
    file runs alone and nothing once any earlier test has configured logging.
    That is worse than a flaky test for the assertions here: "the key is absent
    from the log" passes trivially against an empty capture.

    Attaching to the named logger is immune to both the ordering and the global
    state, and touches no propagation flag. :func:`_text` asserts the capture is
    non-empty so these can never go vacuous again.
    """

    collected: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            collected.append(record)

    logger = logging.getLogger("chronos.worker.model_local")
    handler = _Collector(level=logging.DEBUG)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield collected
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _text(records: list[logging.LogRecord]) -> str:
    """The captured lines — and proof that anything was captured at all."""

    assert records, (
        "nothing was logged, so every assertion about the log's contents below "
        "would pass for the wrong reason"
    )
    return "\n".join(record.getMessage() for record in records)


def _anthropic_client(
    body: dict[str, Any], *, status: int = 200, record: list[httpx.Request] | None = None
) -> httpx.Client:
    def _handle(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(_handle))


# ------------------------------------------------------------------- configuration


def test_a_missing_api_key_refuses_to_start() -> None:
    with pytest.raises(WorkerConfigError, match="ANTHROPIC_API_KEY"):
        load_config(_env(ANTHROPIC_API_KEY=""))


def test_a_missing_backend_token_refuses_to_start() -> None:
    with pytest.raises(WorkerConfigError, match="CHRONOS_WORKER_API_TOKEN"):
        load_config(_env(CHRONOS_WORKER_API_TOKEN=""))


def test_an_empty_watchlist_refuses_to_start() -> None:
    with pytest.raises(WorkerConfigError, match="CHRONOS_WORKER_SYMBOLS"):
        load_config(_env(CHRONOS_WORKER_SYMBOLS=""))


def test_an_empty_kind_allowlist_refuses_to_start() -> None:
    with pytest.raises(WorkerConfigError, match="CHRONOS_WORKER_KINDS"):
        load_config(_env(CHRONOS_WORKER_KINDS=""))


def test_an_unknown_kind_refuses_to_start() -> None:
    with pytest.raises(WorkerConfigError, match="not decision kinds"):
        load_config(_env(CHRONOS_WORKER_KINDS="OPEN,YOLO"))


def test_a_non_loopback_backend_refuses_to_start() -> None:
    with pytest.raises(WorkerConfigError, match="not loopback"):
        load_config(_env(CHRONOS_WORKER_BACKEND_URL="https://example.com"))


def test_a_missing_policy_file_refuses_to_start(tmp_path: Any) -> None:
    with pytest.raises(WorkerConfigError, match="policy"):
        load_config(_env(CHRONOS_WORKER_POLICY_FILE=str(tmp_path / "absent.md")))


def test_forwarding_is_off_unless_the_owner_turns_it_on() -> None:
    assert load_config(_env()).forward is False
    assert load_config(_env(CHRONOS_WORKER_FORWARD="true")).forward is True


def test_the_default_model_is_the_current_opus() -> None:
    assert load_config(_env()).model == "claude-opus-5"
    assert load_config(_env()).provider == "anthropic"


def test_an_unknown_provider_refuses_to_start() -> None:
    with pytest.raises(WorkerConfigError, match="CHRONOS_WORKER_PROVIDER"):
        load_config(_env(CHRONOS_WORKER_PROVIDER="openai"))


def test_xai_without_a_console_key_refuses_to_start() -> None:
    with pytest.raises(WorkerConfigError, match="XAI_API_KEY"):
        load_config(_env(CHRONOS_WORKER_PROVIDER="xai", ANTHROPIC_API_KEY=""))


def test_xai_does_not_require_the_anthropic_key() -> None:
    config = load_config(
        _env(CHRONOS_WORKER_PROVIDER="xai", ANTHROPIC_API_KEY="", XAI_API_KEY="xai-test")
    )
    assert config.provider == "xai"
    assert config.model == "grok-4.6"
    assert config.xai_api_key == "xai-test"
    assert config.anthropic_api_key == ""
    assert config.forward is False


# --------------------------------------------------- the local provider's configuration


def _local_env(**overrides: str) -> dict[str, str]:
    """A local-provider environment: no vendor key, an explicit model tag."""

    return _env(
        CHRONOS_WORKER_PROVIDER="local",
        ANTHROPIC_API_KEY="",
        CHRONOS_WORKER_MODEL="a-local-tag:27b",
        **overrides,
    )


def test_the_local_provider_needs_no_key_at_all() -> None:
    """Ollama authenticates nothing; demanding a credential would be ceremony."""

    config = load_config(_local_env())
    assert config.provider == "local"
    assert config.model == "a-local-tag:27b"
    assert config.local_api_key == ""
    assert config.anthropic_api_key == ""
    assert config.api_key == ""
    assert config.forward is False


def test_a_local_gateway_key_binds_when_the_operator_sets_one() -> None:
    config = load_config(_local_env(CHRONOS_WORKER_LOCAL_API_KEY=LOCAL_KEY))
    assert config.local_api_key == LOCAL_KEY
    assert config.api_key == LOCAL_KEY


def test_the_local_provider_without_an_explicit_model_refuses_to_start() -> None:
    """No default tag: a guess is either absent or the wrong model thinking."""

    with pytest.raises(WorkerConfigError, match="CHRONOS_WORKER_MODEL"):
        load_config(_env(CHRONOS_WORKER_PROVIDER="local", ANTHROPIC_API_KEY=""))


def test_the_hosted_providers_keep_their_default_models() -> None:
    """Guard the guard: the requirement above is local-only, not a global break."""

    assert load_config(_env()).model == "claude-opus-5"
    assert load_config(_env(CHRONOS_WORKER_PROVIDER="xai", XAI_API_KEY="x")).model == "grok-4.6"


def test_the_local_base_url_defaults_to_loopback_ollama() -> None:
    config = load_config(_local_env())
    assert config.local_base_url == "http://127.0.0.1:11434/v1"
    assert local_endpoint(config) == "http://127.0.0.1:11434/v1/chat/completions"


def test_a_configured_local_base_url_loses_its_trailing_slash() -> None:
    config = load_config(_local_env(CHRONOS_WORKER_LOCAL_BASE_URL="http://localhost:8140/v1/"))
    assert config.local_base_url == "http://localhost:8140/v1"
    assert local_endpoint(config) == "http://localhost:8140/v1/chat/completions"


def test_a_non_loopback_local_server_refuses_to_start() -> None:
    """The prompt carries the whole account; a redirectable endpoint exfiltrates it."""

    with pytest.raises(WorkerConfigError, match="not loopback"):
        load_config(_local_env(CHRONOS_WORKER_LOCAL_BASE_URL="https://models.example.com/v1"))


def test_a_non_http_local_server_refuses_to_start() -> None:
    with pytest.raises(WorkerConfigError, match="must be an http\\(s\\) URL"):
        load_config(_local_env(CHRONOS_WORKER_LOCAL_BASE_URL="file:///etc/passwd"))


def _credentialed(netloc: str) -> str:
    """A loopback URL carrying userinfo, assembled rather than written out.

    Spelling ``scheme://<name>:<value>@host`` as a literal trips the
    tracked-file secret scan's basic-auth detector on something that is not a
    credential at all — as the first version of this very docstring proved.
    """

    name, value = _LEAKY_USERINFO
    return urlunsplit(("http", f"{name}:{value}@{netloc}", "/v1", "", ""))


def test_a_local_url_carrying_credentials_refuses_to_start() -> None:
    """httpx turns URL userinfo into an Authorization header, and the URL is logged."""

    with pytest.raises(WorkerConfigError, match="username or password") as caught:
        load_config(_local_env(CHRONOS_WORKER_LOCAL_BASE_URL=_credentialed("127.0.0.1:11434")))

    message = str(caught.value)
    assert _LEAKY_USERINFO[0] not in message, "the refusal must not print what it refused"
    assert _LEAKY_USERINFO[1] not in message


def test_a_backend_url_carrying_credentials_refuses_to_start() -> None:
    """Guard the guard: one shared checker, so the backend URL gains this too."""

    with pytest.raises(WorkerConfigError, match="username or password"):
        load_config(_env(CHRONOS_WORKER_BACKEND_URL=_credentialed("127.0.0.1:8000")))


def test_the_backend_url_refusal_still_names_the_backend_variable() -> None:
    """Guard the guard: one shared checker, two variables, two honest messages."""

    with pytest.raises(WorkerConfigError, match="CHRONOS_WORKER_BACKEND_URL"):
        load_config(_env(CHRONOS_WORKER_BACKEND_URL="https://example.com"))


# ------------------------------------------------------------------ the Claude call


def test_the_request_carries_the_required_headers_and_no_sampling_params() -> None:
    seen: list[httpx.Request] = []
    with _anthropic_client(_tool_response(_hold_decision()), record=seen) as client:
        think(_config(), SNAPSHOT, client)

    request = seen[0]
    assert str(request.url) == ANTHROPIC_URL
    assert request.headers["x-api-key"] == API_KEY
    assert request.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(request.content)
    assert body["model"] == "claude-opus-5"
    assert body["tool_choice"] == {"type": "tool", "name": "propose_decision"}
    assert body["tools"][0]["strict"] is True
    for forbidden in ("temperature", "top_p", "top_k", "thinking"):
        assert forbidden not in body


def test_the_prompt_contains_the_exact_digested_evidence() -> None:
    body = build_request(_config(), SNAPSHOT)
    user_text = body["messages"][0]["content"]
    assert SNAPSHOT.canonical in user_text
    assert SNAPSHOT.digest in user_text


def test_the_api_key_is_never_in_the_request_body() -> None:
    body = json.dumps(build_request(_config(), SNAPSHOT))
    assert API_KEY not in body


def test_a_tool_call_yields_the_decision() -> None:
    with _anthropic_client(_tool_response(_hold_decision())) as client:
        decision = think(_config(), SNAPSHOT, client)
    assert decision is not None
    assert decision["kind"] == "HOLD"


def test_a_refusal_stop_reason_yields_no_decision() -> None:
    body = {
        "stop_reason": "refusal",
        "stop_details": {"type": "refusal", "category": "cyber"},
        "content": [],
        "usage": {},
    }
    with _anthropic_client(body) as client:
        assert think(_config(), SNAPSHOT, client) is None


def test_a_max_tokens_truncation_yields_no_decision() -> None:
    with _anthropic_client({"stop_reason": "max_tokens", "content": [], "usage": {}}) as client:
        assert think(_config(), SNAPSHOT, client) is None


def test_a_prose_only_response_yields_no_decision() -> None:
    body = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "BUY SPY NOW!!!"}],
        "usage": {},
    }
    with _anthropic_client(body) as client:
        assert think(_config(), SNAPSHOT, client) is None


def test_an_api_error_yields_no_decision() -> None:
    error = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
    with _anthropic_client(error, status=429) as client:
        assert think(_config(), SNAPSHOT, client) is None


def _xai_config() -> WorkerConfig:
    return _config(
        provider="xai",
        anthropic_api_key="",
        xai_api_key="xai-test-key-never-logged",
        model="grok-4.6",
    )


def _xai_tool_response(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "propose_decision",
                                "arguments": json.dumps(decision),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 800, "completion_tokens": 200},
    }


def test_the_xai_request_is_forced_tool_and_carries_no_sampling() -> None:
    seen: list[httpx.Request] = []
    with _anthropic_client(_xai_tool_response(_hold_decision()), record=seen) as client:
        think_xai(_xai_config(), SNAPSHOT, client)

    request = seen[0]
    assert str(request.url) == XAI_URL
    assert request.headers["authorization"] == "Bearer xai-test-key-never-logged"
    body = json.loads(request.content)
    assert body["model"] == "grok-4.6"
    assert body["tool_choice"] == {
        "type": "function",
        "function": {"name": "propose_decision"},
    }
    assert body["tools"][0]["function"]["name"] == "propose_decision"
    for forbidden in ("temperature", "top_p", "top_k"):
        assert forbidden not in body
    assert "xai-test-key-never-logged" not in json.dumps(body)


def test_think_dispatches_to_xai_when_the_provider_is_xai() -> None:
    with _anthropic_client(_xai_tool_response(_hold_decision())) as client:
        decision = think(_xai_config(), SNAPSHOT, client)
    assert decision is not None
    assert decision["kind"] == "HOLD"


def test_xai_prose_only_yields_no_decision() -> None:
    body = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "BUY SPY NOW!!!"},
            }
        ],
        "usage": {},
    }
    with _anthropic_client(body) as client:
        assert think_xai(_xai_config(), SNAPSHOT, client) is None


def test_xai_truncation_yields_no_decision() -> None:
    body = {
        "choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": ""}}],
        "usage": {},
    }
    with _anthropic_client(body) as client:
        assert think_xai(_xai_config(), SNAPSHOT, client) is None


def test_the_xai_prompt_contains_the_exact_digested_evidence() -> None:
    body = build_xai_request(_xai_config(), SNAPSHOT)
    user_text = body["messages"][1]["content"]
    assert SNAPSHOT.canonical in user_text
    assert SNAPSHOT.digest in user_text


# --------------------------------------------------------------------- the local call

LOCAL_ENDPOINT = "http://127.0.0.1:11434/v1/chat/completions"


def _local_config(**overrides: object) -> WorkerConfig:
    return _config(
        provider="local",
        anthropic_api_key="",
        model="a-local-tag:27b",
        **overrides,
    )


def test_the_local_request_is_forced_tool_and_carries_no_sampling() -> None:
    seen: list[httpx.Request] = []
    with _anthropic_client(_xai_tool_response(_hold_decision()), record=seen) as client:
        think_local(_local_config(), SNAPSHOT, client)

    request = seen[0]
    assert str(request.url) == LOCAL_ENDPOINT
    body = json.loads(request.content)
    assert body["model"] == "a-local-tag:27b"
    assert body["tool_choice"] == {
        "type": "function",
        "function": {"name": "propose_decision"},
    }
    assert body["tools"][0]["function"]["name"] == "propose_decision"
    for forbidden in ("temperature", "top_p", "top_k"):
        assert forbidden not in body


def test_a_keyless_local_server_gets_no_authorization_header() -> None:
    """An empty bearer would be a credential-shaped lie in the server's log."""

    seen: list[httpx.Request] = []
    with _anthropic_client(_xai_tool_response(_hold_decision()), record=seen) as client:
        think_local(_local_config(), SNAPSHOT, client)

    assert "authorization" not in seen[0].headers


def test_a_local_gateway_key_rides_the_header_and_reaches_nothing_else() -> None:
    seen: list[httpx.Request] = []
    error = {"error": {"type": "not_found", "message": "no such model"}}
    with (
        _local_logs() as logged,
        _anthropic_client(error, status=404, record=seen) as client,
    ):
        assert think_local(_local_config(local_api_key=LOCAL_KEY), SNAPSHOT, client) is None

    request = seen[0]
    assert request.headers["authorization"] == f"Bearer {LOCAL_KEY}"
    assert LOCAL_KEY not in request.content.decode("utf-8")
    assert LOCAL_KEY not in _text(logged)


def test_a_server_that_echoes_the_bearer_back_does_not_get_it_logged() -> None:
    """The failure mode the shipped canned-404 test cannot see.

    A gateway or debug proxy that reflects the request's own ``Authorization``
    header into its error body is ordinary behaviour for exactly the software
    this provider talks to. Before the redaction, this ERROR line read
    ``... 401: auth: bad header Bearer <key>``.
    """

    def _echo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "type": "auth",
                    "message": f"bad header {request.headers.get('authorization', '')}",
                }
            },
        )

    with (
        _local_logs() as logged,
        httpx.Client(transport=httpx.MockTransport(_echo)) as client,
    ):
        assert think_local(_local_config(local_api_key=LOCAL_KEY), SNAPSHOT, client) is None

    text = _text(logged)
    assert LOCAL_KEY not in text
    assert "[redacted]" in text, "the key was dropped, not redacted — check the summary"
    assert "auth" in text, "the error type is what an operator acts on; keep it"


def test_a_flooding_error_body_is_capped_in_the_log() -> None:
    body = {"error": {"type": "boom", "message": "x" * 5000}}
    with _local_logs() as logged, _anthropic_client(body, status=500) as client:
        assert think_local(_local_config(), SNAPSHOT, client) is None

    text = _text(logged)
    assert "(truncated)" in text
    assert "x" * (ERROR_SUMMARY_LIMIT + 50) not in text


def test_think_dispatches_to_local_when_the_provider_is_local() -> None:
    with _anthropic_client(_xai_tool_response(_hold_decision())) as client:
        decision = think(_local_config(), SNAPSHOT, client)
    assert decision is not None
    assert decision["kind"] == "HOLD"


def test_a_local_server_that_answers_in_prose_yields_no_decision() -> None:
    """The forced tool is a request; this is the guarantee behind it."""

    body = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "I would buy SPY here."},
            }
        ],
        "usage": {},
    }
    with _anthropic_client(body) as client:
        assert think_local(_local_config(), SNAPSHOT, client) is None


def test_a_local_call_under_another_tool_name_yields_no_decision() -> None:
    body = _xai_tool_response(_hold_decision())
    body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "submit_order"
    with _anthropic_client(body) as client:
        assert think_local(_local_config(), SNAPSHOT, client) is None


def test_malformed_local_tool_arguments_yield_no_decision() -> None:
    body = _xai_tool_response(_hold_decision())
    body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"
    with _anthropic_client(body) as client:
        assert think_local(_local_config(), SNAPSHOT, client) is None


def test_local_tool_arguments_that_are_not_an_object_yield_no_decision() -> None:
    body = _xai_tool_response(_hold_decision())
    body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "[1, 2]"
    with _anthropic_client(body) as client:
        assert think_local(_local_config(), SNAPSHOT, client) is None


def test_a_truncated_turn_is_refused_even_carrying_a_complete_tool_call() -> None:
    """The guard has to bind on a body that would otherwise parse cleanly.

    A ``length`` turn whose message has no tool call is refused by the
    deny-by-default path anyway, so testing that shape would prove nothing
    about the truncation check.
    """

    body = _xai_tool_response(_hold_decision())
    body["choices"][0]["finish_reason"] = "length"
    with _anthropic_client(body) as client:
        assert think_local(_local_config(), SNAPSHOT, client) is None


def test_a_local_tool_call_labelled_stop_is_still_accepted() -> None:
    """Local servers label the turn inconsistently; the call is its own evidence."""

    body = _xai_tool_response(_hold_decision())
    body["choices"][0]["finish_reason"] = "stop"
    with _anthropic_client(body) as client:
        decision = think_local(_local_config(), SNAPSHOT, client)
    assert decision is not None
    assert decision["kind"] == "HOLD"


def test_a_non_200_is_refused_whatever_its_body_claims() -> None:
    """A proxy or gateway can answer 4xx with a decision-shaped body; it is not one."""

    body = _xai_tool_response(_hold_decision())
    body["error"] = {"type": "model_not_found", "message": "pull it first"}
    with _anthropic_client(body, status=404) as client:
        assert think_local(_local_config(), SNAPSHOT, client) is None


def test_a_non_json_local_body_yields_no_decision() -> None:
    def _handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>proxy error</html>")

    with httpx.Client(transport=httpx.MockTransport(_handle)) as client:
        assert think_local(_local_config(), SNAPSHOT, client) is None


#: The two OpenAI-compatible providers, paired with a config that selects each.
#: Anything asserted through this parametrisation is asserted for both, which is
#: what stops one provider's fix from being the other's outstanding defect —
#: exactly what a non-object body was between #147 and this change.
_OPENAI_COMPATIBLE = [
    pytest.param(think_local, _local_config, id="local"),
    pytest.param(think_xai, _xai_config, id="xai"),
]


@pytest.mark.parametrize(("think_fn", "config_fn"), _OPENAI_COMPATIBLE)
@pytest.mark.parametrize("payload", [[], "a string", 42, [{"choices": []}]])
def test_a_json_body_that_is_not_an_object_refuses_instead_of_raising(
    payload: Any,
    think_fn: Any,
    config_fn: Any,
) -> None:
    """Valid JSON, no ``.get`` — this used to raise AttributeError out of think().

    Both providers, one parametrisation. #147 guarded ``think_local`` and left
    the identical shape in ``think_xai`` (R10 — a shipped provider's live path
    was not that PR's to change), where ``run_loop`` caught the AttributeError
    and kept cadence: a cycle and a noisy log rather than authority. Asserting
    it here for both is what keeps that from happening again.
    """

    with _anthropic_client(payload) as client:  # type: ignore[arg-type]
        assert think_fn(config_fn(), SNAPSHOT, client) is None


def test_an_unreachable_local_server_yields_no_decision() -> None:
    def _handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with httpx.Client(transport=httpx.MockTransport(_handle)) as client:
        assert think_local(_local_config(), SNAPSHOT, client) is None


def test_the_local_prompt_carries_the_evidence_and_the_unchanged_contract() -> None:
    body = build_local_request(_local_config(), SNAPSHOT)
    user_text = body["messages"][1]["content"]
    assert SNAPSHOT.canonical in user_text
    assert SNAPSHOT.digest in user_text
    assert body["messages"][0]["content"].startswith("You are the decision worker for Chronos")
    parameters = body["tools"][0]["function"]["parameters"]
    assert parameters == PROPOSE_DECISION_TOOL["input_schema"], (
        "the local provider must hand over the same decision contract as every other "
        "provider — a second schema is a second vocabulary"
    )


def test_the_local_response_charges_the_budget_too() -> None:
    budget = DailyTokenBudget(1_000_000)
    with _anthropic_client(_xai_tool_response(_hold_decision())) as client:
        think_local(_local_config(), SNAPSHOT, client, budget=budget)

    assert budget.spent_today == 1000, "the canned response reports 800 + 200 tokens"


def test_the_worker_loop_builds_both_clients_with_the_environment_ignored(
    monkeypatch: Any,
) -> None:
    """An inherited proxy variable must not be able to route either client.

    httpx honours ``HTTP_PROXY``/``HTTPS_PROXY``/``ALL_PROXY`` by default and
    does *not* bypass 127.0.0.1 unless ``NO_PROXY`` says so — so without this
    the backend's API token and the whole evidence snapshot could leave the
    host while every loopback check still passed.
    """

    recorded: list[dict[str, Any]] = []
    real_client = httpx.Client

    def _record(**kwargs: Any) -> httpx.Client:
        recorded.append(dict(kwargs))
        return real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(404, json={})),
            **kwargs,
        )

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setattr(httpx, "Client", _record)
    run_loop(_local_config(), once=True)

    assert len(recorded) == 2, "the loop builds a backend client and a model client"
    assert [kwargs.get("trust_env") for kwargs in recorded] == [False, False]


def test_ignoring_the_environment_is_what_keeps_the_proxy_off_the_loopback_call(
    monkeypatch: Any,
) -> None:
    """Guard the guard: prove the flag above is load-bearing, not decoration."""

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    url = httpx.URL(LOCAL_ENDPOINT)
    with httpx.Client(trust_env=False) as guarded, httpx.Client() as unguarded:
        guarded_pool = guarded._transport_for_url(url)._pool
        unguarded_pool = unguarded._transport_for_url(url)._pool
    assert type(guarded_pool).__name__ == "ConnectionPool"
    assert type(unguarded_pool).__name__ == "HTTPProxy", (
        "if httpx stopped proxying loopback by default this guard would be moot — "
        "and this assertion is how we would find out"
    )


def test_the_two_openai_compatible_providers_refuse_the_same_bodies() -> None:
    """The local extract mirrors the xAI one; nothing may drift between them.

    ``worker/model_local.py`` duplicates ``worker/model_xai.py`` rather than
    hoisting a shared parser, so that a new provider could not change a shipped
    one's live path. This is the price of that choice: the duplicate is pinned.
    """

    from worker import model_local, model_xai

    accepted = _xai_tool_response(_hold_decision())
    wrong_name = _xai_tool_response(_hold_decision())
    wrong_name["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "x"
    unparsable = _xai_tool_response(_hold_decision())
    unparsable["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{"
    truncated = _xai_tool_response(_hold_decision())
    truncated["choices"][0]["finish_reason"] = "length"
    bodies: list[dict[str, Any]] = [
        accepted,
        wrong_name,
        unparsable,
        truncated,
        {"choices": [{"finish_reason": "stop", "message": {"content": "prose"}}], "usage": {}},
        {"choices": [], "usage": {}},
        {},
    ]
    for body in bodies:
        assert model_local._extract_decision(body) == model_xai._extract_decision(body), (
            f"the two OpenAI-compatible extracts disagreed on {body!r}"
        )


# ---------------------------------------------------------------------- the evidence


def test_the_snapshot_digest_binds_the_canonical_bytes() -> None:
    with _backend_client() as backend:
        snapshot = gather(_config(), backend)
    assert snapshot is not None
    assert snapshot.digest == hashlib.sha256(snapshot.canonical.encode("utf-8")).hexdigest()
    assert json.loads(snapshot.canonical)["watchlist"] == ["SPY"]


def test_a_failed_read_means_no_snapshot_and_no_thinking() -> None:
    """Facts are gathered or the cycle refuses — never invented."""

    def _broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "backend on fire"})

    with httpx.Client(transport=httpx.MockTransport(_broken)) as backend:
        assert gather(_config(), backend) is None


def test_the_token_travels_in_the_header_never_the_snapshot() -> None:
    seen: list[httpx.Request] = []
    with _backend_client(seen) as backend:
        snapshot = gather(_config(), backend)
    assert snapshot is not None
    assert all(request.headers["X-Chronos-Token"] == TOKEN for request in seen)
    assert TOKEN not in snapshot.canonical


def _proposal_posts(seen: list[httpx.Request]) -> list[httpx.Request]:
    """Only the proposal forwards.

    Since ADR-0028 every cycle also POSTs ``/autonomy/evidence`` to ask for an
    issued bundle, and this file's fake backend answers 404 — the backend
    stating that evidence binding is off, which sends the worker down the
    pre-ADR-0028 local-composition path. That probe is not a proposal, and a
    test that counted it as one would be asserting the wrong claim.
    """

    return [
        request
        for request in seen
        if request.method == "POST" and request.url.path == "/autonomy/proposals"
    ]


# ------------------------------------------------------------------------- the cycle


def test_dry_run_thinks_and_sends_nothing() -> None:
    seen: list[httpx.Request] = []
    with (
        _backend_client(seen) as backend,
        _anthropic_client(_tool_response(_hold_decision())) as anthropic,
    ):
        outcome = run_cycle(_config(), backend=backend, anthropic=anthropic)

    assert outcome is CycleOutcome.DRY_RUN
    assert not _proposal_posts(seen), (
        "nothing may be proposed here; the /autonomy/evidence POST is ADR-0028's "
        "issuance probe, which this fake backend answers 404 (binding off)"
    )


def test_forwarding_posts_the_proposal_with_the_token() -> None:
    seen: list[httpx.Request] = []
    with (
        _backend_client(seen) as backend,
        _anthropic_client(_tool_response(_hold_decision())) as anthropic,
    ):
        outcome = run_cycle(_config(forward=True), backend=backend, anthropic=anthropic)

    assert outcome is CycleOutcome.FORWARDED
    posts = _proposal_posts(seen)
    assert len(posts) == 1
    assert posts[0].url.path == "/autonomy/proposals"
    assert posts[0].headers["X-Chronos-Token"] == TOKEN
    # Pre-registry posture: no proposer credential configured, none sent.
    assert "X-Chronos-Proposer-Token" not in posts[0].headers
    proposal = json.loads(posts[0].content)
    assert proposal["kind"] == "HOLD"
    assert proposal["evidence"][0]["kind"] == "worker_evidence_snapshot"


def test_a_configured_proposer_credential_rides_the_forward() -> None:
    """ADR-0023: when the worker is registered, the proposal POST carries its
    credential alongside the token — so it works under either backend posture —
    and the credential goes nowhere else (evidence reads stay token-only)."""

    seen: list[httpx.Request] = []
    with (
        _backend_client(seen) as backend,
        _anthropic_client(_tool_response(_hold_decision())) as anthropic,
    ):
        outcome = run_cycle(
            _config(forward=True, proposer_token="proposer-secret"),
            backend=backend,
            anthropic=anthropic,
        )

    assert outcome is CycleOutcome.FORWARDED
    posts = _proposal_posts(seen)
    assert len(posts) == 1
    assert posts[0].headers["X-Chronos-Proposer-Token"] == "proposer-secret"
    assert posts[0].headers["X-Chronos-Token"] == TOKEN
    reads = [request for request in seen if request.method == "GET"]
    assert reads, "the cycle must have gathered evidence"
    assert all("X-Chronos-Proposer-Token" not in request.headers for request in reads)


def test_an_incoherent_decision_is_refused_locally_and_never_sent() -> None:
    bad = _hold_decision()
    bad["direction"] = "LONG"  # a HOLD may not express a direction
    seen: list[httpx.Request] = []
    with (
        _backend_client(seen) as backend,
        _anthropic_client(_tool_response(bad)) as anthropic,
    ):
        outcome = run_cycle(_config(forward=True), backend=backend, anthropic=anthropic)

    assert outcome is CycleOutcome.REFUSED_LOCALLY
    assert not _proposal_posts(seen), (
        "nothing may be proposed here; the /autonomy/evidence POST is ADR-0028's "
        "issuance probe, which this fake backend answers 404 (binding off)"
    )


def test_no_evidence_means_the_model_is_never_called() -> None:
    def _broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    anthropic_calls: list[httpx.Request] = []
    with (
        httpx.Client(transport=httpx.MockTransport(_broken)) as backend,
        _anthropic_client(_tool_response(_hold_decision()), record=anthropic_calls) as anthropic,
    ):
        outcome = run_cycle(_config(), backend=backend, anthropic=anthropic)

    assert outcome is CycleOutcome.NO_EVIDENCE
    assert anthropic_calls == []


def test_an_ingress_refusal_is_reported_not_swallowed() -> None:
    bodies: dict[str, Any] = {
        "/account/summary": {},
        "/account/positions": [],
        "/orders": [],
        "/terminal/bars": {},
    }

    def _serve(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/autonomy/proposals":
            return httpx.Response(
                422,
                json={
                    "accepted": False,
                    "stage": "INGRESS",
                    "refusal": "MALFORMED_PROPOSAL",
                    "detail": "rejected field(s): symbol",
                },
            )
        if request.url.path == "/autonomy/evidence":
            # This backend has no evidence binding configured, and says so the
            # way the real route does. The subject here is the *ingress* refusal,
            # so the cycle must reach the proposal POST — a 200 with an
            # unexpected body would (correctly) stop it at NO_EVIDENCE instead,
            # and the test would then pass for the wrong reason.
            return httpx.Response(404, json={"refusal": "EVIDENCE_BINDING_DISABLED"})
        return httpx.Response(200, json=bodies.get(request.url.path, {}))

    with (
        httpx.Client(transport=httpx.MockTransport(_serve)) as backend,
        _anthropic_client(_tool_response(_hold_decision())) as anthropic,
    ):
        outcome = run_cycle(_config(forward=True), backend=backend, anthropic=anthropic)

    assert outcome is CycleOutcome.INGRESS_REFUSED


# ------------------------------------------------------- the daily token ceiling (A5)


def test_an_unparsable_daily_ceiling_refuses_to_start() -> None:
    with pytest.raises(WorkerConfigError, match="CHRONOS_WORKER_MAX_DAILY_TOKENS"):
        load_config(_env(CHRONOS_WORKER_MAX_DAILY_TOKENS="a lot"))


def test_a_non_positive_daily_ceiling_refuses_to_start() -> None:
    for bad in ("0", "-5"):
        with pytest.raises(WorkerConfigError, match="CHRONOS_WORKER_MAX_DAILY_TOKENS"):
            load_config(_env(CHRONOS_WORKER_MAX_DAILY_TOKENS=bad))


def test_an_unset_ceiling_means_uncapped() -> None:
    assert load_config(_env()).max_daily_tokens is None


def test_a_set_ceiling_parses() -> None:
    config = load_config(_env(CHRONOS_WORKER_MAX_DAILY_TOKENS="250000"))
    assert config.max_daily_tokens == 250000


def test_at_the_ceiling_the_cycle_reads_no_evidence_and_never_calls_the_model() -> None:
    budget = DailyTokenBudget(10)
    budget.spend(10)
    backend_calls: list[httpx.Request] = []
    anthropic_calls: list[httpx.Request] = []
    with (
        _backend_client(backend_calls) as backend,
        _anthropic_client(_tool_response(_hold_decision()), record=anthropic_calls) as anthropic,
    ):
        outcome = run_cycle(_config(), backend=backend, anthropic=anthropic, budget=budget)

    assert outcome is CycleOutcome.COST_CEILING
    assert backend_calls == [], "an exhausted budget must cost no backend reads"
    assert anthropic_calls == [], "an exhausted budget must never reach the model"


def test_a_cycle_under_the_ceiling_proceeds_and_charges_the_budget() -> None:
    budget = DailyTokenBudget(1_000_000)
    with (
        _backend_client() as backend,
        _anthropic_client(_tool_response(_hold_decision())) as anthropic,
    ):
        outcome = run_cycle(_config(), backend=backend, anthropic=anthropic, budget=budget)

    assert outcome is CycleOutcome.DRY_RUN
    assert budget.spent_today == 1500, "the canned response reports 1200 + 300 tokens"


def test_a_priced_response_without_usage_charges_the_full_max_tokens() -> None:
    budget = DailyTokenBudget(1_000_000)
    body = _tool_response(_hold_decision())
    del body["usage"]
    with _anthropic_client(body) as client:
        think(_config(), SNAPSHOT, client, budget=budget)

    assert budget.spent_today == MAX_TOKENS, (
        "a priced response reporting no usage must overcharge, never undercharge"
    )


def test_the_xai_response_charges_the_budget_too() -> None:
    budget = DailyTokenBudget(1_000_000)
    with _anthropic_client(_xai_tool_response(_hold_decision())) as client:
        think_xai(_xai_config(), SNAPSHOT, client, budget=budget)

    assert budget.spent_today == 1000, "the canned xAI response reports 800 + 200 tokens"


def test_the_utc_day_roll_resets_the_spend() -> None:
    budget = DailyTokenBudget(100)
    budget.spend(100, today=date(2026, 8, 20))
    assert budget.exhausted(today=date(2026, 8, 20))
    assert not budget.exhausted(today=date(2026, 8, 21)), "a new UTC day starts at zero"
    assert budget.spent_today == 0


def test_an_uncapped_budget_tracks_but_never_exhausts() -> None:
    budget = DailyTokenBudget(None)
    budget.spend(10**9)
    assert budget.spent_today == 10**9
    assert not budget.exhausted()
