"""Milestone 5 order endpoints over the real app in demo mode.

Runs the real FastAPI app through its lifespan against the DemoBroker. The
demo broker cannot transmit, so nothing here places an order; these tests pin
the gating (token, allowlist, not-found) and that the order router is wired.
The full propose->submit lifecycle is proven at the service level in
tests/integration/test_order_pipeline.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chronos.api.main import create_app
from chronos.config.settings import get_settings
from chronos.orders.service import OrderManagementService

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def demo_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DEMO_PROFILE", "empty_account")
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


@pytest.fixture()
def headers(demo_env: Path) -> dict[str, str]:
    token = (demo_env / "backend_api_token").read_text(encoding="utf-8").strip()
    return {"X-Chronos-Token": token}


def test_orders_require_token(client: TestClient) -> None:
    assert client.get("/orders").status_code == 401


def test_list_orders_empty(client: TestClient, headers: dict[str, str]) -> None:
    response = client.get("/orders", headers=headers)
    assert response.status_code == 200
    assert response.json() == []


def test_startup_and_operator_reconcile_publish_readiness(
    client: TestClient,
    headers: dict[str, str],
    demo_env: Path,
) -> None:
    health = client.get("/health").json()
    assert health["reconciliation_status"] == "RECONCILED"
    assert health["reconciliation_generation"] >= 1

    response = client.post("/orders/reconcile", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "RECONCILED"
    assert body["unresolved_count"] == 0
    assert body["remaining_active_count"] == 0
    log_text = (demo_env / "chronos.log").read_text(encoding="utf-8")
    assert '"operation":"operator_reconcile"' in log_text
    assert '"reconciliation_status":"RECONCILED"' in log_text
    assert '"outcome":"ready"' in log_text


def test_get_unknown_order_404(client: TestClient, headers: dict[str, str]) -> None:
    assert client.get("/orders/does-not-exist", headers=headers).status_code == 404


def test_propose_non_allowlisted_symbol_403(client: TestClient, headers: dict[str, str]) -> None:
    body = {
        "symbol": "ZZZZ",
        "product_family": "STOCK",
        "intent": "OPEN_LONG_STOCK",
        "quantity": 1,
        "limit_price": "100.00",
    }
    response = client.post("/orders/propose", json=body, headers=headers)
    assert response.status_code == 403


def test_cancel_unknown_order_conflicts(client: TestClient, headers: dict[str, str]) -> None:
    # No such working order -> the mutation service refuses (409), never 200.
    response = client.post("/orders/does-not-exist/cancel", headers=headers)
    assert response.status_code == 409


def test_propose_non_day_tif_for_stock_is_422(client: TestClient, headers: dict[str, str]) -> None:
    # Adversarial-review C2: only CRYPTO may set a non-DAY TIF; an option/stock
    # IOC request is rejected explicitly, not silently coerced to DAY.
    body = {
        "symbol": "AAPL",
        "product_family": "STOCK",
        "intent": "OPEN_LONG_STOCK",
        "quantity": 1,
        "limit_price": "100.00",
        "time_in_force": "IOC",
    }
    response = client.post("/orders/propose", json=body, headers=headers)
    assert response.status_code == 422
    assert "non-DAY" in response.json()["detail"]


def test_startup_reconciliation_failure_keeps_api_available_and_locked(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_reconciliation(self: OrderManagementService, *, now: object = None) -> object:
        del self, now
        raise RuntimeError("sensitive broker payload DU1234567 balance 250000")

    monkeypatch.setattr(OrderManagementService, "reconcile_on_restart_report", fail_reconciliation)
    app = create_app()
    with TestClient(app) as test_client:
        headers = {"X-Chronos-Token": (demo_env / "backend_api_token").read_text().strip()}
        health = test_client.get("/health").json()
        assert health["reconciliation_status"] == "PENDING"
        assert health["writer_lease_held"] is True
        assert test_client.get("/orders", headers=headers).status_code == 200
        response = test_client.post("/orders/reconcile", headers=headers)
        assert response.status_code == 409
        assert "submission remains locked" in response.json()["detail"]

    log_text = (demo_env / "chronos.log").read_text(encoding="utf-8")
    assert '"event":"submission_reconciliation_failed"' in log_text
    assert '"error_type":"RuntimeError"' in log_text
    assert '"operation":"operator_reconcile"' in log_text
    assert "DU1234567" not in log_text


@pytest.fixture()
def crypto_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DEMO_PROFILE", "empty_account")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    monkeypatch.setenv("BACKEND_TOKEN_FILE", str(tmp_path / "backend_api_token"))
    # Tuple settings from env are JSON-decoded by pydantic-settings.
    monkeypatch.setenv("CRYPTO_ALLOWLIST", '["DOGE"]')
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture()
def crypto_client(crypto_env: Path) -> Iterator[TestClient]:
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def crypto_headers(crypto_env: Path) -> dict[str, str]:
    token = (crypto_env / "backend_api_token").read_text(encoding="utf-8").strip()
    return {"X-Chronos-Token": token}


def test_propose_unqualifiable_crypto_is_502_not_500(
    crypto_client: TestClient, crypto_headers: dict[str, str]
) -> None:
    # Adversarial-review C3: an allowlisted-but-unqualifiable crypto symbol makes
    # the broker raise BrokerError; propose maps that to 502, not a bare 500.
    # DOGE is on the allowlist but the DemoBroker has no such crypto fixture.
    body = {
        "symbol": "DOGE",
        "product_family": "CRYPTO",
        "intent": "OPEN_LONG_CRYPTO",
        "quantity": "0.5",
        "limit_price": "0.10",
    }
    response = crypto_client.post("/orders/propose", json=body, headers=crypto_headers)
    assert response.status_code == 502
