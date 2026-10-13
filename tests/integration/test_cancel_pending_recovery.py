"""U-B: the dwell-gated recovery of a CANCEL_PENDING whose cancel did not take (design r3 §4.3).

The reconciler is the REAL ``OrderRestartReconciler`` over a real order book and the REAL
``ReconciliationReadiness``; every pass runs inside ``readiness.reconciliation_session`` exactly
as the runtime does (so the per-pass ``generation`` advances every pass, D10). The venue is the
scripted double, given a REAL ``CallbackBridge`` as its ``bridge`` so ``adapter_epoch`` and the
bridge's own invalidation paths (§1.7 a-c) are the production ones.

Times: the cancel mark is the previous Friday (older than ``M`` = 60 s); pass ``k`` runs at
``NOW + 120 s * k`` wall-clock with the injected monotonic clock at ``1000 + 120 * k`` (the
positioned open-market cadence), so pass 1 is the first one at least ``D`` = 90 s after pass 0.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.support.settle_rig import (
    CLIENT_ID,
    EARLIER_TODAY,
    NOW,
    PRIOR,
    Rig,
    ScriptedVenue,
    issues,
    make_rig,
    venue_order,
)

from chronos.broker.callbacks import CallbackBridge
from chronos.broker.request_registry import RequestRegistry
from chronos.domain.enums import OrderLifecycle
from chronos.orders.reconciliation_recovery import OrderRestartReconciler

CP = OrderLifecycle.CANCEL_PENDING
PF = OrderLifecycle.PARTIALLY_FILLED
SUBMITTED = OrderLifecycle.SUBMITTED
CANCELLED = OrderLifecycle.CANCELLED
FILLED = OrderLifecycle.FILLED
RECOVERY = "RECONCILE_RECOVERY"
_ROOT = Path(__file__).resolve().parents[2]


class Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class World:
    """One rig, a monotonic clock and a real bridge wired to the real readiness latch."""

    def __init__(self, *, bridge: bool = True, hooked: bool = True) -> None:
        self.mono = Mono()
        self.rig: Rig = make_rig(ScriptedVenue(), monotonic=self.mono)
        if bridge:
            self.rig.venue.bridge = (
                CallbackBridge(
                    RequestRegistry(),
                    on_connection_uncertain=self.rig.readiness.invalidate,
                    on_managed_account_scope_change=self.rig.readiness.invalidating_transition,
                    client_id=CLIENT_ID,
                )
                if hooked
                else CallbackBridge(RequestRegistry())
            )
        self.rig.seed()

    @property
    def venue(self) -> ScriptedVenue:
        venue: ScriptedVenue = self.rig.venue
        return venue

    def at(self, k: int, *, wall_extra: float = 0.0, mono_extra: float = 0.0):  # type: ignore[no-untyped-def]
        """Pass ``k``: inside a reconciliation session, as the runtime runs it."""

        self.mono.t = 1000.0 + 120.0 * k + mono_extra
        moment = NOW + timedelta(seconds=120 * k + wall_extra)
        with self.rig.readiness.reconciliation_session("reconciliation observation in progress"):
            return self.rig.reconcile(moment)

    def working(self, filled: str = "0", **fields: object) -> None:
        self.venue.open = [venue_order(SUBMITTED, filled, **fields)]  # type: ignore[arg-type]

    def recoveries(self) -> list[tuple[OrderLifecycle | None, OrderLifecycle, Decimal | None]]:
        return [
            (e.from_status, e.to_status, e.filled_quantity)
            for e in self.rig.events()
            if e.source == RECOVERY
        ]


@pytest.fixture
def world() -> Iterator[World]:
    w = World()
    try:
        yield w
    finally:
        w.rig.close()


def _lost_cancel(w: World, filled: str = "0") -> None:
    """D2 (the design's case a): the mark was written, the order kept working at the venue."""

    w.rig.pending()
    w.working(filled)


# --- the recovery row itself (C2-1, C2-prec) -------------------------------------------------


def test_a_lost_cancel_with_no_fills_recovers_to_submitted_after_the_dwell(world: World) -> None:
    _lost_cancel(world)
    world.at(0)
    assert world.rig.status() is CP  # the first observation writes nothing
    report = world.at(1)
    assert world.rig.status() is SUBMITTED, issues(report)
    assert world.recoveries() == [(CP, SUBMITTED, Decimal("0"))]
    event = world.rig.events()[-1]
    mark = next(e for e in reversed(world.rig.events()) if e.source == "CANCEL")
    assert event.event_key == f"intent-1:9001:RECOVER:SUBMITTED:{mark.event_key}:0"
    assert [r.status for r in report.remaining_active] == [SUBMITTED]  # still remaining-active


def test_D8_unchanged_fills_recover_to_partially_filled_with_the_venue_figure(world: World) -> None:
    world.rig.event(PF, at=PRIOR, filled="4")
    _lost_cancel(world, "4")
    world.at(0)
    world.at(1)
    assert world.rig.status() is PF
    assert world.recoveries() == [(CP, PF, Decimal("4"))]


def test_R_work_the_recovery_carries_the_venue_figure_never_the_tracker_maximum(
    world: World,
) -> None:
    """X=3 before S0, E_today=2, V.filled=5: recovers with 5, never 3 (§3.2 example 4)."""

    from tests.support.settle_rig import venue_execution

    world.rig.event(PF, at=PRIOR, filled="3")
    _lost_cancel(world, "5")
    world.venue.execs = [venue_execution("e1", "2", EARLIER_TODAY)]
    world.at(0)
    world.at(1)
    assert world.recoveries() == [(CP, PF, Decimal("5"))]


def test_the_recovery_is_written_through_the_repository_cas_not_the_tracker_ingest(
    world: World,
) -> None:
    """Its source and from/to prove the planner's CAS wrote it (ingest writes RECONCILE and could
    not write CANCEL_PENDING -> SUBMITTED, which the table forbids)."""

    _lost_cancel(world)
    world.at(0)
    world.at(1)
    event = world.rig.events()[-1]
    assert (event.source, event.from_status, event.to_status) == (RECOVERY, CP, SUBMITTED)
    assert event.evidence["recovered_from"] == "dwell"


# --- D1, D13: nothing inside D -----------------------------------------------------------------


def test_D1_nothing_is_recovered_inside_the_dwell(world: World) -> None:
    _lost_cancel(world)
    world.at(0)
    world.mono.t += 60
    with world.rig.readiness.reconciliation_session("pass"):
        world.rig.reconcile(NOW + timedelta(seconds=60))
    assert world.rig.status() is CP and world.recoveries() == []


def test_D13_a_partial_fill_inside_the_dwell_stays_cancel_pending(world: World) -> None:
    _lost_cancel(world, "0")
    world.at(0)
    world.working("3")
    world.mono.t += 60
    with world.rig.readiness.reconciliation_session("pass"):
        world.rig.reconcile(NOW + timedelta(seconds=60))
    assert world.rig.status() is CP and world.recoveries() == []
    assert world.rig.trail()[-1] == ("FILL_PROGRESS", CP)


# --- D9, D14, D18: fill progress is in place and not a reset; the recovery row wins after D -----


def test_D9_a_new_fill_before_the_dwell_is_recorded_in_place(world: World) -> None:
    _lost_cancel(world, "2")
    world.at(0)
    assert world.rig.status() is CP and world.rig.trail()[-1] == ("FILL_PROGRESS", CP)


def test_D14_D18_a_continuously_filling_order_still_recovers_and_the_recovery_row_wins(
    world: World,
) -> None:
    _lost_cancel(world, "2")
    world.at(0)  # FILL_PROGRESS 2 (C2-0), dwell starts
    world.working("4")
    world.at(1)  # dwell satisfied: C2-1 with the current figure, NOT another FILL_PROGRESS
    assert world.rig.status() is PF
    assert world.recoveries() == [(CP, PF, Decimal("4"))]
    assert [t for t in world.rig.trail() if t[0] == "FILL_PROGRESS"] == [("FILL_PROGRESS", CP)]


# --- D2, D4, D5: resets --------------------------------------------------------------------------


def test_D2_a_pending_cancel_at_the_venue_resets_the_dwell(world: World) -> None:
    _lost_cancel(world)
    world.at(0)
    world.venue.open = [venue_order(CP, "0")]  # C1
    world.at(1)
    world.working()
    world.at(2)  # the dwell restarted at pass 2
    assert world.rig.status() is CP and world.recoveries() == []
    world.at(3)
    assert world.rig.status() is SUBMITTED


def test_a_terminal_record_resets_the_dwell(world: World) -> None:
    """A working V and a bound T in one generation is row 12; the dwell starts over."""

    _lost_cancel(world)
    world.at(0)
    world.venue.completed = [venue_order(CANCELLED, "0")]
    world.at(1)
    world.venue.completed = []
    world.at(2)
    assert world.rig.status() is CP and world.recoveries() == []


def test_D4_a_clock_anomaly_resets_the_dwell(world: World) -> None:
    """Pass 1: 120 s of wall clock but 200 s monotonic (beyond the tolerance)."""

    _lost_cancel(world)
    world.at(0)
    world.at(1, mono_extra=80.0)
    assert world.rig.status() is CP and world.recoveries() == []
    world.at(2, mono_extra=80.0)  # consistent with pass 1: the restarted dwell fires
    assert world.rig.status() is SUBMITTED


def test_D5_the_first_pass_after_a_restart_never_recovers(world: World) -> None:
    _lost_cancel(world)
    world.at(0)
    rig = world.rig
    rig.reconciler = OrderRestartReconciler(  # a new process: no dwell state
        connection=rig.connection,
        intents=rig.intents,
        tracker=rig.tracker,
        reconciliation_readiness=rig.readiness,
        expected_broker_client_id=CLIENT_ID,
        monotonic=world.mono,
    )
    world.at(1)
    assert rig.status() is CP and world.recoveries() == []
    world.at(2)
    assert rig.status() is SUBMITTED


def test_an_incomplete_pass_resets_the_dwell(world: World) -> None:
    _lost_cancel(world)
    world.at(0)
    world.venue.completed = None  # the completed-orders read fails: no complete evidence
    world.at(1)
    world.venue.completed = []
    world.at(2)
    assert world.rig.status() is CP and world.recoveries() == []


def test_a_pass_that_raises_resets_the_dwell(world: World) -> None:
    _lost_cancel(world)
    world.at(0)
    real = world.venue.executions

    async def broken(since=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("executions read failed")

    world.venue.executions = broken  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        world.at(1)
    world.venue.executions = real  # type: ignore[method-assign]
    world.at(2)
    assert world.rig.status() is CP and world.recoveries() == []


# --- D6a, D6b: the mark guard ------------------------------------------------------------------


def test_D6a_a_mark_younger_than_M_does_not_start_the_dwell(world: World) -> None:
    world.rig.pending(at=NOW - timedelta(seconds=30))
    world.working()
    world.at(0)  # mark 30 s old: no dwell
    world.at(1)  # mark 150 s old: the dwell starts HERE
    assert world.rig.status() is CP and world.recoveries() == []
    world.at(2)
    assert world.rig.status() is SUBMITTED


def test_D6b_a_new_cancel_mark_after_the_dwell_began_resets_it(world: World) -> None:
    _lost_cancel(world)
    world.at(0)
    world.rig.event(PF, at=NOW, filled="0", source="ORDER_STATUS")  # the intent left CP ...
    world.rig.pending(at=NOW)  # ... and was cancelled again: a NEW mark
    world.at(1)  # new mark key (and only 120 s old: the dwell restarts here)
    assert world.rig.status() is CP and world.recoveries() == []


# --- D10, D11a-h, D12, D12b: the continuity key ------------------------------------------------


def test_D10_an_ordinary_pass_advances_the_generation_but_not_the_uncertainty_epoch(
    world: World,
) -> None:
    _lost_cancel(world)
    world.at(0)
    generation = world.rig.readiness.snapshot().generation
    epoch = world.rig.readiness.uncertainty_epoch()
    world.at(1)
    assert world.rig.readiness.snapshot().generation > generation
    assert world.rig.readiness.uncertainty_epoch() == epoch
    assert world.rig.status() is SUBMITTED


def _bridge(w: World) -> CallbackBridge:
    bridge: CallbackBridge = w.venue.bridge
    return bridge


def _reset_between_passes(w: World, event: object) -> None:
    _lost_cancel(w)
    w.at(0)
    assert callable(event)
    event()
    w.at(1)
    assert w.rig.status() is CP and w.recoveries() == [], "the dwell survived the event"


def test_D11a_an_unsolicited_execution_resets_the_dwell(world: World) -> None:
    _reset_between_passes(
        world, lambda: _bridge(world).unsolicited.observe_execution(client_id=99, order_id=5)
    )


def test_D11b_an_unsolicited_order_status_resets_the_dwell(world: World) -> None:
    _reset_between_passes(
        world, lambda: _bridge(world).unsolicited.observe_order_status(client_id=99, order_id=5)
    )


def test_D11c_a_managed_account_scope_change_resets_the_dwell(world: World) -> None:
    _bridge(world).on_managed_accounts("DU1234567")
    _reset_between_passes(world, lambda: _bridge(world).on_managed_accounts("DU1234567,DU7654321"))


@pytest.mark.parametrize(
    "reason",
    [
        "paper broker connection or account scope changed",  # submission.py:315 (D11d)
        "live broker connection became unhealthy",  # :398 (D11e)
        "live broker account scope changed",  # :408 (D11f)
    ],
    ids=["D11d", "D11e", "D11f"],
)
def test_D11d_e_f_a_submission_time_invalidation_resets_the_dwell(
    world: World, reason: str
) -> None:
    """These sites call ``readiness.invalidate`` directly (pinned below by source); driving the
    live submission path itself would need ALLOW_* true, which this build never sets."""

    _reset_between_passes(world, lambda: world.rig.readiness.invalidate(reason))


def test_D11d_e_f_the_submission_sites_call_the_readiness_invalidate_method() -> None:
    source = (_ROOT / "src/chronos/orders/submission.py").read_text(encoding="utf-8")
    for reason in (
        '"paper broker connection or account scope changed"',
        '"live broker connection became unhealthy"',
        '"live broker account scope changed"',
    ):
        head = source[: source.index(reason)].rstrip()
        assert head.endswith("self._reconciliation_readiness.invalidate("), reason


def test_D11g_an_adapter_reconnect_resets_the_dwell(world: World) -> None:
    _reset_between_passes(world, lambda: _bridge(world).reset_for_reconnect())


def test_D11g_the_managers_reconnect_resets_the_dwell(world: World) -> None:
    """``connect()`` on a healthy session is a no-op by design (connection.py:158-171); a real
    reconnect is ``disconnect()`` then ``connect()``, and both invalidate."""

    def reconnect() -> None:
        world.rig.connection.disconnect()
        world.rig.connection.connect()

    _reset_between_passes(world, reconnect)


def test_D11h_a_manager_call_timeout_resets_the_dwell() -> None:
    w = World()
    try:
        w.rig.close()
        mono = w.mono
        rig = make_rig(ScriptedVenue(), monotonic=mono, call_timeout_seconds=0.2)
        rig.venue.bridge = CallbackBridge(RequestRegistry(), client_id=CLIENT_ID)
        rig.seed()
        w.rig = rig

        def time_out() -> None:
            with pytest.raises(TimeoutError):
                rig.connection.run(asyncio.sleep(2))

        _reset_between_passes(w, time_out)
    finally:
        w.rig.close()


def test_D12_an_adapter_without_an_adapter_epoch_never_recovers() -> None:
    w = World(bridge=False)
    try:
        _lost_cancel(w)
        for k in range(4):
            w.at(k)
        assert w.rig.status() is CP and w.recoveries() == []
    finally:
        w.rig.close()


@pytest.mark.parametrize("event", ["reset_for_reconnect", "on_connection_closed"])
def test_D12b_a_hookless_adapter_that_reconnects_resets_through_the_adapter_epoch(
    event: str,
) -> None:
    """No observer wired: the latch never hears of it, ``adapter_epoch`` must."""

    w = World(hooked=False)
    try:
        epoch = w.rig.readiness.uncertainty_epoch()
        _reset_between_passes(w, getattr(_bridge(w), event))
        assert w.rig.readiness.uncertainty_epoch() == epoch  # the latch never heard of it
    finally:
        w.rig.close()


def test_an_ordinary_pass_does_not_move_the_adapter_epoch(world: World) -> None:
    before = _bridge(world).adapter_epoch
    _lost_cancel(world)
    world.at(0)
    world.at(1)
    assert _bridge(world).adapter_epoch == before and world.rig.status() is SUBMITTED


# --- D17: the observation rule ------------------------------------------------------------------


def test_D17_the_first_observation_is_one_snapshot_and_writes_nothing(world: World) -> None:
    _lost_cancel(world)
    before = len(world.rig.events())
    world.at(0)
    assert world.venue.open_calls == 1 and len(world.rig.events()) == before


def test_D17_a_firing_observation_without_a_stable_pair_writes_nothing(world: World) -> None:
    _lost_cancel(world)
    world.at(0)
    calls = world.venue.open_calls
    world.venue.open_script = [
        *[(venue_order(SUBMITTED, "0"),)] * calls,  # pass 0's read(s)
        (venue_order(SUBMITTED, "0"),),  # pass 1, planning
        (venue_order(SUBMITTED, "1"),),  # pass 1, confirming: not equal
    ]
    report = world.at(1)
    assert world.rig.status() is CP and world.recoveries() == []
    assert any("evidence changed between snapshots" in r for r in issues(report))


# --- D15: a second lost cancel in the same epoch gets a fresh key -------------------------------


def test_D15_a_second_lost_cancel_is_recovered_again_with_a_fresh_key(world: World) -> None:
    _lost_cancel(world)
    world.at(0)
    world.at(1)
    assert world.rig.status() is SUBMITTED
    world.rig.pending(at=NOW)  # cancelled again (SUBMITTED -> CANCEL_PENDING), lost again
    world.at(2)
    world.at(3)
    assert world.rig.status() is SUBMITTED
    keys = [e.event_key for e in world.rig.events() if e.source == RECOVERY]
    assert len(keys) == 2 and keys[0] != keys[1]


# --- identity: only the exactly bound order is ever recovered -----------------------------------


@pytest.mark.parametrize(
    "fields",
    [{"ref": None}, {"perm": None}, {"client": CLIENT_ID + 1}, {"broker_id": 9002}],
    ids=["no_ref", "no_perm", "client", "broker_id"],
)
def test_a_working_order_that_is_not_exactly_bound_is_never_recovered(
    world: World, fields: dict[str, object]
) -> None:
    world.rig.pending()
    world.working("0", **fields)
    for k in range(3):
        world.at(k)
    assert world.rig.status() is CP and world.recoveries() == []


# --- S-R rows: a recovered SUBMITTED settles on a late terminal record (D7) ---------------------


def _recovered_submitted(w: World) -> None:
    _lost_cancel(w)
    w.at(0)
    w.at(1)
    assert w.rig.status() is SUBMITTED


def test_D7_S_R1_a_late_cancelled_record_after_the_recovery_settles_once(world: World) -> None:
    _recovered_submitted(world)
    world.venue.open = []
    world.venue.completed = [venue_order(CANCELLED, "0")]
    report = world.at(2)
    assert world.rig.status() is CANCELLED, issues(report)
    assert world.rig.trail()[-1] == ("RECONCILE", CANCELLED)
    world.at(3)
    assert [t for t in world.rig.trail() if t[1] is CANCELLED] == [("RECONCILE", CANCELLED)]


def test_S_R2_a_late_filled_record_after_the_recovery_settles_filled(world: World) -> None:
    _recovered_submitted(world)
    world.venue.open = []
    world.venue.completed = [venue_order(FILLED, "10")]
    world.at(2)
    assert world.rig.status() is FILLED


def test_S_R3_a_recovered_submitted_with_no_terminal_record_is_as_today(world: World) -> None:
    _recovered_submitted(world)
    world.venue.open = []
    report = world.at(2)
    assert world.rig.status() is SUBMITTED
    assert any("absent from the broker" in r for r in issues(report)), issues(report)


def test_S_R3_an_inconsistent_late_record_is_as_today(world: World) -> None:
    _recovered_submitted(world)
    world.venue.open = []
    world.venue.completed = [venue_order(FILLED, "7")]  # Filled that does not fill: not proof
    report = world.at(2)
    assert world.rig.status() is SUBMITTED
    # "as today": the existing resolver's reason, not the planner's typed refusal (U21)
    assert any("absent from the broker" in r for r in issues(report)), issues(report)


def test_D7_P1_a_late_cancelled_record_after_a_partial_recovery_settles_once(world: World) -> None:
    world.rig.event(PF, at=PRIOR, filled="4")
    _lost_cancel(world, "4")
    world.at(0)
    world.at(1)
    assert world.rig.status() is PF
    world.venue.open = []
    world.venue.completed = [venue_order(CANCELLED, "4")]
    world.at(2)
    assert world.rig.status() is CANCELLED and world.rig.events()[-1].filled_quantity == 4


def test_a_refused_cancel_revert_is_not_recovery_provenance(world: World) -> None:
    """The cancel service's refusal also writes CANCEL_PENDING -> SUBMITTED (source CANCEL):
    that is not a recovery, so a later Cancelled record is Q7's, not S-R1's (U27)."""

    world.rig.pending()
    world.rig.event(SUBMITTED, at=EARLIER_TODAY, source="CANCEL")  # the refusal's revert
    world.venue.completed = [venue_order(CANCELLED, "0")]
    world.at(0)
    assert world.rig.status() is SUBMITTED


def test_a_submitted_order_without_recovery_provenance_is_unchanged(world: World) -> None:
    """Q7 (an externally cancelled SUBMITTED order) stays the existing resolver's."""

    world.venue.completed = [venue_order(CANCELLED, "0")]
    world.at(0)
    assert world.rig.status() is SUBMITTED


# --- U-B r1, Daybreak P1-1: an unrecognised venue status never recovers or writes -------------


def _unknown_row(filled: str = "0", **fields: object) -> object:
    """An otherwise exact open row whose raw IB status Chronos does not recognise."""

    from chronos.orders.tracker import broker_status_to_lifecycle

    lifecycle = broker_status_to_lifecycle(
        "SomeNewStatus", filled_quantity=Decimal(filled), remaining_quantity=10 - Decimal(filled)
    )
    assert lifecycle is OrderLifecycle.SUBMISSION_UNKNOWN
    return venue_order(lifecycle, filled, **fields)  # type: ignore[arg-type]


def _assert_untouched(w: World, before: int, report: object) -> None:
    assert w.rig.status() is CP and len(w.rig.events()) == before
    assert any("unrecognised venue order status" in r for r in issues(report)), issues(report)  # type: ignore[arg-type]


def test_r1_P1_1_an_unrecognised_status_held_past_D_is_never_recovered(world: World) -> None:
    world.rig.pending()
    world.venue.open = [_unknown_row()]
    before = len(world.rig.events())
    for k in range(3):
        report = world.at(k)
    _assert_untouched(world, before, report)
    assert world.recoveries() == []


def test_r1_P1_1_an_unrecognised_status_records_no_fill_progress(world: World) -> None:
    world.rig.pending()
    world.venue.open = [_unknown_row("3")]
    before = len(world.rig.events())
    _assert_untouched(world, before, world.at(0))


def test_r1_sweep_an_unrecognised_status_filled_by_quantity_is_not_proof(world: World) -> None:
    """C3 rests on a recognised venue row; an unknown status with filled = Q is row 12."""

    world.rig.pending()
    world.venue.open = [_unknown_row("10")]
    before = len(world.rig.events())
    _assert_untouched(world, before, world.at(0))


@pytest.mark.parametrize("lifecycle", [SUBMITTED, OrderLifecycle.SUBMISSION_UNKNOWN])
def test_r1_sweep_a_completed_record_with_a_non_terminal_status_is_not_proof(
    world: World, lifecycle: OrderLifecycle
) -> None:
    """A completed-order record is a bound T only with a terminal status (C7 needs Filled):
    a non-terminal or unrecognised one with filled = Q must not settle FILLED."""

    world.rig.pending()
    world.venue.completed = [venue_order(lifecycle, "10")]
    before = len(world.rig.events())
    report = world.at(0)
    assert world.rig.status() is CP and len(world.rig.events()) == before
    assert any("not a terminal status" in r for r in issues(report)), issues(report)


def test_r1_a_recognised_pending_cancel_row_is_still_row_C1(world: World) -> None:
    """Control: PendingCancel is a recognised status (C1, as built), not an unknown one."""

    world.rig.pending()
    world.venue.open = [venue_order(CP, "0")]
    report = world.at(0)
    assert world.rig.status() is CP
    assert not any("unrecognised" in r for r in issues(report))


# --- U-B r1, Daybreak P2-1: the write is linearised to the epochs its plan confirmed ----------


def _after_the_confirming_plan(w: World, monkeypatch: pytest.MonkeyPatch, event: object) -> None:
    """Run ``event`` right after the SECOND (confirming) plan of the next pass returns, i.e.
    after the equality check's inputs exist and before the lifecycle CAS."""

    real = OrderRestartReconciler._plan_settle
    calls = {"n": 0}

    def wrapped(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        plan = real(self, *args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 2:
            assert callable(event)
            event()
        return plan

    monkeypatch.setattr(OrderRestartReconciler, "_plan_settle", wrapped)


def _transition(w: World) -> None:
    with w.rig.readiness.invalidating_transition("managed-account scope changed"):
        pass


_FINAL_WINDOW_EVENTS = {
    "invalidate": lambda w: w.rig.readiness.invalidate("broker state became uncertain"),
    "invalidating_transition": lambda w: _transition(w),
    "unsolicited_execution": lambda w: _bridge(w).unsolicited.observe_execution(
        client_id=99, order_id=5
    ),
    "adapter_reconnect_reset": lambda w: _bridge(w).reset_for_reconnect(),
    "connection_uncertain": lambda w: _bridge(w)._invalidate_connection("1100 connectivity lost"),
}


@pytest.mark.parametrize("name", sorted(_FINAL_WINDOW_EVENTS))
def test_r1_P2_1_a_safety_event_after_the_confirming_plan_blocks_the_recovery_write(
    world: World, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _lost_cancel(world)
    world.at(0)
    event = _FINAL_WINDOW_EVENTS[name]
    _after_the_confirming_plan(world, monkeypatch, lambda: event(world))
    report = world.at(1)
    assert world.rig.status() is CP and world.recoveries() == [], name
    assert any("changed before the write" in r for r in issues(report)), issues(report)


def test_r1_P2_1_the_same_event_blocks_an_in_place_fill_progress_write(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lost_cancel(world, "2")
    before = len(world.rig.events())
    _after_the_confirming_plan(
        world, monkeypatch, lambda: world.rig.readiness.invalidate("uncertain")
    )
    report = world.at(0)
    assert len(world.rig.events()) == before
    assert any("changed before the write" in r for r in issues(report)), issues(report)


def test_r1_P2_1_control_with_no_event_the_recovery_is_written(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lost_cancel(world)
    world.at(0)
    _after_the_confirming_plan(world, monkeypatch, lambda: None)
    world.at(1)
    assert world.rig.status() is SUBMITTED


# --- fable P3-1: the exact-D boundary and a lost fill-progress CAS ----------------------------


def test_r1_fable_P3_1a_a_pass_exactly_D_after_the_first_observation_recovers(
    world: World,
) -> None:
    _lost_cancel(world)
    world.at(0)
    world.mono.t += 90.0  # exactly D
    with world.rig.readiness.reconciliation_session("pass"):
        world.rig.reconcile(NOW + timedelta(seconds=90))
    assert world.rig.status() is SUBMITTED


def test_r1_fable_P3_1b_a_lost_fill_progress_cas_carries_no_dwell(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the in-place write loses its CAS, the dwell does not survive into the next pass."""

    _lost_cancel(world, "2")
    real = world.rig.tracker.record_planned_transition
    monkeypatch.setattr(world.rig.tracker, "record_planned_transition", lambda **kw: False)
    world.at(0)  # fill progress planned, CAS "lost": no dwell carried
    monkeypatch.setattr(world.rig.tracker, "record_planned_transition", real)
    world.at(1)  # the dwell starts HERE, so no recovery yet
    assert world.rig.status() is CP and world.recoveries() == []


def test_r1_P2_1_an_invalidation_during_the_write_waits_for_it_and_lands_after(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The epochs are HELD across the CAS, not just checked before it: another thread's
    invalidation that arrives mid-write blocks until the write is done (one linearisation)."""

    import threading

    _lost_cancel(world)
    world.at(0)
    epoch = world.rig.readiness.uncertainty_epoch()
    real = world.rig.tracker.record_planned_transition
    seen: dict[str, object] = {}

    def during_the_cas(**kwargs):  # type: ignore[no-untyped-def]
        other = threading.Thread(target=world.rig.readiness.invalidate, args=("uncertain",))
        other.start()
        other.join(timeout=0.3)
        seen["blocked"] = other.is_alive()  # still waiting for the latch's lock
        seen["epoch_during"] = world.rig.readiness._uncertainty_epoch
        seen["thread"] = other
        return real(**kwargs)

    monkeypatch.setattr(world.rig.tracker, "record_planned_transition", during_the_cas)
    world.at(1)
    thread = seen["thread"]
    assert isinstance(thread, threading.Thread)
    thread.join(timeout=5)
    assert seen["blocked"] is True and seen["epoch_during"] == epoch
    assert world.rig.status() is SUBMITTED  # the write linearised BEFORE the invalidation
    assert world.rig.readiness.uncertainty_epoch() == epoch + 1  # which then landed


def test_r1_P2_1_a_bridge_that_cannot_hold_its_epoch_never_recovers() -> None:
    """An adapter bridge exposing ``adapter_epoch`` but not ``held_at_adapter_epoch`` cannot
    linearise the write: the recovery is planned and then refused (fail closed), not written."""

    class EpochOnly:
        adapter_epoch = 0

    w = World(bridge=False)
    try:
        w.venue.bridge = EpochOnly()
        _lost_cancel(w)
        w.at(0)
        report = w.at(1)
        assert w.rig.status() is CP and w.recoveries() == []
        assert any("changed before the write" in r for r in issues(report)), issues(report)
    finally:
        w.rig.close()


# --- U-B r2, Daybreak P1-1: EVERY lifecycle write is linearised to the confirmed epochs --------


def _settle_case(w: World, case: str) -> None:
    from tests.support.settle_rig import venue_execution

    w.rig.pending()
    if case == "s4_terminal":
        w.venue.completed = [venue_order(CANCELLED, "0")]
    elif case == "open_book_terminal":
        w.venue.open = [venue_order(CANCELLED, "0")]
    elif case == "c10a_executions":
        w.venue.execs = [venue_execution("e1", "10", EARLIER_TODAY)]
    else:  # pragma: no cover
        raise AssertionError(case)


_SETTLE_CASES = ["s4_terminal", "open_book_terminal", "c10a_executions"]
_SETTLE_EVENTS = {
    "readiness_invalidate": lambda w: w.rig.readiness.invalidate("broker state became uncertain"),
    "adapter_reconnect_reset": lambda w: _bridge(w).reset_for_reconnect(),
}


@pytest.mark.parametrize("event", sorted(_SETTLE_EVENTS))
@pytest.mark.parametrize("case", _SETTLE_CASES)
def test_r2_P1_1_a_safety_event_after_the_confirming_plan_blocks_every_settle_write(
    world: World, monkeypatch: pytest.MonkeyPatch, case: str, event: str
) -> None:
    """Daybreak's probe: a terminal settle stable across both snapshots, then an invalidation
    right after the confirming plan. Nothing is written; the intent stays remaining-active, so
    readiness cannot be published from the discarded proof."""

    _settle_case(world, case)
    _after_the_confirming_plan(world, monkeypatch, lambda: _SETTLE_EVENTS[event](world))
    report = world.at(0)
    assert world.rig.status() is CP, (case, event)
    assert [r.status for r in report.remaining_active] == [CP]
    assert not report.permits_new_opening_order
    assert any("changed before the write" in r for r in issues(report)), issues(report)


@pytest.mark.parametrize("case", _SETTLE_CASES)
def test_r2_P1_1_control_the_same_settle_with_no_event_is_written(
    world: World, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    _settle_case(world, case)
    _after_the_confirming_plan(world, monkeypatch, lambda: None)
    report = world.at(0)
    assert world.rig.status() in {CANCELLED, FILLED} and report.remaining_active == ()


def test_r2_P1_1_after_a_blocked_settle_the_next_pass_re_proves_on_fresh_evidence(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invalidation lands and forces new evidence: the next pass reads both snapshots again
    under the new epoch, and only then settles."""

    _settle_case(world, "s4_terminal")
    _after_the_confirming_plan(
        world, monkeypatch, lambda: world.rig.readiness.invalidate("uncertain")
    )
    world.at(0)
    calls = world.venue.completed_calls
    monkeypatch.undo()
    world.at(1)
    assert world.rig.status() is CANCELLED
    assert world.venue.completed_calls == calls + 2  # a fresh plan and a fresh confirmation


# --- U-B r2, Daybreak P1-2: REJECTED is never FILLED; the terminal mapping is explicit --------


@pytest.mark.parametrize("filled", ["0", "4", "10"])
@pytest.mark.parametrize("source", ["completed", "open"])
def test_r2_P1_2_a_rejected_terminal_record_never_settles_whatever_its_figure(
    world: World, source: str, filled: str
) -> None:
    """Design r3 row C9: Rejected/Inactive is unexpected for a cancel; none, typed reason --
    decided by the lifecycle BEFORE the quantity (a REJECTED, filled=Q record was FILLED)."""

    world.rig.pending()
    row = venue_order(OrderLifecycle.REJECTED, filled)
    if source == "completed":
        world.venue.completed = [row]
    else:
        world.venue.open = [row]
    before = len(world.rig.events())
    report = world.at(0)
    assert world.rig.status() is CP and len(world.rig.events()) == before
    assert any("unexpected terminal status for a cancel: REJECTED" in r for r in issues(report))


def test_r2_the_terminal_status_to_target_table_is_explicit() -> None:
    """The allowlist, pinned: only these terminal statuses settle, each to exactly this target."""

    from chronos.orders import reconciliation_recovery as rr

    assert rr._TERMINAL_TARGET == {CANCELLED: CANCELLED, FILLED: FILLED}
    assert OrderLifecycle.REJECTED not in rr._TERMINAL_TARGET


def test_r2_C3_a_working_row_filled_by_quantity_still_settles_filled(world: World) -> None:
    """Control: C3 is kept, separately, for a recognised WORKING row whose figure reaches Q."""

    world.rig.pending()
    world.venue.open = [venue_order(SUBMITTED, "10")]
    world.at(0)
    assert world.rig.status() is FILLED


def test_r2_a_pending_cancel_row_filled_by_quantity_is_C1_not_C3(world: World) -> None:
    """Stated choice (the design's C3 names a working V; C1 a PendingCancel V): a PendingCancel
    row whose figure reaches Q records that figure in place and settles nothing; the next pass's
    terminal evidence (Filled at the venue, or the S4 record) settles it."""

    world.rig.pending()
    world.venue.open = [venue_order(CP, "10")]
    world.at(0)
    assert world.rig.status() is CP
    assert world.rig.trail()[-1] == ("FILL_PROGRESS", CP)
