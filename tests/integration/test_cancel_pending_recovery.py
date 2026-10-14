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


def test_r2_a_write_plan_without_epochs_is_refused_there_is_no_bypass(world: World) -> None:
    """W01: every write plan carries its epochs (W02/W03 show a plan without them never
    writes); the guard itself also refuses ``None`` rather than waving the write through."""

    with world.rig.reconciler._epochs_held(None) as current:
        assert current is False
    with world.rig.reconciler._epochs_held(world.rig.reconciler._current_epochs()) as current:
        assert current is True  # positive control


# U-B-r2-LEGACY-3 was a strict xfail at r2; S-3 (main 9a163ec/997dc6b, PR #299: the legacy
# resolver decides lifecycle before quantity) fixed it, so on main it XPASSed and its decorator is
# removed: this is now an ordinary regression pin.
def test_r2_legacy_a_rejected_listed_row_filled_by_quantity_is_not_recorded_filled(
    world: World,
) -> None:
    world.venue.open = [venue_order(OrderLifecycle.REJECTED, "10")]
    world.at(0)
    assert world.rig.status() is SUBMITTED


# --- r3 on main: S-3's cases for a CANCEL_PENDING intent, now decided by the settle planner ------
# S-3 (main, PR #299) pinned these through the legacy resolver, which owned CANCEL_PENDING on main;
# with U-A the planner owns it, so S-3's file keeps the statuses the legacy resolver still decides
# and these are their CANCEL_PENDING counterparts. The safety property is the same (the lock is not
# cleared on contradictory or unproven evidence); the planner's typed reasons are its own.


@pytest.mark.parametrize("filled", ["1", "4", "10"])
def test_r3_on_main_a_rejected_listed_row_with_fills_leaves_a_cancel_pending_locked(
    world: World, filled: str
) -> None:
    world.rig.pending()
    world.venue.open = [venue_order(OrderLifecycle.REJECTED, filled)]
    before = len(world.rig.events())
    report = world.at(0)
    assert world.rig.status() is CP and len(world.rig.events()) == before
    assert not report.permits_new_opening_order
    assert any("unexpected terminal status for a cancel: REJECTED" in r for r in issues(report))


@pytest.mark.parametrize("filled", ["1", "4", "10"])
def test_r3_on_main_an_unrecognised_status_row_with_fills_leaves_a_cancel_pending_locked(
    world: World, filled: str
) -> None:
    world.rig.pending()
    world.venue.open = [_unknown_row(filled)]  # type: ignore[list-item]
    before = len(world.rig.events())
    report = world.at(0)
    assert world.rig.status() is CP and len(world.rig.events()) == before
    assert not report.permits_new_opening_order
    assert any("unrecognised venue order status" in r for r in issues(report))


def test_r3_on_main_a_working_partial_records_progress_in_place_never_partially_filled(
    world: World,
) -> None:
    """S-3's 'a working partial stays a working partial' becomes, for CANCEL_PENDING, design C2-0:
    the figure is recorded in place and the lifecycle does not move (no dwell-less revert)."""

    world.rig.pending()
    world.venue.open = [venue_order(SUBMITTED, "4")]
    report = world.at(0)
    assert world.rig.status() is CP and world.rig.trail()[-1] == ("FILL_PROGRESS", CP)
    assert not report.permits_new_opening_order


def test_r3_on_main_no_evidence_leaves_a_cancel_pending_locked(world: World) -> None:
    world.rig.pending()
    report = world.at(0)
    assert world.rig.status() is CP and not report.permits_new_opening_order
    assert any("order absent; no terminal record" in r for r in issues(report))


@pytest.mark.parametrize("typed", [OrderLifecycle.REJECTED, CANCELLED])
def test_r3_on_main_a_zero_fill_terminal_row_under_recorded_fills_leaves_it_locked(
    world: World, typed: OrderLifecycle
) -> None:
    """S-3's LEGACY-5 'partial-then-cancel-pending' path: 3 recorded, then the cancel mark, then a
    terminal row at 0 fills. The planner refuses (C9 for REJECTED; for CANCELLED the venue figure
    0 is below the witnessed lower bound 3): the recorded fills are never lowered."""

    world.rig.event(PF, at=PRIOR, filled="3")
    world.rig.pending()
    world.venue.open = [venue_order(typed, "0")]
    before = len(world.rig.events())
    report = world.at(0)
    assert world.rig.status() is CP and len(world.rig.events()) == before
    assert not report.permits_new_opening_order


@pytest.mark.parametrize("filled", ["1", "4", "10"])
def test_r3_on_main_a_cancelled_listed_row_with_fills_settles_cancelled_at_the_venue_figure(
    world: World, filled: str
) -> None:
    """The one CANCEL_PENDING case whose OUTCOME differs from S-3's legacy pin (stated, flagged in
    RESULT): an exactly bound terminal open-book CANCELLED row is a bound T (design r3 §3.3), so C4
    settles CANCELLED with the venue's figure -- never promoted to FILLED or PARTIALLY_FILLED (the
    lifecycle decides, as S-3 requires) -- under the stable pair and the epoch guard."""

    world.rig.pending()
    world.venue.open = [venue_order(CANCELLED, filled)]
    world.at(0)
    assert world.rig.status() is CANCELLED
    assert world.rig.events()[-1].filled_quantity == Decimal(filled)


# --- U-B r3, fable r2 P2-1: the epochs are bound at SNAPSHOT time, not at plan time ------------

REF_2 = "CHR-ORD-" + "T" * 32


def _second_cancel_pending_intent(w: World) -> None:
    w.rig.seed(intent_id="intent-2", ref=REF_2, broker_id=9002, permanent_id=109002)
    w.rig.broker_id = 9002
    w.rig.event(CP, at=PRIOR, source="CANCEL", intent_id="intent-2")
    w.rig.broker_id = 9001


@pytest.mark.parametrize("event", sorted(_SETTLE_EVENTS))
def test_r3_fable_P2_1_an_event_during_an_earlier_intents_planning_blocks_a_later_intent(
    world: World, event: str
) -> None:
    """Fable's probe 2: two CANCEL_PENDING intents with exact S4 CANCELLED records; the event fires
    inside the pass's first completed-orders read (intent-1's planning). Intent-2's first snapshot
    was read BEFORE the event, so it must not be settled from it."""

    world.rig.pending()
    _second_cancel_pending_intent(world)
    world.venue.completed = [
        venue_order(CANCELLED, "0"),
        venue_order(CANCELLED, "0", ref=REF_2, broker_id=9002, perm=109002),
    ]

    def once() -> None:
        world.venue.during_completed = None
        _SETTLE_EVENTS[event](world)

    world.venue.during_completed = once
    report = world.at(0)
    assert world.rig.status("intent-1") is CP
    assert world.rig.status("intent-2") is CP, "intent-2 settled from a pre-event snapshot"
    assert not report.permits_new_opening_order


def test_r3_fable_P2_1_control_two_intents_with_no_event_both_settle(world: World) -> None:
    world.rig.pending()
    _second_cancel_pending_intent(world)
    world.venue.completed = [
        venue_order(CANCELLED, "0"),
        venue_order(CANCELLED, "0", ref=REF_2, broker_id=9002, perm=109002),
    ]
    world.at(0)
    assert world.rig.status("intent-1") is CANCELLED
    assert world.rig.status("intent-2") is CANCELLED


@pytest.mark.parametrize("event", sorted(_SETTLE_EVENTS))
@pytest.mark.parametrize("case", _SETTLE_CASES)
def test_r3_fable_P2_1_an_event_after_the_snapshot_read_and_before_the_first_plan_blocks(
    world: World, monkeypatch: pytest.MonkeyPatch, case: str, event: str
) -> None:
    """Fable's probe 3: the event lands after the pass's open-orders/executions read and before any
    plan (here: at the first per-intent tracker read). Nothing is written in that pass."""

    _settle_case(world, case)
    real = world.rig.tracker.broker_order_id
    fired = {"done": False}

    def first_read(*args, **kwargs):  # type: ignore[no-untyped-def]
        if not fired["done"]:
            fired["done"] = True
            _SETTLE_EVENTS[event](world)
        return real(*args, **kwargs)

    monkeypatch.setattr(world.rig.tracker, "broker_order_id", first_read)
    report = world.at(0)
    assert fired["done"]
    assert world.rig.status() is CP, (case, event)
    assert not report.permits_new_opening_order


# --- U-B r3, fable r2 P3-1 / P3-2 --------------------------------------------------------------


def test_r3_fable_P3_1_a_write_plan_without_epochs_is_refused_with_its_own_reason(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    real = OrderRestartReconciler._plan_settle

    def without_epochs(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        plan = real(self, *args, **kwargs)
        return dataclasses.replace(plan, epochs=None) if plan.kind == "write" else plan

    monkeypatch.setattr(OrderRestartReconciler, "_plan_settle", without_epochs)
    _settle_case(world, "s4_terminal")
    report = world.at(0)
    assert world.rig.status() is CP
    assert any("write plan carries no epochs; refused" in r for r in issues(report)), issues(report)


def test_r3_fable_P3_2_the_terminal_table_cannot_be_extended_at_runtime() -> None:
    from chronos.orders import reconciliation_recovery as rr

    with pytest.raises(TypeError):
        rr._TERMINAL_TARGET[OrderLifecycle.REJECTED] = FILLED  # type: ignore[index]


@pytest.mark.parametrize("event", sorted(_SETTLE_EVENTS))
@pytest.mark.parametrize("case", _SETTLE_CASES)
def test_r3_fable_P2_1_an_event_during_the_first_snapshot_read_blocks_the_write(
    world: World, monkeypatch: pytest.MonkeyPatch, case: str, event: str
) -> None:
    """The capture comes BEFORE the read (mutant X03 moves it after): an event that lands inside
    the pass's first executions() read -- the open orders already read under the old epoch --
    must not let the pass settle on that half-old snapshot."""

    _settle_case(world, case)
    real = world.venue.executions
    fired = {"done": False}

    async def first_read(since=None):  # type: ignore[no-untyped-def]
        result = await real(since)
        if not fired["done"]:
            fired["done"] = True
            _SETTLE_EVENTS[event](world)
        return result

    monkeypatch.setattr(world.venue, "executions", first_read)
    report = world.at(0)
    assert fired["done"]
    assert world.rig.status() is CP, (case, event)
    assert not report.permits_new_opening_order


# --- r3b on caf70a5: S-12's CANCEL_PENDING executions control, decided by the planner ------------


def test_r3b_on_caf70a5_exact_executions_reaching_q_settle_a_cancel_pending_with_the_s4_read(
    world: World,
) -> None:
    """S-12's control for CANCEL_PENDING (exact-reference executions reaching Q settle FILLED) is
    C10a here, with the completed-orders read available and no terminal record."""

    from tests.support.settle_rig import venue_execution

    world.rig.pending()
    world.venue.execs = [
        venue_execution("e1", "6", EARLIER_TODAY),
        venue_execution("e2", "4", EARLIER_TODAY),
    ]
    world.at(0)
    assert world.rig.status() is FILLED


def test_r3b_on_caf70a5_without_the_s4_read_exact_executions_leave_a_cancel_pending_locked() -> (
    None
):
    """Fail closed (U-A, design §2.4 / row 12): without the completed-orders capability "no
    terminal record" cannot be established, so even exact executions reaching Q do not settle."""

    from tests.support.settle_rig import VenueWithoutCompletedOrders, venue_execution

    rig = make_rig(VenueWithoutCompletedOrders())
    try:
        rig.seed()
        rig.pending()
        rig.venue.execs = [
            venue_execution("e1", "6", EARLIER_TODAY),
            venue_execution("e2", "4", EARLIER_TODAY),
        ]
        report = rig.reconcile()
        assert rig.status() is CP and not report.permits_new_opening_order
        assert any("completed-orders evidence is not available" in r for r in issues(report))
    finally:
        rig.close()


# --- D-2 (pending Muse; the conductor's default): design r3 C4 for a CANCELLED row with fills -----


@pytest.mark.parametrize("filled", ["1", "4", "10"])
def test_D2_a_cancel_pending_with_an_exactly_bound_cancelled_row_with_fills_settles_cancelled(
    world: World, filled: str
) -> None:
    """D-2, kept as design r3 C4: a CANCEL_PENDING intent whose exactly bound listed row is
    CANCELLED with fills SETTLES CANCELLED at the venue's figure -- the lifecycle decides (never
    FILLED or PARTIALLY_FILLED), under the stable pair and the epoch guard -- and the lock is
    released. This SUPERSEDES S-3's legacy-resolver pin
    test_legacy_resolver_reconcile.py::test_a_rejected_or_cancelled_listed_row_with_fills_leaves_
    the_intent_locked[CANCEL_PENDING-CANCELLED-{1,4,10}] (removed from S-3's SENT_ACTIVE in r3,
    8bbc43f), which kept the intent locked because the LEGACY resolver owned CANCEL_PENDING."""

    world.rig.pending()
    world.venue.open = [venue_order(CANCELLED, filled)]
    report = world.at(0)
    assert world.rig.status() is CANCELLED
    event = world.rig.events()[-1]
    assert (event.source, event.from_status, event.to_status) == ("RECONCILE", CP, CANCELLED)
    assert event.filled_quantity == Decimal(filled)
    assert event.evidence["settled_from"] == "open_order"
    assert report.remaining_active == () and report.permits_new_opening_order
