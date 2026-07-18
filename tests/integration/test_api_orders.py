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
