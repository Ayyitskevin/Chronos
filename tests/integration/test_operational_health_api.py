from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chronos.api.main import create_app
from chronos.api.routes.health import router as health_router
from chronos.config.settings import get_settings
from chronos.operations import clock as clock_module
from chronos.operations.health import ReasonCode, StartupFaultCode
from chronos.persistence.database import Database
from chronos.utils.locking import WriterLease


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
    assert body["observations"]["clock_evidence"] == {
        "provider": "disabled",
        "observation_state": "UNKNOWN",
        "age_seconds": None,
        "maximum_error_seconds": None,
        "maximum_allowed_error_seconds": None,
        "failure_code": "disabled",
        "generation": 0,
    }


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


def test_enabled_clock_monitor_samples_once_and_health_reads_only_the_cache(
    health_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del health_env
    calls = 0
    output = "\n".join(
        (
            "Reference ID    : C0A80101 (ntp.example.invalid)",
            "System time     : 0.001 seconds slow of NTP time",
            "Root delay      : 0.002 seconds",
            "Root dispersion : 0.001 seconds",
            "Leap status     : Normal",
        )
    )

    def fake_chronyc(_: float) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            args=("/usr/bin/chronyc", "-n", "tracking"),
            returncode=0,
            stdout=output,
            stderr="",
        )

    monkeypatch.setenv("CLOCK_HEALTH_PROVIDER", "chrony")
    monkeypatch.setenv("CLOCK_HEALTH_MAXIMUM_ERROR_SECONDS", "0.01")
    monkeypatch.setenv("CLOCK_HEALTH_POLL_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("CLOCK_HEALTH_OBSERVATION_MAX_AGE_SECONDS", "7200")
    monkeypatch.setattr(clock_module, "_run_chronyc", fake_chronyc)
    get_settings.cache_clear()

    with TestClient(create_app()) as test_client:
        initial_calls = calls
        bodies = [test_client.get("/health").json() for _ in range(12)]

    assert initial_calls == 1
    assert calls == 1
    assert {body["observations"]["clock"] for body in bodies} == {"SYNCHRONIZED"}
    evidence = bodies[-1]["observations"]["clock_evidence"]
    assert evidence["provider"] == "chrony"
    assert evidence["observation_state"] == "CURRENT"
    assert evidence["maximum_error_seconds"] == pytest.approx(0.003)
    assert evidence["maximum_allowed_error_seconds"] == 0.01
    assert evidence["failure_code"] is None
    assert evidence["generation"] == 1
    serialized = json.dumps(bodies[-1], sort_keys=True)
    assert "ntp.example.invalid" not in serialized
    assert "C0A80101" not in serialized


def test_read_only_backend_also_samples_clock_health(
    health_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = "\n".join(
        (
            "Reference ID    : C0A80101",
            "System time     : 0.001 seconds fast of NTP time",
            "Root delay      : 0.002 seconds",
            "Root dispersion : 0.001 seconds",
            "Leap status     : Normal",
        )
    )

    def fake_chronyc(_: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=("/usr/bin/chronyc", "-n", "tracking"),
            returncode=0,
            stdout=output,
            stderr="",
        )

    monkeypatch.setenv("CLOCK_HEALTH_PROVIDER", "chrony")
    monkeypatch.setenv("CLOCK_HEALTH_MAXIMUM_ERROR_SECONDS", "0.01")
    monkeypatch.setattr(clock_module, "_run_chronyc", fake_chronyc)
    get_settings.cache_clear()
    database = Database(f"sqlite:///{health_env / 'chronos.db'}")
    database.initialize()
    lease = WriterLease(database.sessions, holder="clock-test-writer")
    assert lease.acquire() is True
    try:
        with TestClient(create_app()) as test_client:
            body = test_client.get("/health").json()
            assert body["read_only"] is True
            assert body["observations"]["clock"] == "SYNCHRONIZED"
            assert body["service_readiness"]["state"] == "READY"
    finally:
        lease.release()
        database.dispose()


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
