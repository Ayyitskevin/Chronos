"""Offline Phase A: one real worker cycle against the started demo application.

The backend adapter forwards into real routes, auth and file-backed persistence;
only the local model response and an unreachable connection are simulated. The
autonomy tick is disabled: this proves neither Phase B admission/draining nor
process/TCP wiring, real model behavior, broker truth or promotion eligibility.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import socket
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from worker.config import load_config
from worker.cycle import CycleOutcome, run_cycle

from chronos.api.main import create_app
from chronos.autonomy import (
    AutonomyMandate,
    AutonomyMode,
    FamilyPromotion,
    InstrumentScope,
    OrderForm,
    PromotionLevel,
    StrategyForm,
    TradableAssetClass,
    VersionPins,
)
from chronos.broker.demo import DEMO_ACCOUNT_ID, DemoBroker
from chronos.config.settings import Settings, get_settings
from chronos.supervisor.evidence_bundles import hash_chain_stream
from chronos.utils.identifiers import account_fingerprint

_FINGERPRINT = account_fingerprint(DEMO_ACCOUNT_ID)
_MODEL = "phase-a-fixture"
_EVIDENCE = ("POST", "/autonomy/evidence")
_PROPOSAL = ("POST", "/autonomy/proposals")
_READS = [
    ("GET", "/account/summary"),
    ("GET", "/account/positions"),
    ("GET", "/orders"),
    ("GET", "/terminal/bars"),
]


async def _no_ticks(autonomy: object, **_kwargs: object) -> None:
    """Keep the real startup/teardown, but leave supervisor scheduling out."""


def _no_connection(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("Phase A integration must not open a network connection")


@pytest.fixture()
def phase_a_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[str]:
    # Settings normally loads .env. An empty cwd and cleared setting overrides
    # keep this test independent of both operator files and ambient live flags.
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.lower() in Settings.model_fields:
            monkeypatch.delenv(name)
    monkeypatch.setattr(socket.socket, "connect", _no_connection)
    monkeypatch.setattr(socket.socket, "connect_ex", _no_connection)
    monkeypatch.setattr("chronos.api.main.autonomy_tick_task", _no_ticks)

    now = datetime.now(UTC)
    versions = VersionPins(
        provider="local",
        model_id=_MODEL,
        model_version="1",
        prompt_version="1",
        tool_schema_version="1",
        decision_schema_version="1",
        policy_version="1",
    )
    mandate = AutonomyMandate(
        mandate_id="phase-a-fixture",
        mandate_version=1,
        account_fingerprint=_FINGERPRINT,
        mode=AutonomyMode.SHADOW,
        promotions=(
            FamilyPromotion(asset_class=TradableAssetClass.EQUITY, level=PromotionLevel.SHADOW),
        ),
        effective_from=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
        versions=versions,
        scope=InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY",),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT,),
        ),
        owner_authorization_ref="synthetic-phase-a-test",
        authored_at=now,
    )
    proposer_token = secrets.token_urlsafe(32)
    registry = {
        "schema_version": 1,
        "proposers": [
            {
                "proposer_id": "phase-a-fixture",
                "secret_sha256": hashlib.sha256(proposer_token.encode()).hexdigest(),
                **versions.model_dump(),
                "expires_at": (now + timedelta(hours=1)).isoformat(),
                "enabled": True,
            }
        ],
    }
    for name, content in (
        ("mandate.json", mandate.model_dump_json()),
        ("proposers.json", json.dumps(registry)),
        ("policy.md", "Observe the synthetic SPY evidence and emit HOLD only."),
    ):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
    for name, value in {
        "BROKER_MODE": "demo",
        "DEMO_PROFILE": "empty_account",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'chronos.db'}",
        "LOG_FILE": str(tmp_path / "chronos.log"),
        "BACKEND_TOKEN_FILE": str(tmp_path / "backend_api_token"),
        "LIVE_KILL_SWITCH_FILE": str(tmp_path / "kill.json"),
        "SESSION_BASELINE_FILE": str(tmp_path / "baseline.json"),
        "AUTONOMY_MANDATE_FILE": str(tmp_path / "mandate.json"),
        "AUTONOMY_PROPOSERS_FILE": str(tmp_path / "proposers.json"),
        "AUTONOMY_ALERT_FILE": str(tmp_path / "owner_alerts.jsonl"),
        "CLOCK_HEALTH_PROVIDER": "disabled",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        yield proposer_token
    finally:
        get_settings.cache_clear()


class _WorkerLogs(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture()
def worker_logs() -> Iterator[_WorkerLogs]:
    # configure_logging disables chronos propagation: root caplog is insufficient.
    handler = _WorkerLogs()
    loggers = [logging.getLogger(f"chronos.worker.{name}") for name in ("cycle", "evidence")]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.removeHandler(handler)
            logger.setLevel(level)
        handler.close()


def _committed_state(path: Path) -> dict[str, Any]:
    """A separate connection sees only committed state, including after shutdown."""
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return {
            "bundles": [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM autonomy_evidence_bundles WHERE account_fingerprint = ?",
                    (_FINGERPRINT,),
                )
            ],
            "events": [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload_json FROM hash_chain_records WHERE stream = ? AND kind = ?",
                    (hash_chain_stream(_FINGERPRINT), "evidence_bundle_issued"),
                )
            ],
            "proposals": connection.execute(
                "SELECT count(*) FROM autonomy_proposal_queue"
            ).fetchone()[0],
            "attempts": connection.execute(
                "SELECT count(*) FROM autonomy_decision_attempts"
            ).fetchone()[0],
        }


@pytest.mark.parametrize("case", ["binding-on", "binding-off", "unreachable"])
def test_phase_a_cycle_obeys_the_evidence_contract(
    case: str,
    phase_a_env: str,
    worker_logs: _WorkerLogs,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUTONOMY_EVIDENCE_BUNDLES", str(case != "binding-off").lower())
    settings = get_settings()
    # Derive the app origin independently: accepting just a path would hide A1.
    origin = httpx.URL(f"http://{settings.backend_host}:{settings.backend_port}")
    app = create_app()
    received: list[tuple[str, str]] = []
    attempts: list[httpx.Request] = []
    responses: list[httpx.Response] = []
    model_requests: list[httpx.Request] = []

    @app.middleware("http")
    async def record_route(request: Request, call_next: Any) -> Any:
        received.append((request.method, request.url.path))
        return await call_next(request)

    with TestClient(app, base_url=str(origin)) as started:
        runtime = app.state.backend.runtime
        assert isinstance(runtime.broker, DemoBroker)
        assert runtime.settings.database_url == f"sqlite:///{tmp_path / 'chronos.db'}"
        assert not runtime.settings.allow_order_transmit
        assert not runtime.settings.allow_live_trading
        assert app.state.autonomy is not None
        assert app.state.autonomy.mandate.mode is AutonomyMode.SHADOW
        config = load_config(
            {
                "CHRONOS_WORKER_PROVIDER": "local",
                "CHRONOS_WORKER_MODEL": _MODEL,
                "CHRONOS_WORKER_API_TOKEN": app.state.api_token,
                "CHRONOS_WORKER_PROPOSER_TOKEN": phase_a_env,
                "CHRONOS_WORKER_SYMBOLS": "SPY",
                "CHRONOS_WORKER_KINDS": "HOLD",
                "CHRONOS_WORKER_POLICY_FILE": str(tmp_path / "policy.md"),
                "CHRONOS_WORKER_LOOKBACK_DAYS": "5",
                "CHRONOS_WORKER_FORWARD": "false",
            }
        )
        assert config.forward is False

        def backend_adapter(request: httpx.Request) -> httpx.Response:
            attempts.append(request)
            actual = (request.url.scheme, request.url.host, request.url.port)
            expected = (origin.scheme, origin.host, origin.port)
            if case == "unreachable" or actual != expected:
                raise httpx.ConnectError("Synthetic unreachable backend", request=request)
            response = started.request(
                request.method,
                str(request.url),
                headers=request.headers,
                content=request.content,
            )
            responses.append(response)
            return httpx.Response(
                response.status_code, headers=response.headers, content=response.content
            )

        def model_adapter(request: httpx.Request) -> httpx.Response:
            model_requests.append(request)
            assert str(request.url) == f"{config.local_base_url}/chat/completions"
            assert request.method == "POST"
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "tool_calls": [
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": "propose_decision",
                                            "arguments": json.dumps(
                                                {
                                                    "kind": "HOLD",
                                                    "symbol": "SPY",
                                                    "direction": "NEUTRAL",
                                                    "thesis": "Synthetic evidence warrants HOLD.",
                                                    "rationale": None,
                                                    "quantity": None,
                                                    "strategy": None,
                                                    "time_horizon": None,
                                                    "target_reference": None,
                                                    "confidence": None,
                                                    "invalidation": [],
                                                }
                                            ),
                                        },
                                    }
                                ]
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                },
            )

        before = _committed_state(tmp_path / "chronos.db")
        assert before == {"bundles": [], "events": [], "proposals": 0, "attempts": 0}
        with (
            httpx.Client(transport=httpx.MockTransport(backend_adapter)) as backend,
            httpx.Client(transport=httpx.MockTransport(model_adapter)) as model,
        ):
            outcome = run_cycle(config, backend=backend, anthropic=model)
        after = _committed_state(tmp_path / "chronos.db")

    assert _committed_state(tmp_path / "chronos.db") == after
    attempted = [(request.method, request.url.path) for request in attempts]
    # Observe both sender and actual receiver/commit. The adapter never blocks
    # proposal ingress, so removing the forward guard must trip these bounds.
    assert (
        attempted.count(_PROPOSAL),
        received.count(_PROPOSAL),
        after["proposals"] - before["proposals"],
        after["attempts"] - before["attempts"],
    ) == (0, 0, 0, 0)
    assert attempted and attempted[0] == _EVIDENCE
    messages = [record.getMessage() for record in worker_logs.records]
    assert messages
    dry_runs = [message.partition("\n")[2] for message in messages if message.startswith("DRY RUN")]

    if case == "unreachable":
        assert attempted == [_EVIDENCE]
        assert received == []
        assert responses == []
        assert model_requests == []
        assert dry_runs == []
        assert outcome is CycleOutcome.NO_EVIDENCE
        assert "Evidence issuance is unreachable: ConnectError" in messages
        assert after == before
    else:
        assert outcome is CycleOutcome.DRY_RUN
        assert len(model_requests) == len(dry_runs) == 1
        proposal = json.loads(dry_runs[0])
        assert (proposal["kind"], proposal["symbol"], proposal["direction"]) == (
            "HOLD",
            "SPY",
            "NEUTRAL",
        )
        assert len(proposal["evidence"]) == 1
        citation = proposal["evidence"][0]
        assert citation["kind"] == "worker_evidence_snapshot"
        model_body = json.loads(model_requests[0].content)
        assert model_body["model"] == _MODEL
        prompt = next(row["content"] for row in model_body["messages"] if row["role"] == "user")
        framing, canonical, watchlist = prompt.split("\n\n")
        snapshot = json.loads(canonical)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert digest == citation["digest"]
        assert digest in framing
        assert citation["as_of"] in framing
        assert "Watchlist: SPY." in watchlist
        assert snapshot["watchlist"] == ["SPY"]
        assert received == attempted
        if case == "binding-on":
            assert attempted == [_EVIDENCE]
            assert [response.status_code for response in responses] == [201]
            issued = responses[0].json()
            assert canonical == issued["document"]
            assert len(after["bundles"]) == len(after["events"]) == 1
            row, event = after["bundles"][0], after["events"][0]
            assert citation["evidence_id"] == issued["bundle_id"] == row["bundle_id"]
            assert event["bundle_id"] == row["bundle_id"]
            assert digest == issued["digest"] == row["digest"] == event["digest"]
            assert row["proposer_id"] == event["proposer_id"] == "phase-a-fixture"
            assert citation["as_of"] == issued["issued_at"]
        else:
            assert attempted == [_EVIDENCE, *_READS]
            assert [response.status_code for response in responses] == [404, 200, 200, 200, 200]
            assert responses[0].json()["refusal"] == "EVIDENCE_BINDING_DISABLED"
            assert dict(attempts[-1].url.params) == {
                "symbol": "SPY",
                "interval": "1d",
                "lookback": "5",
            }
            assert snapshot["account"] == responses[1].json()
            assert snapshot["positions"] == responses[2].json()
            assert snapshot["open_orders"] == responses[3].json()
            assert snapshot["daily_bars"] == {"SPY": responses[4].json()}
            assert citation["evidence_id"] == f"worker-snapshot:{snapshot['as_of']}"
            assert citation["as_of"] == snapshot["as_of"]
            assert after == before
