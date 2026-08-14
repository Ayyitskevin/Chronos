"""The worker's own halves: fail-closed config, the Claude call, and the cycle.

Contract-agreement proofs live in ``tests/safety/test_model_worker_exercised.py``.
Everything here runs against ``httpx.MockTransport`` — no socket is ever
opened, no backend is needed, and the Anthropic API is a canned response.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest
from worker.config import WorkerConfig, WorkerConfigError, load_config
from worker.cycle import CycleOutcome, run_cycle
from worker.evidence import EvidenceSnapshot, gather
from worker.model import ANTHROPIC_URL, build_request, think

API_KEY = "sk-test-key-never-logged"
TOKEN = "backend-token"


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
        "anthropic_api_key": API_KEY,
        "model": "claude-opus-5",
        "api_token": TOKEN,
        "proposer_token": "",
        "backend_url": "http://127.0.0.1:8000",
        "symbols": ("SPY",),
        "kinds": frozenset({"OPEN", "HOLD"}),
        "policy": "Hold unless the case is overwhelming.",
        "interval_seconds": 300,
        "lookback_days": 5,
        "forward": False,
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
