"""Milestone 6 live safety endpoints over the real app in demo mode.

Exercises arming, status, and the kill switch through the real FastAPI app +
lifespan against the DemoBroker. Durable flag files are redirected to tmp_path
so tests never touch the repo. Nothing here enables live transmission.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chronos.api.main import create_app
from chronos.config.settings import get_settings
from chronos.orders.arming import REQUIRED_ARM_PHRASE

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def demo_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DEMO_PROFILE", "empty_account")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    monkeypatch.setenv("BACKEND_TOKEN_FILE", str(tmp_path / "backend_api_token"))
    monkeypatch.setenv("LIVE_KILL_SWITCH_FILE", str(tmp_path / "kill.json"))
    monkeypatch.setenv("SESSION_BASELINE_FILE", str(tmp_path / "baseline.json"))
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


def test_live_status_requires_token(client: TestClient) -> None:
    assert client.get("/live/status").status_code == 401


def test_status_fresh_is_disarmed_and_disengaged(
    client: TestClient, headers: dict[str, str]
) -> None:
    body = client.get("/live/status", headers=headers).json()
    assert body["arm"]["armed"] is False
    assert body["kill_switch"]["engaged"] is False


def test_arm_with_correct_phrase_then_status_armed(
    client: TestClient, headers: dict[str, str]
) -> None:
    armed = client.post("/live/arm", json={"phrase": REQUIRED_ARM_PHRASE}, headers=headers)
    assert armed.status_code == 200
    assert armed.json()["armed"] is True
    status_body = client.get("/live/status", headers=headers).json()
    assert status_body["arm"]["armed"] is True


def test_arm_with_wrong_phrase_rejected_without_echo(
    client: TestClient, headers: dict[str, str]
) -> None:
    response = client.post("/live/arm", json={"phrase": "nope"}, headers=headers)
    assert response.status_code == 400
    assert "nope" not in response.text
    assert REQUIRED_ARM_PHRASE not in response.text


def test_kill_switch_engage_blocks_and_disengage_requires_note(
    client: TestClient, headers: dict[str, str]
) -> None:
    engaged = client.post("/live/kill", json={"reason": "manual test halt"}, headers=headers)
    assert engaged.status_code == 200
    assert engaged.json()["engaged"] is True
    assert client.get("/live/status", headers=headers).json()["kill_switch"]["engaged"] is True
    # Disengage needs a non-empty note.
    empty_note = client.post("/live/kill/disengage", json={"note": "  "}, headers=headers)
    assert empty_note.status_code == 422
    ok = client.post("/live/kill/disengage", json={"note": "reviewed"}, headers=headers)
    assert ok.status_code == 200
    assert ok.json()["engaged"] is False


def test_disarm_after_arm(client: TestClient, headers: dict[str, str]) -> None:
    client.post("/live/arm", json={"phrase": REQUIRED_ARM_PHRASE}, headers=headers)
    disarmed = client.post("/live/disarm", headers=headers)
    assert disarmed.status_code == 200
    assert disarmed.json()["armed"] is False
