"""Milestone 4 strategy endpoints over the real app in demo mode.

Runs the real FastAPI app through its lifespan against the DemoBroker. The
happy-path flow uses the deterministic ``empty_account`` demo profile (the
only profile whose reconciled, flat account produces ELIGIBLE candidates);
the default ``safety_cases`` profile is exercised separately to prove the
rehearsal endpoints answer 200 with a faithful WITHHELD shape when demo
evidence locks the flow. Nothing here can submit an order.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chronos.api.main import create_app
from chronos.config.settings import get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def demo_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    # The regime endpoint reads research/data/raw relative to the process cwd.
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


def _eligible_candidate(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    response = client.post("/strategy/candidates/AAPL", headers=headers)
    assert response.status_code == 200
    evaluation = response.json()
    assert evaluation["status"] == "ELIGIBLE"
    candidates = evaluation["resolution"]["candidates"]
    assert candidates
    first: dict[str, Any] = candidates[0]
    return first


def test_regime_requires_token(client: TestClient) -> None:
    assert client.get("/strategy/regime/SPY").status_code == 401


def test_regime_spy_available_with_note(client: TestClient, headers: dict[str, str]) -> None:
    response = client.get("/strategy/regime/SPY", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "SPY"
    assert body["available"] is True
    assert body["bars"] >= 210
    assert isinstance(body["as_of_session"], str)
    assert body["trend_state"] in {"UPTREND", "DOWNTREND", "MIXED"}
    for field in ("ema_fast", "ema_slow", "rsi2", "vol_percentile"):
        assert isinstance(body[field], str)
    assert "heuristic context — not a validated signal" in body["note"]
    assert isinstance(body["reasons"], list)


def test_regime_msft_unavailable_still_200(client: TestClient, headers: dict[str, str]) -> None:
    response = client.get("/strategy/regime/MSFT", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "MSFT"
    assert body["available"] is False
    assert body["trend_state"] is None
    assert body["ema_fast"] is None
    assert "no local daily history for MSFT" in body["reasons"]
    assert "heuristic context — not a validated signal" in body["note"]


def test_regime_rejects_non_allowlisted_symbol(client: TestClient, headers: dict[str, str]) -> None:
    assert client.get("/strategy/regime/TSLA", headers=headers).status_code == 403


def test_risk_preview_rejects_non_allowlisted_symbol(
    client: TestClient, headers: dict[str, str]
) -> None:
    body = {"symbol": "TSLA", "selected_contract_id": 1, "total_commission_estimate": "1.05"}
    assert client.post("/strategy/risk-preview", json=body, headers=headers).status_code == 403


def test_risk_preview_round_trip(client: TestClient, headers: dict[str, str]) -> None:
    candidate = _eligible_candidate(client, headers)
    con_id = candidate["contract"]["con_id"]
    response = client.post(
        "/strategy/risk-preview",
        json={
            "symbol": "AAPL",
            "selected_contract_id": con_id,
            "total_commission_estimate": "1.05",
        },
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "READY"
    assert body["opening_actions_locked"] is True
    preview = body["preview"]
    assert preview is not None
    assert preview["symbol"] == "AAPL"
    assert preview["selected_contract"]["con_id"] == con_id
    # Decimal money fields serialize as strings through ChronosModel.
    assert isinstance(preview["gross_assignment_obligation"], str)
    assert isinstance(preview["net_premium"], str)


def test_what_if_and_approval_round_trip(client: TestClient, headers: dict[str, str]) -> None:
    candidate = _eligible_candidate(client, headers)
    con_id = candidate["contract"]["con_id"]
    limit_price = candidate["bid"]

    risk = client.post(
        "/strategy/risk-preview",
        json={
            "symbol": "AAPL",
            "selected_contract_id": con_id,
            "total_commission_estimate": "1.05",
        },
        headers=headers,
    ).json()
    assert risk["status"] == "READY"
    obligation = risk["preview"]["gross_assignment_obligation"]

    what_if = client.post(
        "/strategy/demo-what-if",
        json={
            "symbol": "AAPL",
            "selected_contract_id": con_id,
            "total_commission_estimate": "1.05",
            "limit_price": limit_price,
        },
        headers=headers,
    )
    assert what_if.status_code == 200
    what_if_body = what_if.json()
    assert what_if_body["status"] == "WHAT_IF_PREVIEWED"
    assert what_if_body["preview"] is not None
    assert what_if_body["opening_actions_locked"] is True

    approval = client.post(
        "/strategy/demo-approval",
        json={
            "symbol": "AAPL",
            "selected_contract_id": con_id,
            "total_commission_estimate": "1.05",
            "limit_price": limit_price,
            "gross_assignment_obligation": obligation,
            "typed_symbol": "AAPL",
            "acknowledged_quantity": 1,
            "acknowledged_contract_id": con_id,
            "acknowledged_limit_price": limit_price,
            "acknowledged_gross_assignment_obligation": obligation,
            "risk_terms_acknowledged": True,
        },
        headers=headers,
    )
    assert approval.status_code == 200
    approval_body = approval.json()
    assert approval_body["status"] == "APPROVAL_REHEARSED"
    assert approval_body["receipt"] is not None
    assert approval_body["opening_actions_locked"] is True


def test_demo_what_if_rejects_non_allowlisted_symbol(
    client: TestClient, headers: dict[str, str]
) -> None:
    body = {
        "symbol": "TSLA",
        "selected_contract_id": 1,
        "total_commission_estimate": "1.05",
        "limit_price": "3.15",
    }
    assert client.post("/strategy/demo-what-if", json=body, headers=headers).status_code == 403


def test_post_endpoints_require_token(client: TestClient) -> None:
    for path in ("/strategy/risk-preview", "/strategy/demo-what-if", "/strategy/demo-approval"):
        assert client.post(path, json={}).status_code == 401


class TestSafetyCasesProfile:
    """The default demo profile locks the flow: endpoints still answer 200, WITHHELD."""

    @pytest.fixture()
    def safety_client(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> Iterator[tuple[TestClient, dict[str, str]]]:
        monkeypatch.chdir(REPO_ROOT)
        monkeypatch.setenv("BROKER_MODE", "demo")
        monkeypatch.delenv("DEMO_PROFILE", raising=False)
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
        monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
        monkeypatch.setenv("BACKEND_TOKEN_FILE", str(tmp_path / "backend_api_token"))
        get_settings.cache_clear()
        app = create_app()
        with TestClient(app) as test_client:
            token = (tmp_path / "backend_api_token").read_text(encoding="utf-8").strip()
            yield test_client, {"X-Chronos-Token": token}
        get_settings.cache_clear()

    def test_risk_preview_withheld_shape(
        self, safety_client: tuple[TestClient, dict[str, str]]
    ) -> None:
        client, headers = safety_client
        evaluation = client.post("/strategy/candidates/AAPL", headers=headers).json()
        assert evaluation["status"] == "NO_TRADE"
        assert evaluation["resolution"] is None
        response = client.post(
            "/strategy/risk-preview",
            json={
                "symbol": "AAPL",
                "selected_contract_id": 2002,
                "total_commission_estimate": "1.05",
            },
            headers=headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "WITHHELD"
        assert body["preview"] is None
        assert body["reasons"]
        assert body["opening_actions_locked"] is True
