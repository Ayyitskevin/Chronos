"""DEFER356: the deferred #356 reconciliation notes, P3-1 (placeholder sizes) and P3-4 (scale).

Every case drives the REAL ``OrderRestartReconciler`` over the settle rig's in-memory book and a
scripted venue. ``ibapi`` placeholder sizes: ``UNSET_DECIMAL`` is ``2**127 - 1`` and
``UNSET_DOUBLE`` is ``1.7976931348623157e308``; ``execution_from_pair`` accepts any positive size,
so a blank-reference execution can carry one (ASSUMED-IB). Before the P3-1 fix, ``quantize`` raised
``decimal.InvalidOperation`` on such a row-bound sum and the whole pass aborted: one bad row stopped
every other intent. The fix refuses, typed, before quantizing, and the clean second intent settles.
"""

from collections.abc import Iterator
from decimal import Decimal

import pytest
from tests.support.settle_rig import (
    BROKER_ID,
    PERM_ID,
    PRIOR,
    Rig,
    make_rig,
    venue_execution,
    venue_order,
)

from chronos.domain.enums import OrderLifecycle as L

UNSET_DECIMAL = Decimal(2**127 - 1)  # ibapi.const.UNSET_DECIMAL
UNSET_DOUBLE = Decimal("1.7976931348623157E+308")  # ibapi.const.UNSET_DOUBLE
OTHER_REF = "CHR-ORD-" + "T" * 32


@pytest.fixture
def rig() -> Iterator[Rig]:
    instance = make_rig()
    try:
        yield instance
    finally:
        instance.close()


def _seed_clean_second_intent(rig: Rig) -> None:
    """A second SUBMITTED intent with its own ids and a clean CANCELLED@0 row: it must settle."""

    rig.seed(intent_id="intent-2", ref=OTHER_REF, permanent_id=PERM_ID + 1, broker_id=BROKER_ID + 1)
    rig.venue.open.append(
        venue_order(L.CANCELLED, "0", ref=OTHER_REF, perm=PERM_ID + 1, broker_id=BROKER_ID + 1)
    )


@pytest.mark.parametrize(
    "size",
    [Decimal("1E+20"), UNSET_DECIMAL, UNSET_DOUBLE],
    ids=["1e20", "UNSET_DECIMAL", "UNSET_DOUBLE"],
)
def test_a_placeholder_sized_blank_execution_holds_one_intent_and_the_other_still_settles(
    rig: Rig, size: Decimal
) -> None:
    rig.seed()
    rig.pending()
    rig.venue.open = [venue_order(L.CANCELLED, "0")]
    _seed_clean_second_intent(rig)
    rig.venue.execs = [venue_execution("placeholder", size, PRIOR, ref=None, perm=None)]
    before = rig.events()

    report = rig.reconcile()  # before the fix: decimal.InvalidOperation aborts the whole pass

    assert rig.status() is L.CANCEL_PENDING and rig.events() == before
    held = {item.intent_id: item.reason for item in report.unresolved}
    assert set(held) == {"intent-1"}
    assert held["intent-1"].startswith("snapshot fill conflict:")
    assert "exceed the order quantity" in held["intent-1"]
    assert held["intent-1"].endswith(
        "; held for review"
    )  # no credibility claim: an overfill reads the same
    assert "not credible" not in held["intent-1"]
    assert rig.status("intent-2") is L.CANCELLED
    assert {item.intent_id for item in report.applied_updates} == {"intent-2"}
    assert not report.permits_new_opening_order


@pytest.mark.parametrize(
    "sizes,exceeds",
    [(("6", "4"), False), (("6", "4.00000001"), True)],
    ids=["equal-to-quantity", "one-tick-over"],
)
def test_the_quantity_bound_refuses_only_above_the_order_quantity(
    rig: Rig, sizes: tuple[str, str], exceeds: bool
) -> None:
    rig.seed()
    rig.pending()
    rig.venue.open = [venue_order(L.CANCELLED, "0")]
    rig.venue.execs = [
        venue_execution(f"blank-{index}", size, PRIOR, ref=None, perm=None)
        for index, size in enumerate(sizes)
    ]
    before = rig.events()

    report = rig.reconcile()

    assert rig.status() is L.CANCEL_PENDING and rig.events() == before
    reason = report.unresolved[0].reason
    assert reason.startswith("snapshot fill conflict:")
    assert ("exceed the order quantity" in reason) is exceeds
    assert reason.endswith("; held for review") is exceeds
    assert "not credible" not in reason


def test_the_legacy_route_already_holds_a_placeholder_sized_execution_without_aborting(
    rig: Rig,
) -> None:
    rig.seed()  # SUBMITTED: the legacy resolver sums the witnesses without quantizing
    rig.venue.open = [venue_order(L.CANCELLED, "0")]
    _seed_clean_second_intent(rig)
    rig.venue.execs = [venue_execution("placeholder", UNSET_DECIMAL, PRIOR, ref=None, perm=None)]
    before = rig.events()

    report = rig.reconcile()

    assert rig.status() is L.SUBMITTED and rig.events() == before
    assert rig.status("intent-2") is L.CANCELLED
    assert {item.intent_id for item in report.unresolved} == {"intent-1"}


@pytest.mark.parametrize(
    "figure,own_fill,outcome",
    [
        ("5.000000001", "5.000000001", "settle"),  # equal beyond eight places: ruled to settle
        ("5", "5.000000004", "settle"),  # below by less than one eighth place: equal at scale
        ("5", "5.00000001", "refuse"),  # below by one representable tick: still refused
        ("5.00000001", "5", "settle"),  # above the bound: settles before and after
        ("4.999999996", "5", "settle"),  # figure under 5 by 4e-9: equal at the stored scale
        ("4.999999999", "5", "settle"),  # figure one nine-place tick under: equal at scale
        ("4.99999999", "5", "refuse"),  # figure one representable tick under: refused
    ],
    ids=[
        "equal-9dp",
        "under-by-4e-9",
        "under-by-1e-8",
        "over-by-1e-8",
        "figure-under-by-4e-9",
        "figure-one-9dp-tick-under",
        "figure-one-tick-under",
    ],
)
def test_the_witnessed_lower_bound_is_compared_at_the_same_eight_place_scale(
    rig: Rig, figure: str, own_fill: str, outcome: str
) -> None:
    rig.seed()
    rig.pending()
    rig.venue.open = [venue_order(L.CANCELLED, figure)]
    rig.venue.execs = [venue_execution("own-fill", own_fill, PRIOR)]  # referenced: an OWN fill
    before = rig.events()

    report = rig.reconcile()

    if outcome == "settle":
        assert rig.status() is L.CANCELLED and report.unresolved == ()
        assert len(report.applied_updates) == 1
    else:
        assert rig.status() is L.CANCEL_PENDING and rig.events() == before
        assert "below the witnessed lower bound" in report.unresolved[0].reason


def test_a_blank_execution_on_a_reused_broker_id_holds_then_clears_when_it_leaves_the_snapshot(
    rig: Rig,
) -> None:
    """The behaviour behind the docs/limitations.md reused-broker-id paragraph (DEFER356 P3-3)."""

    rig.seed()
    rig.pending()
    rig.venue.open = [venue_order(L.CANCELLED, "0")]
    # another order's fill reported on the same account, client, broker id, side and contract
    rig.venue.execs = [venue_execution("reused-id", "5", PRIOR, ref=None, perm=None)]
    before = rig.events()

    held = rig.reconcile()

    assert rig.status() is L.CANCEL_PENDING and rig.events() == before
    assert held.unresolved[0].reason.startswith("snapshot fill conflict:")
    assert not held.permits_new_opening_order

    rig.venue.execs = []  # the execution leaves the snapshot

    cleared = rig.reconcile()

    assert rig.status() is L.CANCELLED and cleared.unresolved == ()
    assert len(cleared.applied_updates) == 1


# --- R7: Kimi's #361 findings. P2: a placeholder in the venue's OWN figure, or in a recorded
# figure, still aborted the pass. P3: the working-row comparison was not at the stored scale.


def _placeholder_row(lifecycle: L, size: Decimal):
    """A venue row whose figure is a placeholder but whose quantity is still the order's.

    ``BrokerOrder`` validates ``filled + remaining == quantity`` with both non-negative, and the
    official adapter's open-orders read fails the whole snapshot on that (D-43 R4), so this shape
    is built past validation with ``model_copy``: it is what Kimi's reproduction put on the book,
    and the planner must hold it typed, never raise.
    """

    return venue_order(lifecycle, "0").model_copy(
        update={"filled_quantity": size, "remaining_quantity": Decimal("0")}
    )


def _held_typed_and_the_other_settles(
    rig: Rig, report, before, *, label: str, detail: str = "exceeds the order quantity 10"
) -> str:
    assert rig.status() is L.CANCEL_PENDING and rig.events() == before
    held = {item.intent_id: item.reason for item in report.unresolved}
    assert set(held) == {"intent-1"}
    assert held["intent-1"].startswith(label)
    assert detail in held["intent-1"]  # the quantity prints at the book's scale: 10.00000000
    assert held["intent-1"].endswith("; held for review")
    assert rig.status("intent-2") is L.CANCELLED
    assert {item.intent_id for item in report.applied_updates} == {"intent-2"}
    return held["intent-1"]


@pytest.mark.parametrize(
    "size",
    [Decimal("1E+20"), UNSET_DECIMAL, UNSET_DOUBLE],
    ids=["1e20", "UNSET_DECIMAL", "UNSET_DOUBLE"],
)
@pytest.mark.parametrize("lifecycle", [L.CANCELLED, L.FILLED], ids=["cancelled", "filled"])
def test_a_placeholder_terminal_figure_holds_one_intent_and_the_other_still_settles(
    rig: Rig, lifecycle: L, size: Decimal
) -> None:
    rig.seed()
    rig.pending()
    rig.venue.open = [_placeholder_row(lifecycle, size)]
    _seed_clean_second_intent(rig)
    before = rig.events()

    report = rig.reconcile()  # before the fix: decimal.InvalidOperation aborts the whole pass

    _held_typed_and_the_other_settles(rig, report, before, label="venue fill figure")


@pytest.mark.parametrize(
    "lifecycle", [L.SUBMITTED, L.CANCEL_PENDING], ids=["filled-by-quantity", "pending-cancel"]
)
def test_a_placeholder_working_figure_holds_typed_on_both_working_paths(
    rig: Rig, lifecycle: L
) -> None:
    """SUBMITTED at a placeholder is "filled by quantity" (C3) and reaches the settle; a
    PendingCancel row (C1) takes the in-place path, where the placeholder was WRITTEN as fill
    progress before the fix (the C3 one raised)."""

    rig.seed()
    rig.pending()
    rig.venue.open = [_placeholder_row(lifecycle, UNSET_DECIMAL)]
    _seed_clean_second_intent(rig)
    before = rig.events()

    report = rig.reconcile()

    _held_typed_and_the_other_settles(rig, report, before, label="venue fill figure")


@pytest.mark.parametrize(
    "recorded,detail",
    [("1E+20", "exceeds the order quantity 10"), ("1E+400", "is not finite")],
    ids=["1e20", "beyond-float-range"],
)
@pytest.mark.parametrize(
    "venue_row", [L.CANCELLED, L.SUBMITTED], ids=["terminal-path", "working-path"]
)
def test_a_placeholder_recorded_figure_holds_typed_instead_of_aborting(
    rig: Rig, venue_row: L, recorded: str, detail: str
) -> None:
    """The witnessed lower bound reads the book's recorded figures. A status callback carrying
    ibapi's UNSET_DECIMAL passes the consumer's 1e100 placeholder threshold and the tracker's
    finite-and-non-negative check, so the book can hold one; the planner must hold the intent
    typed, not abort on quantizing the bound. ``order_events`` stores REAL: 1E+20 reads back
    exactly, and a figure beyond float range reads back as ``Infinity`` (docs/limitations.md,
    D-43 Q3), which the bound carries and the planner must also hold typed."""

    rig.seed()
    rig.event(L.PARTIALLY_FILLED, at=PRIOR, filled=recorded, quantity=Decimal(recorded))
    rig.pending()
    rig.venue.open = [venue_order(venue_row, "0")]
    _seed_clean_second_intent(rig)
    before = rig.events()

    report = rig.reconcile()

    reason = _held_typed_and_the_other_settles(
        rig, report, before, label="witnessed lower bound", detail=detail
    )
    assert "below the witnessed lower bound" not in reason


@pytest.mark.parametrize(
    "figure,own_fill,outcome",
    [
        ("5", "5.000000004", "progress"),  # Kimi's P3 case: equal at the stored scale
        ("5.000000001", "5.000000001", "progress"),  # equal beyond eight places
        ("5", "5.00000001", "refuse"),  # below by one representable tick: still refused
    ],
    ids=["under-by-4e-9", "equal-9dp", "under-by-1e-8"],
)
def test_the_working_row_comparison_uses_the_same_eight_place_scale(
    rig: Rig, figure: str, own_fill: str, outcome: str
) -> None:
    """A working SUBMITTED row is compared to the witnessed bound at the same scale as a terminal
    one (P3-4 extended, Kimi P3). Equal at eight places records the venue figure as fill
    progress in place; one tick under refuses."""

    rig.seed()
    rig.pending()
    rig.venue.open = [venue_order(L.SUBMITTED, figure)]
    rig.venue.execs = [venue_execution("own-fill", own_fill, PRIOR)]  # referenced: an OWN fill
    before = rig.events()

    report = rig.reconcile()

    assert rig.status() is L.CANCEL_PENDING
    if outcome == "progress":
        assert report.unresolved == ()
        reasons = [item.reason for item in report.proven if item.intent_id == "intent-1"]
        assert reasons == ["fill progress recorded in place"]  # in place: no lifecycle change
        assert len(rig.events()) == len(before) + 1
        stored = Decimal(figure).quantize(Decimal("0.00000001"))  # the book keeps eight places
        assert rig.events()[-1].filled_quantity == stored
    else:
        assert rig.events() == before
        assert "below the witnessed lower bound" in report.unresolved[0].reason
