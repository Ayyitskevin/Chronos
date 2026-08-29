"""Backend API in demo mode: auth, read endpoints, read-only lease fallback.

Runs the real FastAPI app through its lifespan against the DemoBroker — no
IBKR, no network beyond loopback TestClient calls, and (structurally) no
order endpoints exist at this milestone.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chronos.api.main import create_app
from chronos.config.settings import get_settings
from chronos.orders.service import OrderManagementService
from chronos.persistence.database import Database
from chronos.utils.locking import WriterLease


@pytest.fixture()
def demo_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    monkeypatch.setenv("BACKEND_TOKEN_FILE", str(tmp_path / "backend_api_token"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture()
def client(demo_env: Path) -> Iterator[TestClient]:
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def _token(demo_env: Path) -> str:
    return (demo_env / "backend_api_token").read_text(encoding="utf-8").strip()


def test_health_requires_no_token(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["broker_mode"] == "demo"
    assert body["read_only"] is False
    assert body["writer_lease_held"] is True


def test_account_endpoints_reject_missing_token(client: TestClient) -> None:
    for path in ("/account/summary", "/account/positions", "/strategy/reconciliation"):
        assert client.get(path).status_code == 401


def test_account_endpoints_reject_wrong_token(client: TestClient) -> None:
    response = client.get("/account/summary", headers={"X-Chronos-Token": "wrong"})
    assert response.status_code == 401


def test_account_summary_round_trip(client: TestClient, demo_env: Path) -> None:
    response = client.get("/account/summary", headers={"X-Chronos-Token": _token(demo_env)})
    assert response.status_code == 200
    body = response.json()
    assert body["account_id"]
    assert "net_liquidation" in body


def test_positions_round_trip(client: TestClient, demo_env: Path) -> None:
    response = client.get("/account/positions", headers={"X-Chronos-Token": _token(demo_env)})
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_reconciliation_round_trip(client: TestClient, demo_env: Path) -> None:
    response = client.get("/strategy/reconciliation", headers={"X-Chronos-Token": _token(demo_env)})
    assert response.status_code == 200
    assert "status" in response.json()


def test_candidates_rejects_non_allowlisted_symbol(client: TestClient, demo_env: Path) -> None:
    response = client.post(
        "/strategy/candidates/TSLA", headers={"X-Chronos-Token": _token(demo_env)}
    )
    assert response.status_code == 403


def test_candidates_round_trip(client: TestClient, demo_env: Path) -> None:
    response = client.post(
        "/strategy/candidates/AAPL", headers={"X-Chronos-Token": _token(demo_env)}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "AAPL"
    assert "status" in body


def test_second_backend_starts_read_only(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Hold the lease externally, as a first backend would.
    settings_db = f"sqlite:///{demo_env / 'chronos.db'}"
    database = Database(settings_db)
    database.initialize()
    external = WriterLease(database.sessions, holder="test-first-writer")
    assert external.acquire() is True
    try:

        def unexpected_reconciliation(
            self: OrderManagementService, *, now: object = None
        ) -> object:
            del self, now
            raise AssertionError("read-only startup must skip reconciliation")

        app = create_app()
        with TestClient(app) as test_client:
            health = test_client.get("/health").json()
            assert health["read_only"] is True
            assert health["writer_lease_held"] is False
            assert health["service_readiness"]["state"] == "READY"
            assert health["trading_capability"]["paper_new_exposure"]["state"] == "BLOCKED"
            assert (
                "writer_lease_absent"
                in health["trading_capability"]["paper_new_exposure"]["reasons"]
            )
    finally:
        external.release()
        database.dispose()


def test_no_order_endpoints_exist_yet(client: TestClient) -> None:
    # Milestone 1 exposes read endpoints only: nothing that could submit,
    # modify, or cancel an order is routed at all.
    app_routes = {getattr(route, "path", "") for route in client.app.routes}
    assert not any("order" in path for path in app_routes)
    assert not any("live" in path for path in app_routes)
