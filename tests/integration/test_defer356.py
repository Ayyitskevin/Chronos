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
