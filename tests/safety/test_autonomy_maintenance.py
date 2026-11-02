"""MAINT-BUILD: the real lifespan must not activate a matching grant during cutover.

Reuse OPS-3's synthetic startup harness (real DemoBroker, SQLite, grant loader and
activation), observing one real tick instead of running an unbounded scheduler.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select

from chronos.api import main, maintenance
from chronos.persistence.schema import AutonomyMandateActivationRow, HashChainRow
from tests.safety.test_proposal_route_autonomy_admission import _World

SWITCH = "CHRONOS_AUTONOMY_MAINTENANCE"


@pytest.fixture
def world(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[_World, threading.Event]:
    world = _World(tmp_path, monkeypatch)
    world.write_mandate()
    world.configure(mandate_file=world.mandate)
    ticked = threading.Event()

    async def observe_tick(autonomy: Any, **_kwargs: object) -> None:
        await asyncio.to_thread(autonomy.run_tick, datetime.now(UTC))
        ticked.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "autonomy_tick_task", observe_tick)
    return world, ticked


def rows(app: Any) -> tuple[int, int]:
    with app.state.backend.runtime.database.sessions() as session:
        activations = session.scalar(select(func.count()).select_from(AutonomyMandateActivationRow))
        records = session.scalar(
            select(func.count())
            .select_from(HashChainRow)
            .where(HashChainRow.stream.like("autonomy.%"))
        )
        return int(activations or 0), int(records or 0)


def assert_inhibited(client: Any, app: Any, ticked: threading.Event) -> None:
    client.portal.call(asyncio.sleep, 0.03)
    assert getattr(app.state, "autonomy", None) is None
    assert not ticked.is_set()
    assert rows(app) == (0, 0)
    health = client.get("/health").json()
    assert "autonomy_maintenance" in health["observations"]["startup_faults"]
    tasks = {t["name"]: t["state"] for t in health["observations"]["tasks"]}
    assert tasks["autonomy"] == "NOT_EXPECTED"
    assert tasks["reconciliation"] != "NOT_EXPECTED"
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 503
    assert (
        client.get(
            "/terminal/commands", headers={"X-Chronos-Token": app.state.api_token}
        ).status_code
        == 200
    )


def test_explicit_off_retains_real_activation_and_first_tick(
    world: tuple[_World, threading.Event], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, ticked = world
    monkeypatch.setenv(SWITCH, "off")
    with rig.boot() as (client, app):
        assert ticked.wait(5), "the real first tick did not run"
        assert getattr(app.state, "autonomy", None) is not None
        assert rows(app)[0] == 1
        assert rows(app)[1] >= 1
        assert (
            "autonomy_maintenance"
            not in client.get("/health").json()["observations"]["startup_faults"]
        )


@pytest.mark.parametrize("value", ["on", None, "", "not-a-mode"])
def test_maintenance_or_unknown_state_never_builds_or_activates(
    world: tuple[_World, threading.Event],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    value: str | None,
) -> None:
    rig, ticked = world
    if value is None:
        monkeypatch.delenv(SWITCH, raising=False)
    else:
        monkeypatch.setenv(SWITCH, value)
    # First-start logging disables propagation at the chronos logger. Capture
    # directly so this oracle also works when this case runs alone.
    logger = logging.getLogger("chronos.api")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.CRITICAL), rig.boot() as (client, app):
            assert_inhibited(client, app, ticked)
            assert any(getattr(r, "event", None) == "autonomy_maintenance" for r in caplog.records)
    finally:
        logger.removeHandler(caplog.handler)


def test_explicit_lift_requires_a_new_lifespan(
    world: tuple[_World, threading.Event], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, ticked = world
    monkeypatch.setenv(SWITCH, "on")
    with rig.boot() as (client, app):
        assert_inhibited(client, app, ticked)
        monkeypatch.setenv(SWITCH, "off")
        assert_inhibited(client, app, ticked)
    with rig.boot() as (client, app):
        assert ticked.wait(5)
        assert getattr(app.state, "autonomy", None) is not None
        assert rows(app)[0] == 1


def test_startup_cannot_auto_lift_a_latched_inhibit(
    world: tuple[_World, threading.Event], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, ticked = world
    monkeypatch.setenv(SWITCH, "on")
    original = main.build_runtime

    def build_and_change_environment(**kwargs: Any) -> Any:
        runtime = original(**kwargs)
        monkeypatch.setenv(SWITCH, "off")
        return runtime

    monkeypatch.setattr(main, "build_runtime", build_and_change_environment)
    with rig.boot() as (client, app):
        assert_inhibited(client, app, ticked)


def test_unreadable_switch_state_inhibits_instead_of_aborting_backend(
    world: tuple[_World, threading.Event], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, ticked = world

    class UnreadableEnvironment:
        def get(self, *_args: object) -> str:
            raise OSError("synthetic environment read failure")

    monkeypatch.setattr(maintenance, "environ", UnreadableEnvironment())
    with rig.boot() as (client, app):
        assert_inhibited(client, app, ticked)
