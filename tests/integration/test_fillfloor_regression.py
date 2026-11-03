"""Fill progress must survive same-status callbacks and fence stale cancel settlement."""

from decimal import Decimal

from tests.support.settle_rig import NOW, PERM_ID, ScriptedVenue, make_rig, venue_order

from chronos.domain.enums import OrderLifecycle
from chronos.orders.tracker import OrderStatusUpdate


def _pending_cancel_fill_update() -> OrderStatusUpdate:
    return OrderStatusUpdate(
        intent_id="intent-1",
        broker_order_id=9001,
        permanent_id=PERM_ID,
        client_id=17,
        lifecycle=OrderLifecycle.CANCEL_PENDING,
        filled_quantity=Decimal("1"),
        remaining_quantity=Decimal("9"),
        occurred_at=NOW,
    )


def test_same_status_pending_cancel_fill_increase_is_persisted_and_idempotent() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.pending()

        outcome = rig.tracker.ingest(_pending_cancel_fill_update(), current_account_id=rig.account)

        assert outcome.lifecycle_changed is False
        assert outcome.identity_refined is False
        assert outcome.fill_progress_recorded is True
        assert rig.status() is OrderLifecycle.CANCEL_PENDING
        assert rig.events()[-1].filled_quantity == Decimal("1")
        assert rig.events()[-1].from_status is OrderLifecycle.CANCEL_PENDING
        assert rig.events()[-1].to_status is OrderLifecycle.CANCEL_PENDING

        duplicate = rig.tracker.ingest(
            _pending_cancel_fill_update(), current_account_id=rig.account
        )
        assert duplicate.recorded is False
        assert len(rig.events()) == 3
    finally:
        rig.close()


def test_fill_during_confirmation_refuses_stale_cancelled_zero_settlement() -> None:
    venue = ScriptedVenue()
    rig = make_rig(venue)
    try:
        rig.seed()
        rig.pending()
        venue.completed = [venue_order(OrderLifecycle.CANCELLED, "0")]
        fill_outcomes = []

        def report_fill_during_confirming_read() -> None:
            if venue.completed_calls != 2:
                return
            venue.during_completed = None
            fill_outcomes.append(
                rig.tracker.ingest(_pending_cancel_fill_update(), current_account_id=rig.account)
            )

        venue.during_completed = report_fill_during_confirming_read
        report = rig.reconcile()

        assert len(fill_outcomes) == 1
        assert fill_outcomes[0].fill_progress_recorded is True
        assert rig.status() is OrderLifecycle.CANCEL_PENDING
        assert rig.tracker.recorded_filled_quantity(
            "intent-1", current_account_id=rig.account
        ) == Decimal("1")
        assert not any(
            event.to_status is OrderLifecycle.CANCELLED and event.filled_quantity == 0
            for event in rig.events()
        )
        assert len(report.unresolved) == 1
        assert report.unresolved[0].intent_id == "intent-1"
        assert report.unresolved[0].reason.startswith("fill_floor_conflict: RecordedFillsAdvanced:")
        assert "recorded fills advanced" in report.unresolved[0].reason
    finally:
        rig.close()


def test_fill_between_replan_and_write_is_typed_and_keeps_cancel_pending() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.pending()
        rig.venue.completed = [venue_order(OrderLifecycle.CANCELLED, "0")]
        real_record_transition = rig.repo.record_transition
        injected = False

        def record_after_late_fill(**kwargs: object) -> bool:
            nonlocal injected
            if kwargs.get("to_status") is OrderLifecycle.CANCELLED and not injected:
                injected = True
                outcome = rig.tracker.ingest(
                    _pending_cancel_fill_update(), current_account_id=rig.account
                )
                assert outcome.fill_progress_recorded is True
            return real_record_transition(**kwargs)  # type: ignore[arg-type]

        rig.repo.record_transition = record_after_late_fill  # type: ignore[method-assign]
        report = rig.reconcile()

        assert injected is True
        assert rig.status() is OrderLifecycle.CANCEL_PENDING
        assert rig.tracker.recorded_filled_quantity(
            "intent-1", current_account_id=rig.account
        ) == Decimal("1")
        assert not any(event.to_status is OrderLifecycle.CANCELLED for event in rig.events())
        assert len(report.unresolved) == 1
        assert report.unresolved[0].reason.startswith("fill_floor_conflict: RecordedFillsAdvanced:")
        assert "recorded fills advanced" in report.unresolved[0].reason
    finally:
        rig.close()
