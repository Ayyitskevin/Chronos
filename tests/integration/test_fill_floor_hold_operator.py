"""FLOORHOLD: what the operator sees. The reply of ``POST /orders/reconcile`` names each fill-floor
hold with the intent id, the basis and both figures, and nothing else (no account, no order ref, no
broker id); the run row and the log carry the same. A pass without a hold replies with none."""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chronos.api.main import create_app
from chronos.config.settings import get_settings
from chronos.domain.enums import OrderLifecycle
from chronos.orders.reconciliation_recovery import (
    FillFloorBasis,
    FillFloorHold,
    OrderRestartReconciliationReport,
    RemainingActiveIntent,
    RestartOrderObservation,
)
from chronos.orders.service import OrderManagementService

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def demo_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DEMO_PROFILE", "empty_account")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LIVE_KILL_SWITCH_FILE", str(tmp_path / "live_kill_switch.json"))
    monkeypatch.setenv("SESSION_BASELINE_FILE", str(tmp_path / "session_drawdown.json"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    monkeypatch.setenv("BACKEND_TOKEN_FILE", str(tmp_path / "backend_api_token"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _held_report(*, with_hold: bool) -> OrderRestartReconciliationReport:
    hold = FillFloorHold(
        basis=FillFloorBasis.WITNESSED_LOWER_BOUND, floor=Decimal("7.00000000"), venue=Decimal("5")
    )
    return OrderRestartReconciliationReport(
        proven=(),
        unresolved=(
            RestartOrderObservation(
                intent_id="intent-held",
                local_status=OrderLifecycle.CANCEL_PENDING,
                broker_status=None,
                transition_applied=False,
                reason="venue fill figure 5 is below the witnessed lower bound 7.00000000",
                fill_floor_hold=hold if with_hold else None,
            ),
        ),
        remaining_active=(
            RemainingActiveIntent(intent_id="intent-held", status=OrderLifecycle.CANCEL_PENDING),
        ),
        applied_updates=(),
    )


def _post(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch, report: OrderRestartReconciliationReport
) -> dict[str, object]:
    def fixed(self: OrderManagementService, *, now: object = None) -> object:
        del self, now
        return report

    monkeypatch.setattr(OrderManagementService, "reconcile_on_restart_report", fixed)
    with TestClient(create_app()) as client:
        token = (demo_env / "backend_api_token").read_text(encoding="utf-8").strip()
        response = client.post("/orders/reconcile", headers={"X-Chronos-Token": token})
    assert response.status_code == 200
    return response.json()


def test_the_reply_names_the_hold_with_the_intent_and_both_figures(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _post(demo_env, monkeypatch, _held_report(with_hold=True))

    assert body["status"] == "PENDING"
    assert body["unresolved_count"] == 1
    assert body["fill_floor_holds"] == [
        {
            "intent_id": "intent-held",
            "basis": "WITNESSED_LOWER_BOUND",
            "floor_figure": "7",
            "venue_figure": "5",
        }
    ]
    log_text = (demo_env / "chronos.log").read_text(encoding="utf-8")
    entries = [json.loads(line) for line in log_text.splitlines() if line.startswith("{")]
    holds = [e for e in entries if e.get("event") == "fill_floor_hold"]
    line = (
        "Fill floor hold on intent intent-held: venue figure 5 is below 7 "
        "(WITNESSED_LOWER_BOUND); no operator exit"
    )
    # the startup pass (same patched evidence) and the operator pass each name it once
    assert [(e["operation"], e["message"], e["level"]) for e in holds] == [
        ("startup_reconcile", line, "WARNING"),
        ("operator_reconcile", line, "WARNING"),
    ]


def test_a_held_intent_of_another_kind_replies_with_no_hold(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _post(demo_env, monkeypatch, _held_report(with_hold=False))

    assert body["status"] == "PENDING" and body["unresolved_count"] == 1
    assert body["fill_floor_holds"] == []


def test_a_clean_pass_replies_with_no_hold(demo_env: Path) -> None:
    with TestClient(create_app()) as client:
        token = (demo_env / "backend_api_token").read_text(encoding="utf-8").strip()
        body = client.post("/orders/reconcile", headers={"X-Chronos-Token": token}).json()
    assert body["status"] == "RECONCILED" and body["fill_floor_holds"] == []
