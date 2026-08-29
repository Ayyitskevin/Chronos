from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chronos.api.main import create_app
from chronos.api.routes.health import router as health_router
from chronos.config.settings import get_settings
from chronos.operations.health import ReasonCode, StartupFaultCode


@pytest.fixture()
def health_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    monkeypatch.setenv("BACKEND_TOKEN_FILE", str(tmp_path / "backend_api_token"))
    monkeypatch.setenv("LIVE_KILL_SWITCH_FILE", str(tmp_path / "kill.json"))
    monkeypatch.setenv("SESSION_BASELINE_FILE", str(tmp_path / "baseline.json"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture()
def client(health_env: Path) -> Iterator[TestClient]:
    del health_env
    with TestClient(create_app()) as test_client:
        yield test_client


def test_schema_v2_separates_answering_readiness_and_capability(client: TestClient) -> None:
    response = client.get("/health")
    body = response.json()

    assert response.status_code == 200
    assert body["schema_version"] == 2
    assert body["status"] == "ok"
    assert body["status_scope"] == "compatibility_only"
    assert body["liveness"] == {"state": "LIVE", "reasons": []}
    assert body["service_readiness"] == {"state": "READY", "reasons": []}
    assert body["trading_capability"]["paper_new_exposure"]["state"] == "BLOCKED"
    assert "lane_not_configured" in body["trading_capability"]["paper_new_exposure"]["reasons"]
    assert body["observations"]["clock"] == "UNKNOWN"


def test_compatibility_fields_keep_their_original_types(client: TestClient) -> None:
    body = client.get("/health").json()

    assert isinstance(body["status"], str)
    assert isinstance(body["broker_mode"], str)
    assert isinstance(body["environment"], str)
    assert isinstance(body["read_only"], bool)
    assert isinstance(body["writer_lease_held"], bool)
    assert isinstance(body["reconciliation_status"], str)
    assert isinstance(body["reconciliation_generation"], int)


def test_health_polling_does_not_call_the_broker(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = client.app.state.backend.runtime.broker

    async def forbidden() -> object:
        raise AssertionError("health polling must consume the cached observation")

    monkeypatch.setattr(broker, "connection_status", forbidden)

    generations = {
        client.get("/health").json()["observations"]["broker"]["generation"] for _ in range(12)
    }

    assert len(generations) == 1


def test_store_read_failure_is_not_hidden_by_compatibility_ok(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = client.app.state.backend.runtime.database
    monkeypatch.setattr(database, "readable", lambda: False)

    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["service_readiness"]["state"] == "NOT_READY"
    assert body["service_readiness"]["reasons"] == [ReasonCode.STORE_UNREADABLE]
    assert body["observations"]["store_readable"] is False


def test_disconnected_cache_blocks_without_a_remote_health_probe(client: TestClient) -> None:
    runtime = client.app.state.backend.runtime
    runtime.connection.disconnect()

    body = client.get("/health").json()

    broker = body["observations"]["broker"]
    assert broker["connected"] is None
    assert broker["observation_state"] == "CURRENT"
    assert (
        "broker_connection_unknown" in body["trading_capability"]["paper_new_exposure"]["reasons"]
    )


def test_broker_loop_failure_blocks_trading_but_not_operator_reads(client: TestClient) -> None:
    client.app.state.backend.runtime.connection.close()

    body = client.get("/health").json()

    assert body["service_readiness"]["state"] == "READY"
    assert body["observations"]["broker_loop_running"] is False
    assert "broker_loop_down" in body["trading_capability"]["paper_new_exposure"]["reasons"]


def test_pending_reconciliation_blocks_trading_but_not_operator_reads(
    client: TestClient,
) -> None:
    readiness = client.app.state.backend.runtime.reconciliation_readiness
    readiness.invalidate("test lifecycle uncertainty")

    body = client.get("/health").json()

    assert body["service_readiness"]["state"] == "READY"
    assert body["observations"]["reconciliation"]["status"] == "PENDING"
    assert "reconciliation_not_ready" in body["trading_capability"]["paper_new_exposure"]["reasons"]


def test_sanitized_startup_fault_makes_service_not_ready(client: TestClient) -> None:
    backend = client.app.state.backend
    backend.note_startup_fault(StartupFaultCode.PROPOSER_REGISTRY_INVALID)

    body = client.get("/health").json()

    assert body["service_readiness"] == {
        "state": "NOT_READY",
        "reasons": ["startup_degraded"],
    }
    assert body["observations"]["startup_faults"] == ["proposer_registry_invalid"]


def test_unauthenticated_health_body_never_discloses_sensitive_inputs(
    client: TestClient,
) -> None:
    body = json.dumps(client.get("/health").json(), sort_keys=True)

    for forbidden in ("du1234567", "account_id", "fingerprint", "token", "mandate_id"):
        assert forbidden not in body.lower()


def test_app_without_lifespan_reports_starting_not_ready() -> None:
    app = FastAPI()
    app.include_router(health_router)
    with TestClient(app) as client:
        body = client.get("/health").json()

    assert body["status"] == "starting"
    assert body["liveness"]["state"] == "LIVE"
    assert body["service_readiness"]["state"] == "STARTING"
