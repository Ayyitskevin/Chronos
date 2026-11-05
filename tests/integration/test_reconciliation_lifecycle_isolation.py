"""A contradictory lifecycle holds its intent without aborting the whole pass."""

from decimal import Decimal

from tests.support.settle_rig import (
    BROKER_ID,
    EARLIER_TODAY,
    PERM_ID,
    PRIOR,
    make_rig,
    venue_order,
)

from chronos.domain.enums import OrderLifecycle as L


def test_stale_submitted_row_holds_partial_fill_but_other_intent_still_settles() -> None:
    rig = make_rig()
    try:
        rig.seed()
        rig.event(L.PARTIALLY_FILLED, at=PRIOR, filled="5")
        before = rig.events()
        other_ref = "CHR-ORD-" + "T" * 32
        rig.seed(
            intent_id="intent-2",
            ref=other_ref,
            broker_id=BROKER_ID + 1,
            permanent_id=PERM_ID + 1,
            at=EARLIER_TODAY,
        )
        rig.venue.open = [
            venue_order(L.SUBMITTED, "0"),
            venue_order(L.CANCELLED, "0", ref=other_ref, broker_id=BROKER_ID + 1, perm=PERM_ID + 1),
        ]

        report = rig.reconcile()

        assert rig.status() is L.PARTIALLY_FILLED
        assert rig.events() == before
        assert [item.intent_id for item in report.unresolved] == ["intent-1"]
        assert report.unresolved[0].reason.startswith("lifecycle conflict:")
        assert "PARTIALLY_FILLED -> SUBMITTED" in report.unresolved[0].reason
        assert rig.status("intent-2") is L.CANCELLED
        assert rig.events("intent-2")[-1].filled_quantity == Decimal("0")
        assert [item.intent_id for item in report.applied_updates] == ["intent-2"]
        assert [item.intent_id for item in report.proven] == ["intent-2"]
        assert [item.intent_id for item in report.remaining_active] == ["intent-1"]
    finally:
        rig.close()
