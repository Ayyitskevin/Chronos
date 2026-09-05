"""Forwarding is ON only in a private dict passed to load_config in this test.

This is not the operator switch: no environment, .env, unit, configuration file
or production default sets forwarding. Any such change would cross that boundary.
The real demo app remains SHADOW/NON_SUBMITTING, with both clients on MockTransport.

Offline Phase B proves receipt, credential binding, drain and a journaled SHADOW
refusal, plus expiry and transaction rollback. It proves no admitted trade,
reservation survival at handoff (#151), settings-source loading, real model,
process/TCP wiring, broker truth, sustained campaign or promotion eligibility.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import socket
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
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
from chronos.autonomy.enums import NON_SUBMITTING_AUTONOMY_MODES
from chronos.broker.demo import DEMO_ACCOUNT_ID, DemoBroker
from chronos.config.settings import Settings
from chronos.domain.enums import BrokerMode, DemoProfile
from chronos.persistence import hash_chain
from chronos.persistence.schema import AutonomyDecisionAttemptRow, HashChainRow
from chronos.supervisor import durable
from chronos.supervisor.proposers import ProposerRegistration
from chronos.utils.identifiers import account_fingerprint

_FINGERPRINT = account_fingerprint(DEMO_ACCOUNT_ID)
_MODEL = "phase-b-fixture"
_DECISIONS = f"autonomy.decisions:{_FINGERPRINT}"
_CYCLES = f"autonomy.cycles:{_FINGERPRINT}"
_POSTURE = {
    "version": 1,
    "identity": "authenticated",
    "registry": "configured",
    "evidence_binding": "in_force",
    "credential_epoch_bound": True,
}


async def _no_ticks(autonomy: object, **_kwargs: object) -> None:
    """Real lifespan, with just the background drain scheduler held for one tick."""


def _no_connection(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("Phase B integration must not open a network connection")


@pytest.fixture()
def phase_b_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Settings, str, str]:
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
        mandate_id=_MODEL,
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
        owner_authorization_ref="synthetic-phase-b-test",
        authored_at=now,
    )
    token = secrets.token_urlsafe(32)
    registration = ProposerRegistration(
        proposer_id=_MODEL,
        secret_sha256=hashlib.sha256(token.encode()).hexdigest(),
        **versions.model_dump(),
        expires_at=now + timedelta(hours=1),
        enabled=True,
    )
    document = registration.model_dump(mode="json")
    document["expires_at"] = registration.expires_at.astimezone(UTC).isoformat()
    # An independent oracle for the complete registration, not registration_binding.
    entry_digest = hashlib.sha256(
        json.dumps(
            {
                "domain": "chronos.proposer-registration.v1",
                "registry_schema_version": 1,
                "registration": document,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    for name, content in (
        ("mandate.json", mandate.model_dump_json()),
        ("proposers.json", json.dumps({"schema_version": 1, "proposers": [document]})),
        ("policy.md", "Observe the synthetic SPY evidence and emit HOLD only."),
    ):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)

    # Trusted typed fixture data bypasses BaseSettings environment/dotenv sources
    # AND settings validation. Real runtime construction and grant loaders remain.
    settings = Settings.model_construct(
        broker_mode=BrokerMode.DEMO,
        demo_profile=DemoProfile.EMPTY_ACCOUNT,
        allow_order_transmit=False,
        allow_live_trading=False,
        database_url=f"sqlite:///{tmp_path / 'chronos.db'}",
        log_file=tmp_path / "chronos.log",
        backend_token_file=tmp_path / "backend_api_token",
        live_kill_switch_file=tmp_path / "kill.json",
        session_baseline_file=tmp_path / "baseline.json",
        autonomy_mandate_file=tmp_path / "mandate.json",
        autonomy_proposers_file=tmp_path / "proposers.json",
        autonomy_alert_file=tmp_path / "owner_alerts.jsonl",
        clock_health_provider="disabled",
        autonomy_evidence_bundles=True,
    )
    assert settings.broker_mode is BrokerMode.DEMO
    assert settings.demo_profile is DemoProfile.EMPTY_ACCOUNT
    assert not settings.allow_order_transmit and not settings.allow_live_trading
    monkeypatch.setattr("chronos.runtime.get_settings", lambda: settings)
    return settings, token, entry_digest


class _WorkerLogs(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture()
def worker_logs() -> Iterator[_WorkerLogs]:
    handler = _WorkerLogs()
    logger = logging.getLogger("chronos.worker.cycle")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        handler.close()


def _committed_state(path: Path) -> dict[str, Any]:
    """Fresh connections observe commits, not the drain's identity map/transaction."""
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return {
            key: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for key, table in (
                ("queue", "autonomy_proposal_queue"),
                ("bundles", "autonomy_evidence_bundles"),
                ("attempts", "autonomy_decision_attempts"),
                ("activity", "autonomy_session_counters"),
                ("journal", "hash_chain_records"),
                ("alerts", "autonomy_owner_alerts"),
            )
        }


def _journal(state: dict[str, Any], stream: str) -> list[dict[str, Any]]:
    return [row for row in state["journal"] if row["stream"] == stream]


def _assert_mode_refusal(payload: dict[str, Any]) -> None:
    # Raw bytes must contain the field; a reader's fallback cannot invent proof.
    assert payload["posture"] == _POSTURE
    posture = durable.read_posture(payload)
    assert posture is not None and posture.as_payload() == _POSTURE
    assert payload["admitted"] is False
    assert payload["refusal"] == "MODE_CANNOT_SUBMIT"
    checks = payload["checks"]
    first_failure = next(index for index, check in enumerate(checks) if not check["passed"])
    assert first_failure > 0
    assert all(check["evaluated"] and check["passed"] for check in checks[:first_failure])
    assert checks[first_failure]["name"] == "mode_may_submit"
    assert checks[first_failure]["evaluated"]


def _assert_no_activity(state: dict[str, Any]) -> None:
    assert all(row["orders_submitted"] == 0 for row in state["activity"])
    assert all(Decimal(str(row["turnover_usd"])) == 0 for row in state["activity"])


class _AfterAdmission(RuntimeError):
    """Synthetic failure after a real SHADOW refusal, before the drain commits."""


@pytest.mark.parametrize("case", ["shadow", "expired", "rollback"])
def test_phase_b_shadow_path(
    case: str,
    phase_b_settings: tuple[Settings, str, str],
    worker_logs: _WorkerLogs,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings, token, entry_digest = phase_b_settings
    db_path = tmp_path / "chronos.db"
    submissions: list[object] = []
    original_submit = DemoBroker.submit_order

    async def observe_submit(broker: DemoBroker, request: Any, **kwargs: Any) -> Any:
        submissions.append(request)
        return await original_submit(broker, request, **kwargs)

    monkeypatch.setattr(DemoBroker, "submit_order", observe_submit)
    origin = httpx.URL(f"http://{settings.backend_host}:{settings.backend_port}")
    app = create_app()
    received: list[tuple[str, str]] = []
    requests: list[httpx.Request] = []
    responses: list[httpx.Response] = []
    model_requests: list[httpx.Request] = []
    provisional: list[dict[str, Any]] = []

    @app.middleware("http")
    async def record_route(request: Request, call_next: Any) -> Any:
        received.append((request.method, request.url.path))
        return await call_next(request)

    with TestClient(app, base_url=str(origin)) as started:
        runtime = app.state.backend.runtime
        autonomy = app.state.autonomy
        assert runtime.settings is settings
        assert isinstance(runtime.broker, DemoBroker)
        assert not runtime.settings.allow_order_transmit and not runtime.settings.allow_live_trading
        assert app.state.backend.may_write and app.state.backend.recovery_hold is None
        assert app.state.backend.startup_faults == ()
        assert autonomy is not None
        assert autonomy.mandate.mode is AutonomyMode.SHADOW
        assert autonomy.mandate.mode in NON_SUBMITTING_AUTONOMY_MODES
        assert autonomy.mandate.account_fingerprint == _FINGERPRINT
        config = load_config(
            {
                "CHRONOS_WORKER_PROVIDER": "local",
                "CHRONOS_WORKER_MODEL": _MODEL,
                "CHRONOS_WORKER_API_TOKEN": app.state.api_token,
                "CHRONOS_WORKER_PROPOSER_TOKEN": token,
                "CHRONOS_WORKER_SYMBOLS": "SPY",
                "CHRONOS_WORKER_KINDS": "HOLD",
                "CHRONOS_WORKER_POLICY_FILE": str(tmp_path / "policy.md"),
                "CHRONOS_WORKER_LOOKBACK_DAYS": "5",
                "CHRONOS_WORKER_FORWARD": "true",
            }
        )
        assert config.forward is True

        def backend_adapter(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert (request.url.scheme, request.url.host, request.url.port) == (
                origin.scheme,
                origin.host,
                origin.port,
            )
            response = started.request(
                request.method, str(request.url), headers=request.headers, content=request.content
            )
            responses.append(response)
            return httpx.Response(
                response.status_code, headers=response.headers, content=response.content
            )

        def model_adapter(request: httpx.Request) -> httpx.Response:
            model_requests.append(request)
            assert request.method == "POST"
            assert str(request.url) == f"{config.local_base_url}/chat/completions"
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

        before = _committed_state(db_path)
        assert before["queue"] == before["bundles"] == before["attempts"] == []
        assert _journal(before, _DECISIONS) == _journal(before, _CYCLES) == []
        _assert_no_activity(before)
        with (
            httpx.Client(
                transport=httpx.MockTransport(backend_adapter), trust_env=False
            ) as backend,
            httpx.Client(transport=httpx.MockTransport(model_adapter), trust_env=False) as model,
        ):
            outcome = run_cycle(config, backend=backend, anthropic=model)
        assert outcome is CycleOutcome.FORWARDED
        assert any(
            message.startswith("HOLD proposal queued (stage=QUEUED)")
            for message in worker_logs.messages
        )
        assert (
            received
            == [(request.method, request.url.path) for request in requests]
            == [
                ("POST", "/autonomy/evidence"),
                ("POST", "/autonomy/proposals"),
            ]
        )
        assert [response.status_code for response in responses] == [201, 202]
        assert len(model_requests) == 1
        queued = _committed_state(db_path)
        assert len(queued["queue"]) == len(queued["bundles"]) == 1
        row, bundle = queued["queue"][0], queued["bundles"][0]
        assert row["status"] == "PENDING"
        assert row["payload"].encode("utf-8") == requests[1].content
        assert row["proposer_id"] == bundle["proposer_id"] == _MODEL
        assert (
            row["proposer_credential_epoch"]
            == bundle["proposer_credential_epoch"]
            == (hashlib.sha256(token.encode()).hexdigest())
        )
        assert (
            row["proposer_registry_entry_digest"]
            == bundle["proposer_registry_entry_digest"]
            == (entry_digest)
        )
        assert queued["attempts"] == _journal(queued, _DECISIONS) == []
        issued = responses[0].json()
        proposal = json.loads(requests[1].content)
        assert (proposal["kind"], proposal["symbol"], proposal["direction"]) == (
            "HOLD",
            "SPY",
            "NEUTRAL",
        )
        assert len(proposal["evidence"]) == 1
        citation = proposal["evidence"][0]
        assert citation["evidence_id"] == issued["bundle_id"] == bundle["bundle_id"]
        model_body = json.loads(model_requests[0].content)
        assert model_body["model"] == _MODEL
        prompt = next(
            message["content"] for message in model_body["messages"] if message["role"] == "user"
        )
        framing, canonical, watchlist = prompt.split("\n\n")
        assert canonical == issued["document"]
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        assert digest == citation["digest"] == issued["digest"] == bundle["digest"]
        issuance = [
            event for event in queued["journal"] if event["kind"] == "evidence_bundle_issued"
        ]
        assert len(issuance) == 1
        issuance_payload = json.loads(issuance[0]["payload_json"])
        assert issuance_payload["bundle_id"] == bundle["bundle_id"]
        assert issuance_payload["digest"] == digest
        assert issuance_payload["proposer_id"] == _MODEL
        assert [event for event in queued["journal"] if event["kind"] == "proposal_accepted"] == []
        assert digest in framing and citation["as_of"] in framing
        assert citation["as_of"] == issued["issued_at"]
        assert "Watchlist: SPY." in watchlist

        def fail_after_admission(session: Session, **_kwargs: Any) -> None:
            session.flush()
            admissions = list(
                session.scalars(
                    select(HashChainRow).where(
                        HashChainRow.stream == _DECISIONS,
                    )
                )
            )
            assert len(admissions) == 1 and admissions[0].kind == "refused"
            payload = json.loads(admissions[0].payload_json)
            _assert_mode_refusal(payload)
            attempts = list(session.scalars(select(AutonomyDecisionAttemptRow)))
            assert len(attempts) == 1 and attempts[0].decision_id == payload["decision_id"]
            assert not attempts[0].admitted and attempts[0].refusals == 1
            provisional.append(payload)
            raise _AfterAdmission

        if case == "rollback":
            monkeypatch.setattr("chronos.supervisor.proposals.mark_processed", fail_after_admission)
        tick_at = datetime.now(UTC)
        if case == "expired":
            tick_at = datetime.fromisoformat(issued["expires_at"]) + timedelta(seconds=1)
            assert tick_at < autonomy.mandate.expires_at
        report = autonomy.run_tick(tick_at)
        after = _committed_state(db_path)
        assert after["bundles"] == queued["bundles"]
        assert [
            event for event in after["journal"] if event["kind"] == "evidence_bundle_issued"
        ] == (issuance)
        assert len(after["queue"]) == 1 and after["queue"][0]["id"] == row["id"]
        admissions = _journal(after, _DECISIONS)
        cycles = _journal(after, _CYCLES)
        if case == "rollback":
            assert len(provisional) == 1
            assert report.failure == "tick raised _AfterAdmission"
            assert report.proposals_judged == 0 and report.outcomes == []
            assert after["queue"] == queued["queue"]
            assert after["attempts"] == admissions == cycles == []
            failure_events = [
                event
                for event in after["journal"]
                if event["kind"] == "alert_raised"
                and json.loads(event["payload_json"])["kind"] == "runtime.tick_failed"
            ]
            assert len(failure_events) == 1
            assert [event for event in after["journal"] if event not in failure_events] == (
                queued["journal"]
            )
            assert any(alert["kind"] == "runtime.tick_failed" for alert in after["alerts"])
        else:
            assert report.ok, report.failure
            assert report.proposals_judged == len(report.outcomes) == 1
            judged = report.outcomes[0]
            expected = (
                ("STAMP", "EVIDENCE_BUNDLE_EXPIRED")
                if case == "expired"
                else (
                    "ADMISSION",
                    "MODE_CANNOT_SUBMIT",
                )
            )
            assert (judged.stage.value, judged.refusal) == expected
            assert (
                after["queue"][0]["status"],
                after["queue"][0]["cycle_stage"],
                after["queue"][0]["refusal"],
            ) == ("PROCESSED", *expected)
            assert len(cycles) == 1
            cycle = json.loads(cycles[0]["payload_json"])
            assert (cycle["stage"], cycle["refusal"]) == expected
            if case == "expired":
                assert after["attempts"] == admissions == []
            else:
                assert len(admissions) == len(after["attempts"]) == 1
                assert admissions[0]["kind"] == "refused"
                payload = json.loads(admissions[0]["payload_json"])
                _assert_mode_refusal(payload)
                attempt = after["attempts"][0]
                assert attempt["decision_id"] == payload["decision_id"] == judged.decision_id
                assert cycle["decision_id"] == judged.decision_id
                assert not attempt["admitted"] and attempt["refusals"] == 1
                assert judged.decision is not None
                assert judged.decision.provenance.proposer_id == _MODEL
                assert judged.decision.provenance.evidence_bundle_id == issued["bundle_id"]
                stamped = [
                    event for event in after["journal"] if event["kind"] == "proposal_accepted"
                ]
                assert len(stamped) == 1
                assert json.loads(stamped[0]["payload_json"])["decision_id"] == judged.decision_id
        assert report.orders_handed_off == report.orders_confirmed == report.orders_unconfirmed == 0
        assert report.orders_rejected_after_send == report.handoff_refusals == 0
        _assert_no_activity(after)
        assert submissions == []
        with runtime.database.sessions() as session:
            for stream in {event["stream"] for event in after["journal"]}:
                verification = hash_chain.verify(session, stream)
                assert verification.ok and verification.records > 0

    assert submissions == []
    assert _committed_state(db_path) == after
    _assert_no_activity(_committed_state(db_path))
