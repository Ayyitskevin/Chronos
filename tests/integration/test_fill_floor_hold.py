"""FLOORHOLD: the recorded-floor hold shows up as itself (observability only).

Every place the reconciler refuses a venue fill figure for being below a figure Chronos recorded or
witnessed tags the observation with a typed ``FillFloorHold`` (basis, floor figure, venue figure).
Each site is driven with its own data (``tests/support/fill_floor_scenarios.py``); a hold that is
not of this kind carries none; a clean intent beside a hold gets none; and the tag goes exactly
when the venue later reports a figure at or above the recorded one.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.support.fill_floor_scenarios import HOLD_SCENARIOS, SCENARIOS, FloorScenario
from tests.support.settle_rig import (
    BROKER_ID,
    PERM_ID,
    Rig,
    make_rig,
    venue_order,
)

from chronos.domain.enums import OrderLifecycle as L
from chronos.orders.reconciliation_recovery import FillFloorBasis, FillFloorHold


@pytest.fixture
def rig() -> Iterator[Rig]:
    instance = make_rig()
    try:
        yield instance
    finally:
        instance.close()


def _plain(value: object) -> str:
    return format(value.normalize(), "f")  # type: ignore[attr-defined]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_each_site_tags_its_hold_with_the_figures_and_every_other_hold_carries_none(
    rig: Rig, scenario: FloorScenario
) -> None:
    scenario.setup(rig)

    report = rig.reconcile()

    (item,) = report.unresolved
    assert scenario.reason_fragment in item.reason  # the scenario really reaches this site
    assert not report.permits_new_opening_order
    if scenario.basis is None:
        assert item.fill_floor_hold is None
        assert report.fill_floor_holds == ()
        return
    hold = item.fill_floor_hold
    assert isinstance(hold, FillFloorHold)
    assert hold.basis is FillFloorBasis(scenario.basis)
    assert (_plain(hold.floor), _plain(hold.venue)) == (scenario.floor, scenario.venue)
    assert report.fill_floor_holds == (item,)
    assert hold.as_dict() == {
        "basis": scenario.basis,
        "floor_figure": scenario.floor,
        "venue_figure": scenario.venue,
    }


def test_the_sites_between_them_use_every_basis() -> None:
    assert {s.basis for s in HOLD_SCENARIOS} == {b.value for b in FillFloorBasis}


def test_a_clean_intent_beside_a_hold_does_not_get_the_tag(rig: Rig) -> None:
    from tests.support.fill_floor_scenarios import planner_witnessed_terminal

    planner_witnessed_terminal(rig)
    other_ref = "CHR-ORD-" + "O" * 32
    rig.seed(intent_id="intent-2", ref=other_ref, permanent_id=PERM_ID + 1, broker_id=BROKER_ID + 1)
    rig.venue.open.append(
        venue_order(L.CANCELLED, "0", ref=other_ref, perm=PERM_ID + 1, broker_id=BROKER_ID + 1)
    )

    report = rig.reconcile()

    assert [item.intent_id for item in report.unresolved] == ["intent-1"]
    assert [item.intent_id for item in report.fill_floor_holds] == ["intent-1"]
    assert rig.status("intent-2") is L.CANCELLED
    assert all(item.fill_floor_hold is None for item in report.proven)


def test_the_tag_goes_exactly_when_the_venue_reports_the_recorded_figure(rig: Rig) -> None:
    from tests.support.fill_floor_scenarios import planner_witnessed_terminal

    planner_witnessed_terminal(rig)  # recorded 7; the venue ends at 5

    for figure in ("5", "6"):
        rig.venue.open = [venue_order(L.CANCELLED, figure)]
        report = rig.reconcile()
        (hold,) = (item.fill_floor_hold for item in report.fill_floor_holds)
        assert hold is not None
        assert (_plain(hold.floor), _plain(hold.venue)) == ("7", figure)
        assert rig.status() is L.CANCEL_PENDING

    rig.venue.open = [venue_order(L.CANCELLED, "7")]
    report = rig.reconcile()

    assert rig.status() is L.CANCELLED
    assert report.unresolved == () and report.fill_floor_holds == ()
    assert report.permits_new_opening_order
