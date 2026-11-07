"""FLOORHOLD: the blocker mapping, the run row and the operator reply.

The blocker is ADDED to the existing ones. Status, reason and every gate decision are computed
without it, so these tests compare the same report with and without the hold.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from tests.unit.test_runtime_reconciliation_readiness import (
    FIXED_NOW,
    _manual_review_portfolio,
    _restart_report,
    _runtime_for,
)

from chronos.domain.enums import OrderLifecycle, ReconciliationStatus
from chronos.orders.reconciliation_readiness import ReadinessBlocker
from chronos.orders.reconciliation_recovery import (
    FillFloorBasis,
    FillFloorHold,
    RemainingActiveIntent,
    RestartOrderObservation,
)


def _held(intent_id: str = "intent-held", *, with_hold: bool) -> RestartOrderObservation:
    return RestartOrderObservation(
        intent_id=intent_id,
        local_status=OrderLifecycle.CANCEL_PENDING,
        broker_status=None,
        transition_applied=False,
        reason="venue fill figure 5 is below the witnessed lower bound 7",
        fill_floor_hold=FillFloorHold(
            basis=FillFloorBasis.WITNESSED_LOWER_BOUND, floor=Decimal("7"), venue=Decimal("5")
        )
        if with_hold
        else None,
    )


def _publish(observation: RestartOrderObservation):
    restart = _restart_report(
        unresolved=(observation,),
        remaining_active=(
            RemainingActiveIntent(intent_id="intent-held", status=OrderLifecycle.CANCEL_PENDING),
        ),
    )
    return _runtime_for(restart, _manual_review_portfolio()).reconcile_submission_readiness(
        now=FIXED_NOW
    )


def test_the_blocker_is_added_to_the_restart_blockers_never_replacing_them() -> None:
    plain = _publish(_held(with_hold=False))
    typed = _publish(_held(with_hold=True))

    assert plain.readiness.blockers == {
        ReadinessBlocker.RESTART_SENT_ACTIVE,
        ReadinessBlocker.RESTART_BROKER_EVIDENCE_INCOMPLETE,
    }
    assert typed.readiness.blockers == plain.readiness.blockers | {ReadinessBlocker.FILL_FLOOR_HOLD}
    assert typed.readiness.status is plain.readiness.status is ReconciliationStatus.MANUAL_REVIEW
    assert typed.readiness.reason == plain.readiness.reason
    assert typed.restart.permits_new_opening_order is plain.restart.permits_new_opening_order


def test_a_hold_of_another_kind_does_not_get_the_blocker() -> None:
    assert (
        ReadinessBlocker.FILL_FLOOR_HOLD not in _publish(_held(with_hold=False)).readiness.blockers
    )


def test_the_blocker_vocabulary_is_pinned() -> None:
    assert issubclass(ReadinessBlocker, StrEnum)
    assert ReadinessBlocker("FILL_FLOOR_HOLD") is ReadinessBlocker.FILL_FLOOR_HOLD
    assert [member.value for member in ReadinessBlocker] == [
        "ALLOWLIST_BREACH",
        "LOCAL_EVIDENCE_UNRESOLVED",
        "HELD_POSITION_UNPROVEN",
        "EXECUTIONS_UNALLOCATED",
        "OPEN_ORDER_NOT_EXACT_PUT",
        "OWNERSHIP_ABSENT",
        "SCOPE_CHANGED",
        "PASS_INCOMPLETE",
        "OPEN_ORDERS",
        "RESTART_BROKER_EVIDENCE_INCOMPLETE",
        "RESTART_SENT_ACTIVE",
        "BLOCKERS_UNDETERMINED",
        "FILL_FLOOR_HOLD",
    ]
    assert [member.value for member in FillFloorBasis] == [
        "RECORDED_FILLS",
        "WITNESSED_LOWER_BOUND",
        "SNAPSHOT_EXECUTIONS",
        "ROW_BOUND_EXECUTIONS",
        "WRITE_FLOOR",
    ]


def test_the_run_row_names_the_hold_with_the_intent_and_both_figures_and_nothing_else() -> None:
    from chronos.runtime import _reconciliation_run_snapshot

    published = _publish(_held(with_hold=True))
    snapshot = _reconciliation_run_snapshot(
        trigger="operator",
        report=published,
        generation=published.readiness.generation,
        started_at=FIXED_NOW,
        account_fingerprint="fp",
    )
    entries = [d for d in snapshot.decisions if d.get("kind") == "restart_observation"]
    assert [d["intent_id"] for d in entries] == ["intent-held"]
    assert entries[0]["fill_floor_hold"] == {
        "basis": "WITNESSED_LOWER_BOUND",
        "floor_figure": "7",
        "venue_figure": "5",
    }

    plain = _publish(_held(with_hold=False))
    plain_snapshot = _reconciliation_run_snapshot(
        trigger="operator",
        report=plain,
        generation=plain.readiness.generation,
        started_at=FIXED_NOW,
        account_fingerprint="fp",
    )
    assert all("fill_floor_hold" not in d for d in plain_snapshot.decisions)
