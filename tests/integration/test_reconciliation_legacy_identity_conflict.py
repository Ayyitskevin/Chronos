"""Full own executions cannot override contradictory snapshot identity."""

import pytest
from tests.support.settle_rig import CLIENT_ID, PRIOR, make_rig, venue_execution, venue_order

from chronos.domain.enums import OrderLifecycle as L


@pytest.mark.parametrize("client_id", [CLIENT_ID, CLIENT_ID + 1], ids=["own", "foreign"])
def test_conflicting_reference_holds_legacy_intent_despite_full_own_executions(
    client_id: int,
) -> None:
    rig = make_rig()
    try:
        rig.seed()
        before = rig.events()
        foreign_ref = "CHR-FOREIGN-SNAPSHOT"
        foreign_broker_id = 987654321
        rig.venue.open = [
            venue_order(
                L.SUBMITTED, "0", ref=foreign_ref, broker_id=foreign_broker_id, client=client_id
            )
        ]
        rig.venue.execs = [venue_execution("own-full-fill", "10", PRIOR)]

        report = rig.reconcile()

        assert rig.status() is L.SUBMITTED
        assert rig.events() == before
        assert report.applied_updates == () and report.proven == ()
        assert [item.intent_id for item in report.unresolved] == ["intent-1"]
        reason = report.unresolved[0].reason
        assert reason.startswith("identity conflict:")
        assert foreign_ref not in reason
        assert str(foreign_broker_id) not in reason
        assert str(client_id) not in reason
        assert not report.permits_new_opening_order
    finally:
        rig.close()


def test_uncontradicted_full_own_executions_still_settle() -> None:
    rig = make_rig()
    try:
        rig.seed()
        rig.venue.execs = [venue_execution("own-full-fill", "10", PRIOR)]

        report = rig.reconcile()

        assert rig.status() is L.FILLED
        assert len(report.applied_updates) == 1 and report.unresolved == ()
    finally:
        rig.close()
