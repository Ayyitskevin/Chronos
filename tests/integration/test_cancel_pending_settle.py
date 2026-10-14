"""The CANCEL_PENDING settle planner (design r3 §3, U-A), row by row.

The reconciler is the real ``OrderRestartReconciler`` over a real order book; the venue is a
scripted double (``tests/support/settle_rig.py``). One test per design row, plus the session-
partitioned fill witness, the identity negatives, and the controls that keep the rule fail-closed:
absence alone never settles, a field mismatch refuses, an inconsistent venue figure refuses.

Mapping to the B1 cases this build could not cherry-pick (they live on ``claude/b1-ready``):
``test_a_two_step_cancel_settles_after_reconciliation`` -> ``test_C4_a_two_step_cancel_settles_*``
in ``test_cancel_pending_settle_gateway.py``; the two ``U0-D1`` rows -> the same file's plain and
partial cases (S2 itself is not on this base).
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from tests.support.settle_rig import (
    BEFORE_OPEN,
    EARLIER_TODAY,
    PERM_ID,
    PRIOR,
    QUANTITY,
    Rig,
    ScriptedVenue,
    VenueWithoutCompletedOrders,
    issues,
    make_rig,
    venue_execution,
    venue_order,
)

from chronos.domain.enums import OrderLifecycle

CP = OrderLifecycle.CANCEL_PENDING
PF = OrderLifecycle.PARTIALLY_FILLED
SUBMITTED = OrderLifecycle.SUBMITTED
CANCELLED = OrderLifecycle.CANCELLED
FILLED = OrderLifecycle.FILLED
REJECTED = OrderLifecycle.REJECTED


@pytest.fixture
def rig() -> Iterator[Rig]:
    r = make_rig(ScriptedVenue())
    r.seed()
    try:
        yield r
    finally:
        r.close()


def _writes(rig: Rig, run: object = None) -> int:
    before = len(rig.events())
    rig.reconcile()
    return len(rig.events()) - before


def _prior_fill(rig: Rig, quantity: str) -> None:
    rig.event(PF, at=PRIOR, filled=quantity)


def _today_fill(rig: Rig, quantity: str) -> None:
    rig.event(PF, at=EARLIER_TODAY, filled=quantity)


# --- the basic settle rows -------------------------------------------------------------------


def test_C4_a_cancelled_and_absent_order_with_a_completed_record_settles(rig: Rig) -> None:
    rig.pending()
    rig.venue.completed = [venue_order(CANCELLED, "0")]
    assert _writes(rig) == 1
    assert rig.status() is CANCELLED
    assert rig.trail()[-1] == ("RECONCILE", CANCELLED)
    report = rig.reconcile()  # idempotent: nothing left to do
    assert report.remaining_active == () and report.permits_new_opening_order


def test_C4_the_settling_event_carries_the_venue_figure_and_the_bounds(rig: Rig) -> None:
    _prior_fill(rig, "3")
    rig.pending()
    rig.venue.execs = [venue_execution("e1", "2", EARLIER_TODAY)]
    rig.venue.completed = [venue_order(CANCELLED, "5")]
    rig.reconcile()
    event = rig.events()[-1]
    assert event.to_status is CANCELLED and event.filled_quantity == Decimal("5")
    assert event.remaining_quantity == Decimal("5")
    assert event.evidence["witnessed_lower_bound"] == "5"
    assert event.evidence["unwitnessed_upper_bound"] == "0"


def test_C4_settling_twice_writes_one_terminal_event(rig: Rig) -> None:
    rig.pending()
    rig.venue.completed = [venue_order(CANCELLED, "0")]
    rig.reconcile()
    rig.reconcile()
    assert [t for t in rig.trail() if t[1] is CANCELLED] == [("RECONCILE", CANCELLED)]


def test_C7_a_filled_completed_record_settles_filled(rig: Rig) -> None:
    rig.pending()
    rig.venue.completed = [venue_order(FILLED, "10")]
    rig.reconcile()
    assert rig.status() is FILLED
    assert rig.events()[-1].filled_quantity == QUANTITY


def test_C3_an_open_book_row_that_is_filled_by_quantity_settles_filled(rig: Rig) -> None:
    rig.pending()
    rig.venue.open = [venue_order(FILLED, "10")]
    rig.reconcile()
    assert rig.status() is FILLED


def test_a_terminal_open_book_row_settles_like_a_bound_terminal_record(rig: Rig) -> None:
    rig.pending()
    rig.venue.open = [venue_order(CANCELLED, "0")]
    rig.reconcile()
    assert rig.status() is CANCELLED


def test_C10a_one_session_of_executions_that_fill_the_order_settle_filled(rig: Rig) -> None:
    rig.pending()
    rig.venue.execs = [
        venue_execution("e1", "6", EARLIER_TODAY),
        venue_execution("e2", "4", EARLIER_TODAY),
    ]
    rig.reconcile()
    assert rig.status() is FILLED


# --- absence alone, and the executions-only branch ------------------------------------------


def test_C11_absence_alone_never_settles(rig: Rig) -> None:
    rig.pending()
    report = rig.reconcile()
    assert rig.status() is CP
    assert [r.status for r in report.remaining_active] == [CP]
    assert not report.permits_new_opening_order
    assert any("cancel pending; order absent; no terminal record" in r for r in issues(report))
    assert rig.trail()[-1][1] is CP and len(rig.events()) == 2  # SUBMIT + the mark, nothing else


def test_C10b_executions_without_a_terminal_record_do_not_record_partially_filled(
    rig: Rig,
) -> None:
    """The old executions-only branch re-recorded this order as PARTIALLY_FILLED forever."""

    rig.pending()
    rig.venue.execs = [venue_execution("e1", "5", EARLIER_TODAY)]
    report = rig.reconcile()
    assert rig.status() is CP
    assert any("order absent; fills present; no terminal record" in r for r in issues(report))


def test_the_stranded_partial_then_cancel_settles_once_the_record_exists(rig: Rig) -> None:
    """R2' / R2''': partial fill not yet reconciled, then the cancel, then two passes."""

    rig.pending()
    rig.venue.execs = [venue_execution("e1", "5", EARLIER_TODAY)]
    rig.reconcile()
    assert rig.status() is CP  # pass 1: no record yet, and NOT partially filled
    rig.venue.completed = [venue_order(CANCELLED, "5")]
    rig.reconcile()  # pass 2
    assert rig.status() is CANCELLED
    assert rig.events()[-1].filled_quantity == Decimal("5")
    assert [t for t in rig.trail() if t[1] is CANCELLED] == [("RECONCILE", CANCELLED)]


def test_fill_evidence_and_the_terminal_record_in_the_same_pass_write_one_event(rig: Rig) -> None:
    rig.pending()
    rig.venue.execs = [venue_execution("e1", "5", EARLIER_TODAY)]
    rig.venue.completed = [venue_order(CANCELLED, "5")]
    assert _writes(rig) == 1
    assert rig.status() is CANCELLED
    assert PF not in [t[1] for t in rig.trail()]


# --- the session-partitioned fill witness (Daybreak P2-1a) ----------------------------------


def test_fill_a_prior_session_fill_plus_this_session_settles_at_the_venue_figure(rig: Rig) -> None:
    _prior_fill(rig, "3")  # an event dated before S0
    rig.pending()
    rig.venue.execs = [venue_execution("e1", "2", EARLIER_TODAY)]
    rig.venue.completed = [venue_order(CANCELLED, "5")]
    assert _writes(rig) == 1
    assert rig.status() is CANCELLED and rig.events()[-1].filled_quantity == Decimal("5")


def test_fill_a_venue_figure_below_the_disjoint_lower_bound_is_refused(rig: Rig) -> None:
    """Daybreak's probe: 3 before S0 + 2 now prove at least 5; a record saying 4 is a lie."""

    _prior_fill(rig, "3")
    rig.pending()
    rig.venue.execs = [venue_execution("e1", "2", EARLIER_TODAY)]
    rig.venue.completed = [venue_order(CANCELLED, "4")]
    report = rig.reconcile()
    assert rig.status() is CP
    assert any("below the witnessed lower bound" in r for r in issues(report))


def test_fill_same_session_figures_are_not_double_counted(rig: Rig) -> None:
    _today_fill(rig, "2")  # a tracker figure dated today already counts these executions
    rig.pending()
    rig.venue.execs = [venue_execution("e1", "2", EARLIER_TODAY)]
    rig.venue.completed = [venue_order(CANCELLED, "2")]
    rig.reconcile()
    assert rig.status() is CANCELLED and rig.events()[-1].filled_quantity == Decimal("2")


def test_fill_a_venue_figure_above_the_order_size_is_refused(rig: Rig) -> None:
    rig.pending()
    # BrokerOrder itself forbids filled > quantity, so the lie is a record for a larger order
    rig.venue.completed = [venue_order(CANCELLED, "11", quantity=Decimal("11"))]
    report = rig.reconcile()
    assert rig.status() is CP
    assert any("not bound to this intent" in r for r in issues(report))


def test_fill_an_earlier_session_fill_nobody_witnessed_settles_with_the_upper_bound(
    rig: Rig,
) -> None:
    rig.pending()
    rig.venue.completed = [venue_order(CANCELLED, "4")]
    rig.reconcile()
    event = rig.events()[-1]
    assert rig.status() is CANCELLED and event.filled_quantity == Decimal("4")
    assert event.evidence["witnessed_lower_bound"] == "0"
    assert event.evidence["unwitnessed_upper_bound"] == "4"


def test_fill_an_execution_stamped_before_the_session_start_counts_as_prior(rig: Rig) -> None:
    rig.pending()
    rig.venue.execs = [
        venue_execution("e1", "2", BEFORE_OPEN),  # before S0: a pre-session fill
        venue_execution("e2", "2", EARLIER_TODAY),
    ]
    rig.venue.completed = [venue_order(CANCELLED, "3")]
    report = rig.reconcile()
    assert rig.status() is CP  # 2 (before S0) + 2 (after) are disjoint: at least 4 > 3
    assert any("below the witnessed lower bound" in r for r in issues(report))
    rig.venue.completed = [venue_order(CANCELLED, "4")]
    rig.reconcile()
    assert rig.status() is CANCELLED


def test_fill_clock_skew_that_double_counts_one_fill_refuses_and_never_settles(rig: Rig) -> None:
    _prior_fill(rig, "3")
    rig.pending()
    # the same 3 shares, but the execution is stamped after S0: the bound doubles, so refuse
    rig.venue.execs = [venue_execution("e1", "3", EARLIER_TODAY)]
    rig.venue.completed = [venue_order(CANCELLED, "3")]
    rig.reconcile()
    assert rig.status() is CP


# --- the working-order rows (C1, C2-0) -------------------------------------------------------


def test_C1_a_pending_cancel_at_the_venue_is_left_alone(rig: Rig) -> None:
    rig.pending()
    rig.venue.open = [venue_order(CP, "0")]
    before = len(rig.events())
    report = rig.reconcile()
    assert rig.status() is CP and len(rig.events()) == before
    assert not any("PendingCancel" in r for r in issues(report))
    assert [o.reason for o in report.proven if o.intent_id == "intent-1"]


def test_C2_0_new_fills_are_recorded_in_place_from_the_venue_figure(rig: Rig) -> None:
    _prior_fill(rig, "3")
    rig.pending()
    rig.venue.execs = [venue_execution("e1", "2", EARLIER_TODAY)]
    rig.venue.open = [venue_order(PF, "5")]
    assert _writes(rig) == 1
    assert rig.status() is CP  # the lifecycle did not move
    last = rig.events()[-1]
    assert (last.source, last.to_status, last.from_status) == ("FILL_PROGRESS", CP, CP)
    assert last.filled_quantity == Decimal("5")  # never max(3, 2)


def test_C2_0_unchanged_fills_write_nothing_and_the_order_stays_pending(rig: Rig) -> None:
    _prior_fill(rig, "3")
    rig.pending()
    rig.venue.open = [venue_order(PF, "3")]
    assert _writes(rig) == 0
    assert rig.status() is CP


def test_C2_0_a_working_order_with_no_fills_writes_nothing(rig: Rig) -> None:
    rig.pending()
    rig.venue.open = [venue_order(SUBMITTED, "0")]
    assert _writes(rig) == 0
    assert rig.status() is CP


def test_C2_0_never_moves_the_lifecycle_or_reenables_a_cancel(rig: Rig) -> None:
    """The old branch wrote CANCEL_PENDING -> PARTIALLY_FILLED (cancellable) immediately."""

    rig.pending()
    rig.venue.open = [venue_order(PF, "5")]
    rig.venue.execs = [venue_execution("e1", "5", EARLIER_TODAY)]
    rig.reconcile()
    assert rig.status() is CP
    assert PF not in [t[1] for t in rig.trail()[2:]]


@pytest.mark.parametrize("figure", ["4", "0"])
def test_C2_0_an_inconsistent_or_underreported_venue_figure_is_refused(
    rig: Rig, figure: str
) -> None:
    """4 is below the disjoint bound (3 + 2); 0 is an empty status cache read as zero."""

    _prior_fill(rig, "3")
    rig.pending()
    rig.venue.execs = [venue_execution("e1", "2", EARLIER_TODAY)]
    lifecycle = SUBMITTED if figure == "0" else PF
    rig.venue.open = [venue_order(lifecycle, figure)]
    before = len(rig.events())
    report = rig.reconcile()
    assert rig.status() is CP and len(rig.events()) == before
    assert any("below the witnessed lower bound" in r for r in issues(report))


def test_a_working_order_and_a_bound_terminal_record_disagree_and_nothing_is_written(
    rig: Rig,
) -> None:
    rig.pending()
    rig.venue.open = [venue_order(SUBMITTED, "0")]
    rig.venue.completed = [venue_order(CANCELLED, "0")]
    before = len(rig.events())
    report = rig.reconcile()
    assert rig.status() is CP and len(rig.events()) == before
    assert any("disagree" in r for r in issues(report))


# --- the other terminal outcomes ---------------------------------------------------------------


def test_C8_a_filled_record_that_does_not_fill_the_order_is_not_proof(rig: Rig) -> None:
    rig.pending()
    rig.venue.completed = [venue_order(FILLED, "7")]
    report = rig.reconcile()
    assert rig.status() is CP
    assert any("does not fill the order" in r for r in issues(report))


def test_C9_a_rejected_record_is_unexpected_for_a_cancel(rig: Rig) -> None:
    rig.pending()
    rig.venue.completed = [venue_order(REJECTED, "0")]
    report = rig.reconcile()
    assert rig.status() is CP
    assert any("unexpected terminal status for a cancel" in r for r in issues(report))


# --- identity: a record that is not exactly this order never settles ---------------------------


@pytest.mark.parametrize(
    "mutation",
    [
        {"perm": PERM_ID + 1},
        {"perm": None},
        {"client": 18},
        {"account": "DU7654321"},
        {"broker_id": 9002},
        {"side": "SELL"},
        {"quantity": Decimal("11")},
        {"limit": Decimal("151.00")},
        {"con_id": 1},
        {"symbol": "MSFT"},
    ],
    ids=lambda m: next(iter(m)),
)
def test_C12_a_record_with_one_field_off_never_settles(
    rig: Rig, mutation: dict[str, object]
) -> None:
    from chronos.domain.enums import OrderSide

    args: dict[str, object] = dict(mutation)
    if args.get("side") == "SELL":
        args["side"] = OrderSide.SELL
    rig.pending()
    rig.venue.completed = [venue_order(CANCELLED, "0", **args)]  # type: ignore[arg-type]
    before = len(rig.events())
    report = rig.reconcile()
    assert rig.status() is CP and len(rig.events()) == before
    assert any("not bound to this intent" in r for r in issues(report))


def test_a_record_with_a_blank_reference_is_not_evidence_for_this_intent(rig: Rig) -> None:
    rig.pending()
    rig.venue.completed = [venue_order(CANCELLED, "0", ref=None)]
    report = rig.reconcile()
    assert rig.status() is CP
    assert any("no terminal record" in r for r in issues(report))


def test_two_records_for_one_reference_are_ambiguous_and_settle_nothing(rig: Rig) -> None:
    rig.pending()
    rig.venue.completed = [venue_order(CANCELLED, "0"), venue_order(CANCELLED, "0")]
    report = rig.reconcile()
    assert rig.status() is CP
    assert any("ambiguous" in r for r in issues(report))


def test_an_intent_with_no_persisted_permid_can_never_bind_a_record() -> None:
    r = make_rig(ScriptedVenue())
    try:
        r.seed(permanent_id=None)
        r.pending()
        r.venue.completed = [venue_order(CANCELLED, "0")]
        report = r.reconcile()
        assert r.status() is CP
        assert any("not bound to this intent" in x for x in issues(report))
    finally:
        r.close()


def test_a_record_without_an_order_id_still_binds_by_the_other_fields(rig: Rig) -> None:
    rig.pending()
    rig.venue.completed = [venue_order(CANCELLED, "0", broker_id=0)]
    rig.reconcile()
    assert rig.status() is CANCELLED


def test_a_persisted_permid_conflict_settles_nothing(rig: Rig) -> None:
    rig.event(PF, at=PRIOR, filled="1", permanent_id=PERM_ID + 7)
    rig.pending()
    rig.venue.completed = [venue_order(CANCELLED, "1")]
    before = len(rig.events())
    rig.reconcile()
    assert rig.status() is CP and len(rig.events()) == before


# --- PARTIALLY_FILLED rows ---------------------------------------------------------------------


def test_P1_a_partially_filled_order_the_venue_cancelled_settles_cancelled(rig: Rig) -> None:
    """Includes the intents the old executions-only branch had already stranded."""

    _prior_fill(rig, "5")
    rig.venue.completed = [venue_order(CANCELLED, "5")]
    rig.reconcile()
    assert rig.status() is CANCELLED and rig.events()[-1].filled_quantity == Decimal("5")


def test_P2_a_partially_filled_order_the_venue_filled_settles_filled(rig: Rig) -> None:
    _prior_fill(rig, "5")
    rig.venue.completed = [venue_order(FILLED, "10")]
    rig.reconcile()
    assert rig.status() is FILLED


def test_P3_a_partially_filled_order_with_no_record_is_left_to_the_existing_resolver(
    rig: Rig,
) -> None:
    _prior_fill(rig, "5")
    rig.reconcile()
    assert rig.status() is PF


def test_P4_an_inconsistent_record_for_a_partially_filled_order_settles_nothing(rig: Rig) -> None:
    _prior_fill(rig, "5")
    rig.venue.completed = [venue_order(CANCELLED, "4")]
    before = len(rig.events())
    report = rig.reconcile()
    assert rig.status() is PF and len(rig.events()) == before
    assert any("below the witnessed lower bound" in r for r in issues(report))


def test_P5_a_partially_filled_order_the_venue_still_holds_is_unchanged(rig: Rig) -> None:
    _prior_fill(rig, "5")
    rig.venue.open = [venue_order(PF, "5")]
    rig.reconcile()
    assert rig.status() is PF


# --- the capability, the reads and the confirming snapshot ------------------------------------


def test_without_the_completed_orders_capability_an_absent_order_stays_pending() -> None:
    r = make_rig(VenueWithoutCompletedOrders())
    try:
        r.seed()
        r.pending()
        report = r.reconcile()
        assert r.status() is CP
        assert any("completed-orders evidence is not available" in x for x in issues(report))
    finally:
        r.close()


def test_a_failed_completed_orders_read_is_a_typed_reason_and_writes_nothing(rig: Rig) -> None:
    rig.pending()
    rig.venue.completed = None  # the read raises
    before = len(rig.events())
    report = rig.reconcile()
    assert rig.status() is CP and len(rig.events()) == before
    assert any("completed-orders evidence is not available" in r for r in issues(report))
    assert rig.readiness.snapshot().status.value == "PENDING"


def test_completed_orders_are_not_read_while_every_pending_order_is_still_listed(rig: Rig) -> None:
    rig.pending()
    rig.venue.open = [venue_order(CP, "0")]
    rig.reconcile()
    assert rig.venue.completed_calls == 0


def test_completed_orders_are_not_read_when_nothing_is_cancel_pending_or_partially_filled(
    rig: Rig,
) -> None:
    rig.reconcile()  # a plain SUBMITTED order
    assert rig.venue.completed_calls == 0


def test_a_planned_write_is_confirmed_by_a_second_snapshot_and_dropped_if_it_changed(
    rig: Rig,
) -> None:
    rig.pending()
    rig.venue.completed = [venue_order(CANCELLED, "0")]
    rig.venue.open_script = [(), (venue_order(SUBMITTED, "0"),)]  # the order reappears
    before = len(rig.events())
    report = rig.reconcile()
    assert rig.venue.open_calls == 2
    assert rig.status() is CP and len(rig.events()) == before
    assert any("evidence changed between snapshots" in r for r in issues(report))


def test_a_pass_with_no_planned_write_takes_one_snapshot(rig: Rig) -> None:
    rig.pending()
    rig.reconcile()
    assert rig.venue.open_calls == 1 and rig.venue.exec_calls == 1


def test_a_confirmed_write_takes_exactly_two_snapshots(rig: Rig) -> None:
    rig.pending()
    rig.venue.completed = [venue_order(CANCELLED, "0")]
    rig.reconcile()
    assert rig.venue.open_calls == 2 and rig.status() is CANCELLED


# --- races and the terminal guard ---------------------------------------------------------------


def test_a_cancel_completion_that_wins_the_race_leaves_one_terminal_event(rig: Rig) -> None:
    """The cancel service's own CANCEL_PENDING -> CANCELLED lands during the planner's read."""

    rig.pending()
    rig.venue.completed = [venue_order(CANCELLED, "0")]

    def complete_cancel() -> None:
        rig.event(CANCELLED, at=EARLIER_TODAY, source="CANCEL")
        rig.venue.during_completed = None

    rig.venue.during_completed = complete_cancel
    rig.reconcile()  # the planner's CAS loses: no exception, no second event
    assert rig.status() is CANCELLED
    assert [t for t in rig.trail() if t[1] is CANCELLED] == [("CANCEL", CANCELLED)]


def test_a_terminal_status_is_never_written_over(rig: Rig) -> None:
    rig.pending()
    rig.event(FILLED, at=EARLIER_TODAY, filled="10", source="ORDER_STATUS")
    rig.venue.completed = [venue_order(CANCELLED, "0")]
    rig.reconcile()
    assert rig.status() is FILLED
    assert [t for t in rig.trail() if t[1] is CANCELLED] == []


def test_the_planner_writes_at_most_one_event_per_intent_per_pass(rig: Rig) -> None:
    _prior_fill(rig, "3")
    rig.pending()
    rig.venue.execs = [venue_execution("e1", "2", EARLIER_TODAY)]
    rig.venue.completed = [venue_order(CANCELLED, "5")]
    for _ in range(3):
        before = len(rig.events())
        rig.reconcile()
        assert len(rig.events()) - before <= 1
