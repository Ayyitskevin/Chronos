"""U-A r1: the CANCEL_PENDING settle planner against Daybreak's and fable's review of b068493.

Same rig as ``test_cancel_pending_settle.py`` (the REAL reconciler over a real order book; only
the venue is scripted). Each block names the finding it pins:

* Daybreak P1-1 -- exact persisted identity sets (design r3 §2.1), for completed-order records
  AND terminal open-book rows: the persisted client id, the persisted broker-order-id set and the
  persisted permId must each be exactly the venue's;
* Daybreak P1-2 -- a planned write is confirmed by a SECOND complete snapshot (open orders,
  executions AND completed orders) and a full re-plan; the write happens only if both agree
  (§3.1), so a terminal record that changes between the reads is never discarded;
* Daybreak P2-1 / fable P2-1 -- with no bound record and no working order, C10a settles FILLED
  from THIS session's owned executions only (``E_today >= Q``), never from a cross-session sum;
* Daybreak P3-1 -- any failure of the completed-orders read is a typed hold, never an exception
  out of the reconciliation pass;
* fable P3-1/P3-2/P3-3 -- the executions half of the confirming pair, open-book identity
  negatives, and an execution stamped exactly at the session start.

``S0`` for ``NOW`` (Monday 2026-10-12, New York, EDT = UTC-4) is 2026-10-12 04:00 UTC.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.support.settle_rig import (
    BROKER_ID,
    CLIENT_ID,
    EARLIER_TODAY,
    PERM_ID,
    PRIOR,
    Rig,
    ScriptedVenue,
    issues,
    make_rig,
    venue_execution,
    venue_order,
)

from chronos.domain.enums import OrderLifecycle, OrderSide

CP = OrderLifecycle.CANCEL_PENDING
PF = OrderLifecycle.PARTIALLY_FILLED
SUBMITTED = OrderLifecycle.SUBMITTED
CANCELLED = OrderLifecycle.CANCELLED
FILLED = OrderLifecycle.FILLED

AT_S0 = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)  # exactly the session start (00:00 New York)
JUST_BEFORE_S0 = datetime(2026, 10, 12, 3, 59, 59, tzinfo=UTC)


@pytest.fixture
def rig() -> Iterator[Rig]:
    r = make_rig(ScriptedVenue())
    r.seed()
    try:
        yield r
    finally:
        r.close()


def _fresh(**seed: object) -> Rig:
    r = make_rig(ScriptedVenue())
    r.seed(**seed)  # type: ignore[arg-type]
    return r


def _nothing_written(rig: Rig) -> tuple[int, OrderLifecycle]:
    return len(rig.events()), rig.status()


def _assert_refused(rig: Rig, phrase: str) -> None:
    before = len(rig.events())
    report = rig.reconcile()
    assert rig.status() is CP, issues(report)
    assert len(rig.events()) == before
    assert not report.permits_new_opening_order
    assert any(phrase in reason for reason in issues(report)), issues(report)


# --- Daybreak P1-1: exact persisted identity sets ---------------------------------------------


def test_P1_1_a_record_never_binds_an_intent_with_no_persisted_client_id() -> None:
    r = _fresh(client_id=None)
    try:
        r.pending()
        r.venue.completed = [venue_order(CANCELLED, "0")]
        _assert_refused(r, "not bound to this intent")
    finally:
        r.close()


def test_P1_1_a_record_never_binds_an_intent_whose_one_persisted_client_id_differs() -> None:
    """The record's client id equals the CONFIGURED one; the intent persisted another."""

    r = _fresh(client_id=CLIENT_ID + 1)
    try:
        r.pending()
        r.venue.completed = [venue_order(CANCELLED, "0", client=CLIENT_ID)]
        _assert_refused(r, "not bound to this intent")
    finally:
        r.close()


def test_P1_1_a_record_naming_an_order_id_never_binds_an_intent_with_none_persisted() -> None:
    r = _fresh(broker_id=None)
    try:
        r.pending()
        r.venue.completed = [venue_order(CANCELLED, "0", broker_id=BROKER_ID)]
        _assert_refused(r, "not bound to this intent")
    finally:
        r.close()


@pytest.mark.parametrize("record_order_id", [BROKER_ID + 1, 0], ids=["latest", "absent"])
def test_P1_1_a_broker_id_history_with_two_ids_never_binds(rig: Rig, record_order_id: int) -> None:
    """9001 at submit, 9002 on the mark: a persisted conflict, whatever the record names."""

    rig.broker_id = BROKER_ID + 1
    rig.pending()
    rig.venue.completed = [venue_order(CANCELLED, "0", broker_id=record_order_id)]
    _assert_refused(rig, "broker order ids")


@pytest.mark.parametrize(
    ("lifecycle", "filled"),
    [(CANCELLED, "0"), (FILLED, "10"), (SUBMITTED, "10")],
    ids=["cancelled", "filled", "filled_by_quantity"],
)
def test_P1_1_a_terminal_open_book_row_without_a_permid_never_settles(
    rig: Rig, lifecycle: OrderLifecycle, filled: str
) -> None:
    rig.pending()
    rig.venue.open = [venue_order(lifecycle, filled, perm=None)]
    _assert_refused(rig, "not bound to this intent")


def test_P1_1_an_intent_that_never_learned_a_permid_cannot_settle_from_an_open_book_row() -> None:
    r = _fresh(permanent_id=None)
    try:
        r.pending()
        r.venue.open = [venue_order(CANCELLED, "0")]
        _assert_refused(r, "not bound to this intent")
    finally:
        r.close()


def test_P1_1_a_terminal_open_book_row_never_binds_a_differing_persisted_client_id() -> None:
    r = _fresh(client_id=CLIENT_ID + 1)
    try:
        r.pending()
        r.venue.open = [venue_order(CANCELLED, "0", client=CLIENT_ID)]
        _assert_refused(r, "not bound to this intent")
    finally:
        r.close()


def test_P1_1_the_exact_sets_still_bind_a_record_that_carries_no_order_id(rig: Rig) -> None:
    """Control: an S4 row without an orderId (0) binds by permId + client + the rest."""

    rig.pending()
    rig.venue.completed = [venue_order(CANCELLED, "0", broker_id=0)]
    rig.reconcile()
    assert rig.status() is CANCELLED


# fable P3-2: the open-book settle rows had no identity negatives (mutant f5 survived)
_OPEN_BOOK_FIELD_OFF = [
    {"account": "DU7654321"},
    {"client": CLIENT_ID + 1},
    {"broker_id": BROKER_ID + 1},
    {"side": OrderSide.SELL},
    {"limit": Decimal("151.00")},
    {"con_id": 1},
    {"symbol": "MSFT"},
]


@pytest.mark.parametrize("mutation", _OPEN_BOOK_FIELD_OFF, ids=lambda m: next(iter(m)))
@pytest.mark.parametrize(("lifecycle", "filled"), [(CANCELLED, "0"), (FILLED, "10")])
def test_fable_P3_2_a_terminal_open_book_row_with_one_field_off_never_settles(
    rig: Rig, lifecycle: OrderLifecycle, filled: str, mutation: dict[str, object]
) -> None:
    rig.pending()
    rig.venue.open = [venue_order(lifecycle, filled, **mutation)]  # type: ignore[arg-type]
    before = _nothing_written(rig)
    rig.reconcile()
    assert _nothing_written(rig) == before


@pytest.mark.parametrize("mutation", _OPEN_BOOK_FIELD_OFF, ids=lambda m: next(iter(m)))
def test_fable_P3_2_a_working_row_with_one_field_off_records_no_fill_progress(
    rig: Rig, mutation: dict[str, object]
) -> None:
    rig.pending()
    rig.venue.open = [venue_order(SUBMITTED, "4", **mutation)]  # type: ignore[arg-type]
    before = _nothing_written(rig)
    rig.reconcile()
    assert _nothing_written(rig) == before


# --- Daybreak P1-2: the confirming snapshot includes S4 and re-plans -----------------------------


def test_P1_2_a_record_that_turns_filled_between_the_snapshots_is_never_written_cancelled(
    rig: Rig,
) -> None:
    """Daybreak's probe: plan on Cancelled/0, confirm sees Filled/10: the fill is not discarded."""

    rig.pending()
    rig.venue.completed_script = [
        (venue_order(CANCELLED, "0"),),
        (venue_order(FILLED, "10"),),
    ]
    _assert_refused(rig, "evidence changed between snapshots")
    assert rig.venue.completed_calls == 2


def test_P1_2_a_record_that_disappears_between_the_snapshots_is_never_written(rig: Rig) -> None:
    rig.pending()
    rig.venue.completed_script = [(venue_order(CANCELLED, "0"),), ()]
    _assert_refused(rig, "evidence changed between snapshots")


def test_P1_2_a_record_whose_fill_figure_changes_between_the_snapshots_is_never_written(
    rig: Rig,
) -> None:
    rig.pending()
    rig.venue.completed_script = [(venue_order(CANCELLED, "0"),), (venue_order(CANCELLED, "3"),)]
    _assert_refused(rig, "evidence changed between snapshots")


@pytest.mark.parametrize(
    "mutation",
    [
        {"perm": PERM_ID + 1},
        {"client": CLIENT_ID + 1},
        {"account": "DU7654321"},
        {"limit": Decimal("151.00")},
        {"quantity": Decimal("11")},
    ],
    ids=lambda m: next(iter(m)),
)
def test_P1_2_a_record_whose_identity_changes_between_the_snapshots_is_never_written(
    rig: Rig, mutation: dict[str, object]
) -> None:
    rig.pending()
    rig.venue.completed_script = [
        (venue_order(CANCELLED, "0"),),
        (venue_order(CANCELLED, "0", **mutation),),  # type: ignore[arg-type]
    ]
    _assert_refused(rig, "evidence changed between snapshots")


def test_P1_2_a_failing_second_completed_orders_read_is_never_written(rig: Rig) -> None:
    rig.pending()
    rig.venue.completed_script = [(venue_order(CANCELLED, "0"),), None]
    _assert_refused(rig, "evidence changed between snapshots")


def test_P1_2_a_stable_pair_writes_once_and_reads_completed_orders_twice(rig: Rig) -> None:
    rig.pending()
    rig.venue.completed = [venue_order(CANCELLED, "0")]
    before = len(rig.events())
    rig.reconcile()
    assert rig.status() is CANCELLED and len(rig.events()) == before + 1
    assert (rig.venue.open_calls, rig.venue.exec_calls, rig.venue.completed_calls) == (2, 2, 2)


def test_P1_2_an_execution_whose_quantity_changes_between_the_snapshots_blocks_the_write(
    rig: Rig,
) -> None:
    """Same execution id, a different quantity: the old signature (ids only) missed it."""

    rig.event(PF, at=PRIOR, filled="3")
    rig.pending()
    rig.venue.completed = [venue_order(CANCELLED, "5")]
    rig.venue.exec_script = [
        (venue_execution("e1", "2", EARLIER_TODAY),),
        (venue_execution("e1", "3", EARLIER_TODAY),),
    ]
    _assert_refused(rig, "evidence changed between snapshots")


def test_fable_P3_1_an_execution_that_arrives_between_the_snapshots_blocks_the_write(
    rig: Rig,
) -> None:
    rig.pending()
    rig.venue.completed = [venue_order(CANCELLED, "2")]
    rig.venue.exec_script = [
        (venue_execution("e1", "2", EARLIER_TODAY),),
        (venue_execution("e1", "2", EARLIER_TODAY), venue_execution("e2", "1", EARLIER_TODAY)),
    ]
    _assert_refused(rig, "evidence changed between snapshots")


def test_P1_2_fill_progress_is_confirmed_the_same_way(rig: Rig) -> None:
    """C2-0's in-place write rests on the same pair: the venue figure moves between reads."""

    rig.pending()
    rig.venue.open_script = [(venue_order(SUBMITTED, "4"),), (venue_order(SUBMITTED, "6"),)]
    _assert_refused(rig, "evidence changed between snapshots")


# --- Daybreak P2-1 / fable P2-1: C10a is THIS session's executions only ------------------------


def test_P2_1_a_prior_session_figure_of_9_plus_one_execution_today_does_not_settle(
    rig: Rig,
) -> None:
    """Daybreak's probe: 9 recorded before S0 + 1 executed today, no record: C10b, not FILLED."""

    rig.event(PF, at=PRIOR, filled="9")
    rig.pending()
    rig.venue.execs = [venue_execution("e1", "1", EARLIER_TODAY)]
    _assert_refused(rig, "order absent; fills present; no terminal record")


def test_P2_1_fable_a_a_prior_fill_re_reported_with_a_todays_stamp_does_not_settle(
    rig: Rig,
) -> None:
    """Fable's probe (a): 5 recorded Friday; the same 5 come back stamped today. Half filled."""

    rig.event(PF, at=PRIOR, filled="5")
    rig.pending()
    rig.venue.execs = [venue_execution("e1", "5", EARLIER_TODAY)]
    _assert_refused(rig, "order absent; fills present; no terminal record")


def test_P2_1_fable_b_a_friday_figure_of_3_plus_7_today_does_not_settle(rig: Rig) -> None:
    rig.event(PF, at=PRIOR, filled="3")
    rig.pending()
    rig.venue.execs = [venue_execution("e1", "7", EARLIER_TODAY)]
    _assert_refused(rig, "order absent; fills present; no terminal record")


def test_P2_1_executions_stamped_before_the_session_start_do_not_count_toward_C10a(
    rig: Rig,
) -> None:
    """4 executed before S0 (E_prior) + 6 today: E_today is 6, not 10."""

    rig.pending()
    rig.venue.execs = [
        venue_execution("e1", "4", JUST_BEFORE_S0),
        venue_execution("e2", "6", EARLIER_TODAY),
    ]
    _assert_refused(rig, "order absent; fills present; no terminal record")


def test_C10a_this_sessions_executions_alone_settle_even_with_an_earlier_figure_recorded(
    rig: Rig,
) -> None:
    """The reader's corner, per the letter of C10a: E_today = Q settles FILLED; the earlier
    session's figure is not combined with it (design §3.2 "No-record case")."""

    rig.event(PF, at=PRIOR, filled="3")
    rig.pending()
    rig.venue.execs = [
        venue_execution("e1", "6", EARLIER_TODAY),
        venue_execution("e2", "4", EARLIER_TODAY),
    ]
    rig.reconcile()
    assert rig.status() is FILLED
    event = rig.events()[-1]
    assert event.filled_quantity == Decimal("10") and event.remaining_quantity == Decimal("0")
    assert event.evidence["settled_from"] == "executions"
    # the bound is this session's own executions, not a cross-session sum (3 + 10 = 13)
    assert event.evidence["witnessed_lower_bound"] == "10"
    assert event.evidence["unwitnessed_upper_bound"] == "0"


def test_fable_P3_3_C10a_counts_an_execution_stamped_exactly_at_the_session_start(
    rig: Rig,
) -> None:
    rig.pending()
    rig.venue.execs = [venue_execution("e1", "10", AT_S0)]
    rig.reconcile()
    assert rig.status() is FILLED


def test_fable_P3_3_an_execution_stamped_exactly_at_the_session_start_counts_today(
    rig: Rig,
) -> None:
    """3 recorded before S0 + 2 executed AT S0 prove 5; a record saying 4 is refused. (Counting
    the S0 execution as prior would give a lower bound of 3 and settle at 4.)"""

    rig.event(PF, at=PRIOR, filled="3")
    rig.pending()
    rig.venue.execs = [venue_execution("e1", "2", AT_S0)]
    rig.venue.completed = [venue_order(CANCELLED, "4")]
    _assert_refused(rig, "below the witnessed lower bound")


# --- Daybreak P3-1: any failure of the completed-orders read is a typed hold -------------------


@pytest.mark.parametrize(
    "error",
    [RuntimeError("a completed-orders request is already in flight"), ValueError("bad row")],
    ids=["runtime", "value"],
)
def test_P3_1_any_failure_of_the_completed_orders_read_is_a_typed_hold(
    rig: Rig, error: Exception
) -> None:
    rig.pending()
    rig.venue.completed_error = error
    _assert_refused(rig, "completed-orders evidence is not available")
    assert rig.readiness.snapshot().status.value == "PENDING"
