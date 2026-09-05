"""R-72 / ADR-0054: a boot that follows a restore comes up read-only and unreconciled.

ADR-0049 (R-66) closed one half of `docs/VISION_COMPLETION_PLAN.md` §6 finding 3: a
state file missing *after this installation wrote one* reads closed. Its own
Consequences section named what it left open — booting **read-only** and
**unreconciled**, and the mandate's auto-activation on that boot — and disclosed the
residual that a restore bringing back nothing, marker included, still presents as a
fresh install.

The marker alone cannot close either, because it is one store: absence of the marker
and absence of everything are the same bytes. These tests exercise the second witness
— the marker's installation id recorded in the Chronos database — and the three
consequences a disagreement between the two stores must have.

The matrix tests are pure: they call the evaluator on already-resolved inputs, so a
row cannot pass by accident of file-system ordering. The consequence tests boot the
real app twice through its real lifespan, deleting state between boots.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chronos.api.main import create_app
from chronos.autonomy import (
    AutonomyMandate,
    AutonomyMode,
    CapitalLimits,
    ConcentrationLimits,
    FamilyPromotion,
    InstrumentScope,
    MarketDataRequirements,
    OrderForm,
    PromotionLevel,
    StrategyForm,
    TradableAssetClass,
    VersionPins,
)
from chronos.broker.demo import DEMO_ACCOUNT_ID
from chronos.config.settings import get_settings
from chronos.domain.enums import DataQuality
from chronos.operations.health import BackgroundTaskName, TaskState
from chronos.orders.recovery_hold import (
    RECOVERY_PENDING_NAME,
    RecoveryHold,
    RecoveryHoldReason,
    evaluate_recovery_hold,
    read_restore_pending_token,
)
from chronos.orders.state_generation import StateGenerationMarker
from chronos.utils.identifiers import account_fingerprint

REPO_ROOT = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 9, 4, 14, 30, tzinfo=UTC)


# --- The detection matrix (pure) --------------------------------------------
#
# One row per line of ADR-0054's table. Each asserts the reason as well as the
# hold, because "some hold fired" would let two rows collapse into one guard.


def test_a_lost_state_directory_holds() -> None:
    """Marker gone, database row present: the state directory did not come back.

    This is exactly ADR-0049's disclosed residual — a restore that omits the
    marker too — closed whenever the database survived to witness it.
    """

    hold = evaluate_recovery_hold(
        marker_installation_id=None,
        marker_unreadable=False,
        recorded_installation_id="aaaa",
        restore_pending_token=None,
    )
    assert hold is not None
    assert hold.reason is RecoveryHoldReason.STATE_DIRECTORY_LOST


def test_a_replaced_database_holds() -> None:
    """Marker present, no row: the database was replaced beneath surviving files."""

    hold = evaluate_recovery_hold(
        marker_installation_id="aaaa",
        marker_unreadable=False,
        recorded_installation_id=None,
        restore_pending_token=None,
    )
    assert hold is not None
    assert hold.reason is RecoveryHoldReason.DATABASE_REPLACED


def test_two_installations_mixed_together_hold() -> None:
    """Both stores present and disagreeing: the two came from different snapshots."""

    hold = evaluate_recovery_hold(
        marker_installation_id="aaaa",
        marker_unreadable=False,
        recorded_installation_id="bbbb",
        restore_pending_token=None,
    )
    assert hold is not None
    assert hold.reason is RecoveryHoldReason.INSTALLATION_MISMATCH


def test_an_unreadable_marker_holds() -> None:
    """Parity with `was_materialized`: a marker we cannot read proves nothing."""

    hold = evaluate_recovery_hold(
        marker_installation_id=None,
        marker_unreadable=True,
        recorded_installation_id="aaaa",
        restore_pending_token=None,
    )
    assert hold is not None
    assert hold.reason is RecoveryHoldReason.MARKER_UNREADABLE


def test_a_restore_tool_witness_holds_a_consistent_installation() -> None:
    """The wholesale-restore case: both stores agree, and only the tool knows.

    A self-consistent restore of the whole state directory carries both stores
    from one snapshot, so no disagreement exists to find. `chronos.recovery
    restore` leaves this witness behind precisely because nothing inside the
    directory can tell.
    """

    hold = evaluate_recovery_hold(
        marker_installation_id="aaaa",
        marker_unreadable=False,
        recorded_installation_id="aaaa",
        restore_pending_token="token-1",
    )
    assert hold is not None
    assert hold.reason is RecoveryHoldReason.RESTORE_PENDING


def test_a_consistent_installation_does_not_hold() -> None:
    """Guard the guard: an ordinary restart must not boot held."""

    assert (
        evaluate_recovery_hold(
            marker_installation_id="aaaa",
            marker_unreadable=False,
            recorded_installation_id="aaaa",
            restore_pending_token=None,
        )
        is None
    )


def test_a_fresh_install_does_not_hold() -> None:
    """Guard the guard: neither store exists yet, and nothing has been lost."""

    assert (
        evaluate_recovery_hold(
            marker_installation_id=None,
            marker_unreadable=False,
            recorded_installation_id=None,
            restore_pending_token=None,
        )
        is None
    )


def test_the_witness_token_is_part_of_the_binding() -> None:
    """A second restore must not be covered by the first restore's acknowledgement."""

    def _hold(token: str) -> RecoveryHold:
        hold = evaluate_recovery_hold(
            marker_installation_id="aaaa",
            marker_unreadable=False,
            recorded_installation_id="aaaa",
            restore_pending_token=token,
        )
        assert hold is not None
        return hold

    assert _hold("token-1").binding != _hold("token-2").binding


def test_a_present_but_unreadable_restore_witness_still_holds(tmp_path: Path) -> None:
    """Presence is the signal; an unparseable witness must not read as absence."""

    path = tmp_path / RECOVERY_PENDING_NAME
    path.write_text("{ not json", encoding="utf-8")
    token = read_restore_pending_token(path)
    assert token is not None


# --- The consequences, over the real app and its real lifespan ---------------


@pytest.fixture()
def demo_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DEMO_PROFILE", "empty_account")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    monkeypatch.setenv("BACKEND_TOKEN_FILE", str(tmp_path / "backend_api_token"))
    monkeypatch.setenv("LIVE_KILL_SWITCH_FILE", str(tmp_path / "live_kill_switch.json"))
    monkeypatch.setenv("SESSION_BASELINE_FILE", str(tmp_path / "session_drawdown.json"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _headers(root: Path) -> dict[str, str]:
    return {"X-Chronos-Token": (root / "backend_api_token").read_text(encoding="utf-8").strip()}


def _boot(root: Path) -> TestClient:
    del root
    return TestClient(create_app())


def _lose_the_state_directory(root: Path) -> None:
    """The restore that omitted the sidecar: marker gone, database intact."""

    (root / "state_generation.json").unlink()


def test_a_first_boot_seeds_both_stores_and_does_not_hold(demo_env: Path) -> None:
    """The writer witnesses its own installation in both stores, at the first boot.

    Seeding matters for what comes after: without a marker written here, a later
    loss of the state directory would have nothing to be missing.
    """

    with _boot(demo_env) as client:
        state = client.app.state.backend  # type: ignore[attr-defined]
        assert state.recovery_hold is None
        assert state.may_write is True
    marker = StateGenerationMarker(demo_env / "state_generation.json").read()
    assert marker is not None
    assert marker.installation_id
    # Seeding must not claim a materialisation: R-66's readings are unchanged.
    assert marker.materialized == ()


def test_a_boot_after_a_lost_state_directory_refuses_writer_routes(demo_env: Path) -> None:
    """Read-only at the route layer: every mutating endpoint refuses with 409."""

    with _boot(demo_env):
        pass
    _lose_the_state_directory(demo_env)
    with _boot(demo_env) as client:
        headers = _headers(demo_env)
        state = client.app.state.backend  # type: ignore[attr-defined]
        assert state.recovery_hold is not None
        assert state.recovery_hold.reason is RecoveryHoldReason.STATE_DIRECTORY_LOST
        armed = client.post("/live/arm", json={"phrase": "irrelevant"}, headers=headers)
        assert armed.status_code == 409


def test_a_held_backend_holds_its_lease_rather_than_dropping_it(demo_env: Path) -> None:
    """The hold is orthogonal to the lease, deliberately.

    Releasing the lease would hand write authority to the next process to start,
    which reads the same unverified state and would decide the same way — so the
    hold refuses writes while this process stays the single writer.
    """

    with _boot(demo_env):
        pass
    _lose_the_state_directory(demo_env)
    with _boot(demo_env) as client:
        state = client.app.state.backend  # type: ignore[attr-defined]
        assert state.writer is True  # still the lease holder
        assert state.may_write is False  # and still refusing to write


def test_a_held_backend_denies_at_the_submission_boundary(demo_env: Path) -> None:
    """Not only over HTTP: the autonomy handoff never passes through a route."""

    with _boot(demo_env):
        pass
    _lose_the_state_directory(demo_env)
    with _boot(demo_env) as client:
        state = client.app.state.backend  # type: ignore[attr-defined]
        boundary = state.runtime.order_management.submission_boundary
        assert boundary._lease_verifier() is False  # type: ignore[attr-defined]


def test_a_held_backend_stays_unreconciled(demo_env: Path) -> None:
    """Unreconciled means the latch is never published, and says why."""

    with _boot(demo_env):
        pass
    _lose_the_state_directory(demo_env)
    with _boot(demo_env) as client:
        state = client.app.state.backend  # type: ignore[attr-defined]
        snapshot = state.runtime.reconciliation_readiness.snapshot()
        assert snapshot.status.name == "PENDING"
        assert state.startup_faults  # the fault is visible to /healthz


def _mandate(now: datetime) -> AutonomyMandate:
    """An owner grant for the demo account, valid around ``now``.

    SHADOW, deliberately. What is under test is whether ADR-0017's auto-activation
    runs at all, and ADR-0051 caps a *submitting* mandate to the authenticated
    proposer posture — a PAPER grant here would raise
    ``UnauthenticatedSubmittingMandate`` and give a `None` runtime for a reason
    that has nothing to do with a recovery hold. That is the same way the first
    version of the test below was hollow, so the grant is one this backend really
    does assemble.
    """

    return AutonomyMandate(
        mandate_id="m-recovery-hold-test",
        mandate_version=1,
        account_fingerprint=account_fingerprint(DEMO_ACCOUNT_ID),
        mode=AutonomyMode.SHADOW,
        promotions=(
            FamilyPromotion(asset_class=TradableAssetClass.EQUITY, level=PromotionLevel.SHADOW),
        ),
        effective_from=now - timedelta(hours=1),
        expires_at=now + timedelta(days=1),
        versions=VersionPins(
            provider="external-worker",
            model_id="ingress",
            model_version="1",
            prompt_version="1",
            tool_schema_version="1",
            decision_schema_version="1",
            policy_version="1",
        ),
        scope=InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY",),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT,),
        ),
        capital=CapitalLimits(
            allocated_capital_usd=Decimal(50_000),
            max_order_notional_usd=Decimal(10_000),
            max_gross_exposure_usd=Decimal(500_000),
            max_net_exposure_usd=Decimal(500_000),
            max_position_notional_usd=Decimal(100_000),
            max_shares_per_order=100,
            min_cash_floor_usd=Decimal(1_000),
            min_buying_power_usd=Decimal(500),
        ),
        concentration=ConcentrationLimits(max_symbol_exposure_pct=Decimal("0.50")),
        market_data=MarketDataRequirements(
            max_quote_age_seconds=Decimal(5),
            permitted_data_qualities=(DataQuality.LIVE,),
        ),
        owner_authorization_ref="owner-recovery-hold-test",
        authored_at=now,
    )


async def _no_ticks(autonomy: object, **_kwargs: object) -> None:
    """Stand in for the tick task: this exercises activation, not the schedule."""

    return None


@pytest.fixture()
def mandated_env(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """`demo_env` plus a valid owner grant, so autonomy would activate on boot."""

    mandate_file = demo_env / "mandate.json"
    mandate_file.write_text(_mandate(datetime.now(UTC)).model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("AUTONOMY_MANDATE_FILE", str(mandate_file))
    monkeypatch.setenv("AUTONOMY_ALERT_FILE", str(demo_env / "owner_alerts.jsonl"))
    monkeypatch.setattr("chronos.api.main.autonomy_tick_task", _no_ticks)
    get_settings.cache_clear()
    yield demo_env
    get_settings.cache_clear()


def test_a_mandate_auto_activates_when_there_is_no_hold(mandated_env: Path) -> None:
    """The positive control, and it is the whole reason the next test means anything.

    Without a configured grant `build_autonomy_runtime` returns None on every
    boot, so a "no autonomy under a hold" assertion would pass against code that
    never had the hold in it. This proves the grant really does auto-activate
    here (ADR-0017), which is what the hold then has to prevent.
    """

    with _boot(mandated_env) as client:
        assert client.app.state.backend.recovery_hold is None  # type: ignore[attr-defined]
        assert getattr(client.app.state, "autonomy", None) is not None  # type: ignore[attr-defined]


def test_a_held_backend_builds_no_autonomy_runtime(mandated_env: Path) -> None:
    """The mandate must not auto-activate on a boot that follows a restore.

    `build_autonomy_runtime` is where ADR-0017's activation happens, so not
    calling it is the whole mechanism. Refusing to activate is deliberately not
    revoking: no revocation row is written, so the grant still activates on the
    boot after the hold is acknowledged.
    """

    with _boot(mandated_env):
        pass
    _lose_the_state_directory(mandated_env)
    with _boot(mandated_env) as client:
        assert client.app.state.backend.recovery_hold is not None  # type: ignore[attr-defined]
        assert getattr(client.app.state, "autonomy", None) is None  # type: ignore[attr-defined]


def test_a_held_backend_starts_no_reconciliation_refresher(demo_env: Path) -> None:
    """Unreconciled has to outlast startup, or the refresher undoes it.

    Leaving readiness PENDING at boot means nothing if the ADR-0020 task is
    running: its whole job is to re-arm the latch on a cadence, so it would
    publish exactly the readiness the hold exists to withhold.
    """

    with _boot(demo_env) as client:
        states = {
            observation.name: observation.state
            for observation in client.app.state.backend.task_observations.snapshot()  # type: ignore[attr-defined]
        }
        assert states[BackgroundTaskName.RECONCILIATION] is not TaskState.NOT_EXPECTED

    _lose_the_state_directory(demo_env)
    with _boot(demo_env) as client:
        states = {
            observation.name: observation.state
            for observation in client.app.state.backend.task_observations.snapshot()  # type: ignore[attr-defined]
        }
        assert states[BackgroundTaskName.RECONCILIATION] is TaskState.NOT_EXPECTED
        # The lease heartbeat still runs: the hold does not drop the lease.
        assert states[BackgroundTaskName.LEASE_HEARTBEAT] is not TaskState.NOT_EXPECTED


def test_a_replaced_database_beside_a_surviving_marker_holds(demo_env: Path) -> None:
    """The other direction, end to end: new database, old state directory.

    A database with no identity row at all has never run this code — `create_all`
    leaves the table empty and only migration 0012 leaves the adoption sentinel —
    so a marker beside it names an installation it never witnessed. Adopting here
    instead of holding would make the whole comparison unfalsifiable.
    """

    with _boot(demo_env):
        pass
    assert (demo_env / "state_generation.json").exists()
    (demo_env / "chronos.db").unlink()  # the database is replaced; the files are not
    with _boot(demo_env) as client:
        hold = client.app.state.backend.recovery_hold  # type: ignore[attr-defined]
        assert hold is not None
        assert hold.reason is RecoveryHoldReason.DATABASE_REPLACED


# --- Clearing the hold: a typed act with a note ------------------------------


def _acknowledge(client: TestClient, root: Path, note: str) -> int:
    return client.post(
        "/live/recovery/acknowledge",
        json={"note": note},
        headers=_headers(root),
    ).status_code


def test_the_acknowledgement_refuses_an_empty_note(demo_env: Path) -> None:
    """The kill switch's rule, applied here: no note, no act."""

    with _boot(demo_env):
        pass
    _lose_the_state_directory(demo_env)
    with _boot(demo_env) as client:
        assert _acknowledge(client, demo_env, "   ") == 422


def test_the_acknowledgement_is_reachable_while_the_hold_is_in_force(
    demo_env: Path,
) -> None:
    """Guard the guard: an unreachable acknowledgement would be no escape at all."""

    with _boot(demo_env):
        pass
    _lose_the_state_directory(demo_env)
    with _boot(demo_env) as client:
        assert _acknowledge(client, demo_env, "restore drill 2026-09-04") == 200


def test_an_acknowledged_hold_is_cleared_on_the_next_start(demo_env: Path) -> None:
    """Durable, and effective at the next boot rather than mid-process."""

    with _boot(demo_env):
        pass
    _lose_the_state_directory(demo_env)
    with _boot(demo_env) as client:
        assert _acknowledge(client, demo_env, "restore drill 2026-09-04") == 200
        # Still held for the rest of this process's life.
        assert client.app.state.backend.recovery_hold is not None  # type: ignore[attr-defined]
    with _boot(demo_env) as client:
        assert client.app.state.backend.recovery_hold is None  # type: ignore[attr-defined]
        assert client.app.state.backend.may_write is True  # type: ignore[attr-defined]


def test_an_acknowledgement_does_not_cover_a_different_mismatch(demo_env: Path) -> None:
    """An acknowledgement binds the pair it saw, not "restores" in general."""

    with _boot(demo_env):
        pass
    _lose_the_state_directory(demo_env)
    with _boot(demo_env) as client:
        assert _acknowledge(client, demo_env, "restore drill one") == 200
    # A different restore: the marker comes back, from another installation.
    StateGenerationMarker(demo_env / "state_generation.json").ensure_installation(
        "a-different-installation", now=_NOW
    )
    with _boot(demo_env) as client:
        hold = client.app.state.backend.recovery_hold  # type: ignore[attr-defined]
        assert hold is not None
        assert hold.reason is RecoveryHoldReason.INSTALLATION_MISMATCH


def test_the_acknowledgement_does_not_disengage_the_kill_switch(demo_env: Path) -> None:
    """Three independent typed acts; clearing one must not clear another.

    The two mechanisms run together here on purpose. The marker survives, so R-66
    reads the vanished kill file as ENGAGED; the restore witness raises the hold.
    Acknowledging the restore is the operator saying "I know where this state came
    from" -- it is not the operator saying the emergency stop may go, and that
    still takes its own note at ``POST /live/kill/disengage``.
    """

    with _boot(demo_env) as client:
        client.post(
            "/live/kill",
            json={"reason": "operator stop"},
            headers=_headers(demo_env),
        )
    # Only the kill file, so the marker still proves this installation wrote one.
    (demo_env / "live_kill_switch.json").unlink()
    (demo_env / RECOVERY_PENDING_NAME).write_text(
        json.dumps({"token": "drill-2026-09-04"}), encoding="utf-8"
    )
    with _boot(demo_env) as client:
        assert client.app.state.backend.recovery_hold is not None  # type: ignore[attr-defined]
        assert _acknowledge(client, demo_env, "restore drill 2026-09-04") == 200
    with _boot(demo_env) as client:
        assert client.app.state.backend.recovery_hold is None  # type: ignore[attr-defined]
        body = client.get("/live/status", headers=_headers(demo_env)).json()
        assert body["kill_switch"]["engaged"] is True


def test_losing_the_marker_too_is_caught_by_the_database_witness(demo_env: Path) -> None:
    """ADR-0049's disclosed residual, closed while the database survives.

    Deleting the marker with the kill file returns R-66 to the permissive reading
    -- the switch genuinely reads DISENGAGED, because nothing in the directory
    remembers it was ever written. The database does, and that is the whole point
    of a second store.
    """

    with _boot(demo_env) as client:
        client.post(
            "/live/kill",
            json={"reason": "operator stop"},
            headers=_headers(demo_env),
        )
    (demo_env / "live_kill_switch.json").unlink()
    _lose_the_state_directory(demo_env)
    with _boot(demo_env) as client:
        body = client.get("/live/status", headers=_headers(demo_env)).json()
        assert body["kill_switch"]["engaged"] is False  # the residual, unchanged
        hold = client.app.state.backend.recovery_hold  # type: ignore[attr-defined]
        assert hold is not None
        assert hold.reason is RecoveryHoldReason.STATE_DIRECTORY_LOST
        assert client.app.state.backend.may_write is False  # type: ignore[attr-defined]


def test_a_restore_witness_file_holds_over_the_real_lifespan(demo_env: Path) -> None:
    """The tool-side witness, end to end: consistent stores, restore still detected."""

    with _boot(demo_env):
        pass
    (demo_env / RECOVERY_PENDING_NAME).write_text(
        json.dumps({"token": "drill-2026-09-04"}), encoding="utf-8"
    )
    with _boot(demo_env) as client:
        hold = client.app.state.backend.recovery_hold  # type: ignore[attr-defined]
        assert hold is not None
        assert hold.reason is RecoveryHoldReason.RESTORE_PENDING
