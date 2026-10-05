"""OPS-3: ``POST /autonomy/proposals`` must not answer 202 for a proposal nothing will ever drain.

Observed on main 473c254 (demo, synthetic): when a mandate file is configured but the autonomy
runtime did not start — an invalid cadence, a missing, invalid, mismatched, revoked or unsafe
mandate, a wiring failure — the backend still boots (it must, so it can close positions), its
readiness may even read 200, and the proposal route queues the row and says 202 while no drain
exists. The design (OPS-3r3) closes that with an admission state owned by ``BackendState``:
non-accepting until the lifespan settles it exactly once, accepting only ``RUNNING`` and the
explicit ``NO_MANDATE_CONFIGURED`` posture, compared by identity so a raw string, a ``str``
subclass or a lookalike enum can never stand in for it.

Groups: RED regressions (503 and zero rows where main said 202), default-closed and type pins,
the setter's contract, and GREEN controls that must hold before and after (the documented
no-mandate 202, a running runtime, the writer gate going first, the two clients' reading of a
503). Everything is synthetic and in-process: ``BROKER_MODE=demo``, transmit and live off, a
SHADOW mandate, the drain scheduler held (the lifespan, the grant loaders and the route are real).
"""

from __future__ import annotations

import enum
import json
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from chronos.api.autonomy_wiring import UnauthenticatedSubmittingMandate
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
from chronos.bridge.app import ForwardResult
from chronos.broker.demo import DEMO_ACCOUNT_ID
from chronos.config.settings import Settings
from chronos.domain.enums import BrokerMode, DemoProfile
from chronos.persistence.schema import AutonomyProposalQueueRow
from chronos.supervisor import durable
from chronos.utils.identifiers import account_fingerprint
from tests.unit.test_tradingview_bridge import NOW as _BRIDGE_NOW
from tests.unit.test_tradingview_bridge import _alert, _client, _Recorder

_FINGERPRINT = account_fingerprint(DEMO_ACCOUNT_ID)
_MODEL = "ops3-admission"
_TOKEN_HEADER = "X-Chronos-Token"
_PROPOSAL = {
    "kind": "HOLD",
    "asset_class": "EQUITY",
    "symbol": "SPY",
    "direction": "NEUTRAL",
    "thesis": "ops-3 admission synthetic proposal",
}
# The one refusal body. Fixed text: no fault code, path or exception text can reach a caller.
_REFUSAL = "AUTONOMY_NOT_RUNNING"
_DETAIL = (
    "autonomy is configured on this backend but did not start; proposals are refused until "
    "the owner fixes the configuration and restarts. See GET /health and the server log."
)


async def _no_ticks(autonomy: object, **_kwargs: object) -> None:
    """The real lifespan, with only the background drain scheduler held."""


def _no_connection(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("the admission tests must not open a network connection")


class _World:
    """One temp directory, the settings, and the means to boot the real app against them."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.mandate = tmp_path / "mandate.json"
        self.settings: Settings | None = None

    def write_mandate(self, *, fingerprint: str = _FINGERPRINT, mode: int = 0o600) -> None:
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
            account_fingerprint=fingerprint,
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
            owner_authorization_ref="synthetic-ops3",
            authored_at=now,
        )
        self.mandate.write_text(mandate.model_dump_json(), encoding="utf-8")
        self.mandate.chmod(mode)

    def configure(self, *, mandate_file: Path | None, **overrides: Any) -> None:
        self.monkeypatch.setattr(socket.socket, "connect", _no_connection)
        self.monkeypatch.setattr(socket.socket, "connect_ex", _no_connection)
        self.monkeypatch.setattr("chronos.api.main.autonomy_tick_task", _no_ticks)
        settings = Settings.model_construct(
            broker_mode=BrokerMode.DEMO,
            demo_profile=DemoProfile.EMPTY_ACCOUNT,
            allow_order_transmit=False,
            allow_live_trading=False,
            database_url=f"sqlite:///{self.tmp / 'chronos.db'}",
            log_file=self.tmp / "chronos.log",
            backend_token_file=self.tmp / "backend_api_token",
            live_kill_switch_file=self.tmp / "kill.json",
            session_baseline_file=self.tmp / "baseline.json",
            autonomy_mandate_file=mandate_file,
            autonomy_alert_file=self.tmp / "owner_alerts.jsonl",
            clock_health_provider="disabled",
            **overrides,
        )
        assert not settings.allow_order_transmit and not settings.allow_live_trading
        self.settings = settings
        self.monkeypatch.setattr("chronos.runtime.get_settings", lambda: settings)

    @contextmanager
    def boot(self) -> Iterator[tuple[TestClient, FastAPI]]:
        app = create_app()
        with TestClient(app) as client:
            yield client, app


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _World:
    return _World(tmp_path, monkeypatch)


def _post(client: TestClient, app: FastAPI) -> httpx.Response:
    return client.post(
        "/autonomy/proposals", json=_PROPOSAL, headers={_TOKEN_HEADER: app.state.api_token}
    )


def _rows(app: FastAPI) -> int:
    with app.state.backend.runtime.database.sessions.begin() as session:
        return int(session.scalar(select(func.count()).select_from(AutonomyProposalQueueRow)) or 0)


def _assert_refused(response: httpx.Response, app: FastAPI) -> None:
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["accepted"] is False
    assert body["stage"] == "INGRESS"
    assert body["refusal"] == _REFUSAL
    assert body["detail"] == _DETAIL
    assert _rows(app) == 0


def _admission() -> Any:
    """The new type, imported lazily so a missing symbol fails the test, not collection."""

    from chronos.api import dependencies

    return dependencies


# --------------------------------------------------------------------------- RED regressions


def _boot_configured(world: _World, case: str) -> Callable[[], Any]:
    """Arrange one 'mandate configured, autonomy did not start' case; returns the booter."""

    if case == "cadence":
        world.write_mandate()
        world.configure(
            mandate_file=world.mandate,
            autonomy_idle_interval_seconds=2.0,
            autonomy_min_interval_seconds=5.0,
        )
    elif case == "missing":
        world.configure(mandate_file=world.tmp / "does-not-exist.json")
    elif case == "invalid":
        world.mandate.write_text("{not json", encoding="utf-8")
        world.mandate.chmod(0o600)
        world.configure(mandate_file=world.mandate)
    elif case == "mismatch":
        world.write_mandate(fingerprint="0" * 64)
        world.configure(mandate_file=world.mandate)
    elif case == "unsafe":
        world.write_mandate(mode=0o666)
        world.configure(mandate_file=world.mandate)
    elif case == "revoked":
        world.write_mandate()
        world.configure(mandate_file=world.mandate)
        with world.boot() as (_, app):
            assert app.state.autonomy is not None
            with app.state.backend.runtime.database.sessions.begin() as session:
                assert durable.revoke(
                    session,
                    account_fingerprint=_FINGERPRINT,
                    mandate_id=_MODEL,
                    reason="ops-3 test: owner stood the system down",
                    now=datetime.now(UTC),
                )
    elif case in ("wiring", "unauthenticated"):
        world.write_mandate()
        world.configure(mandate_file=world.mandate)
        error: Exception = (
            RuntimeError("synthetic wiring failure")
            if case == "wiring"
            else UnauthenticatedSubmittingMandate("synthetic")
        )

        def _raises(*_a: object, **_k: object) -> None:
            raise error

        world.monkeypatch.setattr("chronos.api.main.build_autonomy_runtime", _raises)
    else:  # pragma: no cover - a typo in a parametrization
        raise AssertionError(case)
    return world.boot


@pytest.mark.parametrize(
    "case",
    ["cadence", "missing", "invalid", "mismatch", "unsafe", "revoked", "wiring", "unauthenticated"],
)
def test_r_a_configured_mandate_whose_autonomy_did_not_start_refuses_every_proposal(
    world: _World, case: str
) -> None:
    boot = _boot_configured(world, case)
    with boot() as (client, app):
        assert getattr(app.state, "autonomy", None) is None
        _assert_refused(_post(client, app), app)
        # The refusal is repeatable and still writes nothing.
        _assert_refused(_post(client, app), app)


# --------------------------------------------------------------------------- default-closed


def test_d1_an_unsettled_backend_state_refuses(world: _World) -> None:
    world.write_mandate()
    world.configure(mandate_file=world.mandate)
    with world.boot() as (client, app):
        app.state.backend.autonomy_admission = _admission().AutonomyAdmission.NOT_INITIALIZED
        _assert_refused(_post(client, app), app)


def test_d1_a_state_with_no_admission_attribute_refuses(world: _World) -> None:
    world.write_mandate()
    world.configure(mandate_file=world.mandate)
    with world.boot() as (client, app):
        real = app.state.backend

        class _NoAdmission:
            """The real backend state, minus the attribute the guard reads."""

            def __getattr__(self, name: str) -> Any:
                if name == "autonomy_admission":
                    raise AttributeError(name)
                return getattr(real, name)

        app.state.backend = _NoAdmission()
        _assert_refused(_post(client, app), app)


class _LookalikePlain(enum.Enum):
    RUNNING = 1
    NO_MANDATE_CONFIGURED = 2


class _LookalikeStr(enum.StrEnum):
    RUNNING = "running"
    NO_MANDATE_CONFIGURED = "no_mandate_configured"


class _StrSubclass(str):
    __slots__ = ()


_HOSTILE: list[tuple[str, Callable[[], object]]] = [
    ("raw_running", lambda: "running"),
    ("raw_no_mandate_configured", lambda: "no_mandate_configured"),
    ("raw_upper_running", lambda: "RUNNING"),
    ("lookalike_plain_enum_running", lambda: _LookalikePlain.RUNNING),
    ("lookalike_plain_enum_no_mandate", lambda: _LookalikePlain.NO_MANDATE_CONFIGURED),
    ("lookalike_strenum_running", lambda: _LookalikeStr.RUNNING),
    ("lookalike_strenum_no_mandate", lambda: _LookalikeStr.NO_MANDATE_CONFIGURED),
    ("str_subclass_running", lambda: _StrSubclass("running")),
    ("str_subclass_no_mandate", lambda: _StrSubclass("no_mandate_configured")),
    ("none", lambda: None),
    ("true", lambda: True),
    ("one", lambda: 1),
    ("unknown_string", lambda: "bogus"),
]


@pytest.mark.parametrize("make", [m for _, m in _HOSTILE], ids=[n for n, _ in _HOSTILE])
def test_d3_only_the_admission_enum_itself_is_accepted_never_a_value_equal_to_it(
    world: _World, make: Callable[[], object]
) -> None:
    """Daybreak P1-OPS-3r2-1: set membership on a ``StrEnum`` accepted raw strings."""

    world.write_mandate()
    world.configure(mandate_file=world.mandate)
    with world.boot() as (client, app):
        app.state.backend.autonomy_admission = make()
        _assert_refused(_post(client, app), app)


@pytest.mark.parametrize("make", [m for _, m in _HOSTILE], ids=[n for n, _ in _HOSTILE])
def test_s1_the_setter_rejects_anything_that_is_not_a_settled_admission_member(
    world: _World, make: Callable[[], object]
) -> None:
    deps = _admission()
    world.write_mandate()
    world.configure(mandate_file=world.mandate)
    with world.boot() as (_, app):
        state = app.state.backend
        state.autonomy_admission = deps.AutonomyAdmission.NOT_INITIALIZED
        with pytest.raises(RuntimeError):
            state.settle_autonomy_admission(make())
        assert state.autonomy_admission is deps.AutonomyAdmission.NOT_INITIALIZED


def test_s1_the_setter_settles_once_and_never_back_to_not_initialized(world: _World) -> None:
    deps = _admission()
    world.write_mandate()
    world.configure(mandate_file=world.mandate)
    with world.boot() as (_, app):
        state = app.state.backend
        state.autonomy_admission = deps.AutonomyAdmission.NOT_INITIALIZED
        with pytest.raises(RuntimeError):
            state.settle_autonomy_admission(deps.AutonomyAdmission.NOT_INITIALIZED)
        state.settle_autonomy_admission(deps.AutonomyAdmission.CONFIGURED_NOT_STARTED)
        assert state.autonomy_admission is deps.AutonomyAdmission.CONFIGURED_NOT_STARTED
        for again in deps.AutonomyAdmission:
            with pytest.raises(RuntimeError):
                state.settle_autonomy_admission(again)


def test_the_admission_type_is_not_a_string_and_only_two_members_accept() -> None:
    deps = _admission()
    admission = deps.AutonomyAdmission
    assert not issubclass(admission, str)
    assert {m.name for m in admission} == {
        "NOT_INITIALIZED",
        "CONFIGURED_NOT_STARTED",
        "NO_MANDATE_CONFIGURED",
        "RUNNING",
    }
    accepting = {m.name for m in admission if deps.admission_accepts(m)}
    assert accepting == {"RUNNING", "NO_MANDATE_CONFIGURED"}
    for member in admission:
        assert member != member.name and member != str(member.value)


# --------------------------------------------------------------------------- GREEN controls


def test_c1_no_mandate_configured_still_answers_202_and_queues_one_row(world: _World) -> None:
    world.configure(mandate_file=None)
    with world.boot() as (client, app):
        assert getattr(app.state, "autonomy", None) is None
        response = _post(client, app)
        assert response.status_code == 202, response.text
        assert response.json()["accepted"] is True
        assert _rows(app) == 1


def test_c1_the_no_mandate_posture_is_an_explicit_state_not_an_absence(world: _World) -> None:
    deps = _admission()
    world.configure(mandate_file=None)
    with world.boot() as (_, app):
        assert app.state.backend.autonomy_admission is deps.AutonomyAdmission.NO_MANDATE_CONFIGURED


def test_c2_a_valid_mandate_and_cadence_answers_202(world: _World) -> None:
    world.write_mandate()
    world.configure(mandate_file=world.mandate)
    with world.boot() as (client, app):
        assert app.state.autonomy is not None
        response = _post(client, app)
        assert response.status_code == 202, response.text
        assert response.json()["stage"] == "QUEUED"
        assert _rows(app) == 1


def test_c2_a_running_runtime_is_the_running_state(world: _World) -> None:
    deps = _admission()
    world.write_mandate()
    world.configure(mandate_file=world.mandate)
    with world.boot() as (_, app):
        assert app.state.backend.autonomy_admission is deps.AutonomyAdmission.RUNNING


def test_c3_the_writer_gate_still_refuses_first_on_a_read_only_backend(world: _World) -> None:
    world.write_mandate(fingerprint="0" * 64)  # configured and not started: the guard would refuse
    world.configure(mandate_file=world.mandate)
    with world.boot() as (client, app):
        app.state.backend.read_only = True
        app.state.backend.lease = None
        response = _post(client, app)
        assert response.status_code != 202
        assert _REFUSAL not in response.text, "the writer dependency must answer before the guard"
        assert _rows(app) == 0


def test_c5_the_bridge_reads_a_503_refusal_as_a_terminal_422_and_forwards_once() -> None:
    """The deliberate contract: any answered ingress refusal is 422 with the code echoed."""

    refusal = ForwardResult(forwarded=False, status_code=503, refusal=_REFUSAL, detail=_DETAIL)
    client, recorder = _client(forwarder=_Recorder(refusal), now=_BRIDGE_NOW)
    response = client.post("/tradingview/webhook", json=_alert())
    assert response.status_code == 422
    body = response.json()
    assert body["accepted"] is False and body["refusal"] == _REFUSAL
    assert len(recorder.sent) == 1


def test_c6_the_worker_reads_a_503_refusal_as_one_refused_call_and_keeps_its_cadence() -> None:
    from worker.cycle import CycleOutcome, _forward

    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            503,
            json={"accepted": False, "stage": "INGRESS", "refusal": _REFUSAL, "detail": _DETAIL},
        )

    config = SimpleNamespace(
        api_token="local-token", proposer_token="", backend_url="http://127.0.0.1:8765"
    )
    with httpx.Client(transport=httpx.MockTransport(handle)) as backend:
        outcome = _forward(config, backend, json.dumps(_PROPOSAL), kind="HOLD")  # type: ignore[arg-type]
    assert outcome is CycleOutcome.INGRESS_REFUSED
    assert len(calls) == 1
