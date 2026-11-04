"""Fill progress must survive same-status callbacks and fence stale cancel settlement."""

from decimal import Decimal

import pytest
from tests.support.settle_rig import NOW, PERM_ID, ScriptedVenue, make_rig, venue_order

from chronos.domain.enums import OrderLifecycle
from chronos.domain.models import CancellationResult
from chronos.orders.mutations import OrderCancellationService
from chronos.orders.tracker import OrderStatusUpdate
from chronos.persistence.order_repositories import RecordedFillsAdvanced


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

        rig.tracker.ingest(_pending_cancel_fill_update(), current_account_id=rig.account)

        # Assert the persisted behavior, not a newly added result field, so this pin
        # fails on the old implementation because the witnessed quantity is still zero.
        assert rig.status() is OrderLifecycle.CANCEL_PENDING
        assert rig.tracker.recorded_filled_quantity(
            "intent-1", current_account_id=rig.account
        ) == Decimal("1")
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
                rig.tracker.ingest(_pending_cancel_fill_update(), current_account_id=rig.account)
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


def test_lower_same_status_fill_is_ignored_without_appending_an_event() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.pending()
        higher = _pending_cancel_fill_update().model_copy(
            update={"filled_quantity": Decimal("2"), "remaining_quantity": Decimal("8")}
        )
        assert rig.tracker.ingest(higher, current_account_id=rig.account).fill_progress_recorded
        before = rig.events()

        lower = higher.model_copy(
            update={"filled_quantity": Decimal("1"), "remaining_quantity": Decimal("9")}
        )
        rig.tracker.ingest(lower, current_account_id=rig.account)

        assert rig.status() is OrderLifecycle.CANCEL_PENDING
        assert rig.tracker.recorded_filled_quantity(
            "intent-1", current_account_id=rig.account
        ) == Decimal("2")
        assert rig.events() == before
    finally:
        rig.close()


def test_two_level_same_status_fill_progress_uses_distinct_event_keys() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.pending()

        for filled, remaining in (("1", "9"), ("2", "8")):
            update = _pending_cancel_fill_update().model_copy(
                update={
                    "filled_quantity": Decimal(filled),
                    "remaining_quantity": Decimal(remaining),
                }
            )
            assert rig.tracker.ingest(update, current_account_id=rig.account).fill_progress_recorded

        assert rig.status() is OrderLifecycle.CANCEL_PENDING
        assert rig.tracker.recorded_filled_quantity(
            "intent-1", current_account_id=rig.account
        ) == Decimal("2")
        assert [event.filled_quantity for event in rig.events()[-2:]] == [
            Decimal("1"),
            Decimal("2"),
        ]
    finally:
        rig.close()


def test_settle_report_names_in_place_fill_progress() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.pending()
        rig.venue.open = [venue_order(OrderLifecycle.SUBMITTED, "1")]

        report = rig.reconcile()

        observed = next(item for item in report.proven if item.intent_id == "intent-1")
        assert observed.reason == "fill progress recorded in place"
        assert observed.transition_applied is False
        assert rig.status() is OrderLifecycle.CANCEL_PENDING
        assert rig.events()[-1].source == "FILL_PROGRESS"
        assert rig.events()[-1].filled_quantity == Decimal("1")
    finally:
        rig.close()


@pytest.mark.parametrize(
    "terminal_status",
    [OrderLifecycle.FILLED, OrderLifecycle.CANCELLED, OrderLifecycle.REJECTED],
)
def test_repository_refuses_below_floor_for_every_terminal_status(
    terminal_status: OrderLifecycle,
) -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.event(OrderLifecycle.PARTIALLY_FILLED, at=NOW, filled="3")
        before = rig.events()

        with pytest.raises(RecordedFillsAdvanced) as raised:
            rig.repo.record_transition(
                intent_id="intent-1",
                event_key=f"terminal-below-floor:{terminal_status.value}",
                source="TEST",
                from_status=OrderLifecycle.PARTIALLY_FILLED,
                to_status=terminal_status,
                current_account_id=rig.account,
                filled_quantity=Decimal("0"),
                occurred_at=NOW,
                enforce_from_status=True,
            )

        assert raised.value.recorded == Decimal("3")
        assert raised.value.attempted == Decimal("0")
        assert rig.status() is OrderLifecycle.PARTIALLY_FILLED
        assert rig.events() == before
    finally:
        rig.close()


@pytest.mark.parametrize("late_fill", [False, True], ids=["already-witnessed", "during-cancel"])
def test_cancel_service_surfaces_typed_floor_refusal_and_keeps_order_unresolved(
    late_fill: bool,
) -> None:
    venue = ScriptedVenue()
    rig = make_rig(venue)
    try:
        rig.seed()
        if not late_fill:
            fill = _pending_cancel_fill_update().model_copy(
                update={"lifecycle": OrderLifecycle.PARTIALLY_FILLED}
            )
            assert rig.tracker.ingest(fill, current_account_id=rig.account).lifecycle_changed

        async def cancel_order(broker_order_id: int) -> CancellationResult:
            if late_fill:
                assert rig.tracker.ingest(
                    _pending_cancel_fill_update(), current_account_id=rig.account
                ).fill_progress_recorded
            return CancellationResult(
                broker_order_id=broker_order_id,
                requested=True,
                lifecycle=OrderLifecycle.CANCELLED,
                message="synthetic cancel",
                timestamp=NOW,
            )

        venue.cancel_order = cancel_order
        service = OrderCancellationService(
            connection=rig.connection,
            intents=rig.intents,
            tracker=rig.tracker,
            tracker_repo=rig.repo,
        )

        with pytest.raises(RecordedFillsAdvanced):
            service.cancel("intent-1", current_account_id=rig.account, now=NOW)

        assert rig.status() is OrderLifecycle.CANCEL_PENDING
        assert rig.tracker.recorded_filled_quantity(
            "intent-1", current_account_id=rig.account
        ) == Decimal("1")
        assert not any(event.to_status is OrderLifecycle.CANCELLED for event in rig.events())
    finally:
        rig.close()
