"""A terminal snapshot row cannot erase executions from that same snapshot."""

from collections.abc import Iterator
from decimal import Decimal

import pytest
from tests.support.settle_rig import (
    EARLIER_TODAY,
    PRIOR,
    Rig,
    make_rig,
    venue_execution,
    venue_order,
)

from chronos.domain.enums import OrderLifecycle as L
from chronos.persistence.execution_repository import ExecutionRepository


@pytest.fixture
def rig() -> Iterator[Rig]:
    instance = make_rig()
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("status", [L.SUBMITTED, L.SUBMISSION_UNKNOWN])
def test_terminal_row_cannot_drop_executions_witnessed_in_its_snapshot(rig: Rig, status: L) -> None:
    rig.seed()
    if status is L.SUBMISSION_UNKNOWN:
        rig.event(status, at=PRIOR)
    before = rig.events()
    rig.venue.open = [venue_order(L.CANCELLED, "0")]
    rig.venue.execs = [
        venue_execution("earlier-fill", "3", PRIOR),
        venue_execution("today-fill", "2", EARLIER_TODAY),
    ]

    report = rig.reconcile()

    assert rig.status() is status
    assert rig.events() == before
    assert report.applied_updates == () and report.proven == ()
    assert [item.intent_id for item in report.unresolved] == ["intent-1"]
    assert report.unresolved[0].reason.startswith("snapshot fill conflict:")
    assert "5" in report.unresolved[0].reason
    assert not report.permits_new_opening_order


@pytest.mark.parametrize(
    ("lifecycle", "figure", "snapshot_fill", "permanent_id"),
    [
        (L.CANCELLED, "0", None, 109001),
        (L.FILLED, "10", "10", 109001),
        (L.CANCELLED, "0", None, None),
    ],
    ids=["clean-terminal", "equal-figure", "one-id-intent"],
)
def test_coherent_terminal_evidence_still_settles(
    rig: Rig, lifecycle: L, figure: str, snapshot_fill: str | None, permanent_id: int | None
) -> None:
    rig.seed(permanent_id=permanent_id)
    rig.venue.open = [venue_order(lifecycle, figure, perm=permanent_id)]
    if snapshot_fill is not None:
        rig.venue.execs = [venue_execution("equal-fill", snapshot_fill, PRIOR)]

    report = rig.reconcile()

    assert rig.status() is lifecycle
    assert rig.events()[-1].filled_quantity == Decimal(figure)
    assert len(report.applied_updates) == 1 and report.unresolved == ()


def test_record_only_fills_store_does_not_become_a_reconciliation_witness(rig: Rig) -> None:
    rig.seed()
    assert ExecutionRepository(rig.db.sessions).record(
        venue_execution("record-only-fill", "5", PRIOR), correlation_id=None
    )
    rig.venue.open = [venue_order(L.CANCELLED, "0")]
    rig.venue.execs = []

    report = rig.reconcile()

    assert rig.status() is L.CANCELLED
    assert rig.events()[-1].filled_quantity == Decimal("0")
    assert len(report.applied_updates) == 1 and report.unresolved == ()
