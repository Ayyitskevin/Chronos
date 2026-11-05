"""Fill progress must survive same-status callbacks and fence stale cancel settlement."""

import logging
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.integration import test_sim_order_lifecycle as sim
from tests.support.settle_rig import NOW, PERM_ID, ScriptedVenue, make_rig, venue_order

from chronos.api.auth import require_token
from chronos.api.dependencies import require_writer
from chronos.api.routes import orders as orders_route
from chronos.domain.enums import OrderLifecycle, ReconciliationStatus
from chronos.domain.models import CancellationResult
from chronos.orders.mutations import OrderCancellationService, OrderMutationError
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


def test_nonterminal_pending_cancel_observation_does_not_delay_terminal_settlement() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        partial = _pending_cancel_fill_update().model_copy(
            update={
                "lifecycle": OrderLifecycle.PARTIALLY_FILLED,
                "filled_quantity": Decimal("5"),
                "remaining_quantity": Decimal("5"),
            }
        )
        assert rig.tracker.ingest(partial, current_account_id=rig.account).lifecycle_changed
        rig.venue.open = [venue_order(OrderLifecycle.CANCEL_PENDING, "0")]

        first = rig.reconcile()

        assert rig.status() is OrderLifecycle.CANCEL_PENDING
        assert first.unresolved == ()
        rig.venue.open = []
        rig.venue.completed = [venue_order(OrderLifecycle.CANCELLED, "5")]

        second = rig.reconcile()

        assert rig.status() is OrderLifecycle.CANCELLED
        assert rig.events()[-1].filled_quantity == Decimal("5")
        assert second.unresolved == ()
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


def test_first_terminal_figure_beyond_column_scale_is_not_refused_against_itself() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()

        outcome = rig.tracker.ingest(
            _pending_cancel_fill_update().model_copy(
                update={
                    "lifecycle": OrderLifecycle.CANCELLED,
                    "filled_quantity": Decimal("0.123456789"),
                }
            ),
            current_account_id=rig.account,
        )

        assert outcome.lifecycle_changed
        assert rig.status() is OrderLifecycle.CANCELLED
        assert rig.events()[-1].filled_quantity == Decimal("0.12345679")
    finally:
        rig.close()


def test_repeated_terminal_figure_beyond_column_scale_matches_the_stored_floor() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.event(OrderLifecycle.PARTIALLY_FILLED, at=NOW, filled="0.123456789")
        assert rig.events()[-1].filled_quantity == Decimal("0.12345679")

        outcome = rig.tracker.ingest(
            _pending_cancel_fill_update().model_copy(
                update={
                    "lifecycle": OrderLifecycle.CANCELLED,
                    "filled_quantity": Decimal("0.123456789"),
                }
            ),
            current_account_id=rig.account,
        )

        assert outcome.lifecycle_changed
        assert rig.status() is OrderLifecycle.CANCELLED
        assert rig.events()[-1].filled_quantity == Decimal("0.12345679")
    finally:
        rig.close()


def test_settle_planner_accepts_the_same_over_scale_fill_it_witnessed() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        partial = _pending_cancel_fill_update().model_copy(
            update={
                "lifecycle": OrderLifecycle.PARTIALLY_FILLED,
                "filled_quantity": Decimal("0.123456789"),
                "remaining_quantity": Decimal("9.876543211"),
            }
        )
        assert rig.tracker.ingest(partial, current_account_id=rig.account).lifecycle_changed
        rig.pending()
        rig.venue.completed = [venue_order(OrderLifecycle.CANCELLED, "0.123456789")]

        report = rig.reconcile()

        assert report.unresolved == (), report.unresolved
        assert rig.status() is OrderLifecycle.CANCELLED
        assert rig.events()[-1].filled_quantity == Decimal("0.12345679")
    finally:
        rig.close()


def test_genuinely_lower_over_scale_terminal_figure_is_still_refused() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.event(OrderLifecycle.PARTIALLY_FILLED, at=NOW, filled="0.12345679")
        before = rig.events()

        with pytest.raises(RecordedFillsAdvanced) as raised:
            rig.tracker.ingest(
                _pending_cancel_fill_update().model_copy(
                    update={
                        "lifecycle": OrderLifecycle.CANCELLED,
                        "filled_quantity": Decimal("0.123456781"),
                    }
                ),
                current_account_id=rig.account,
            )

        assert raised.value.recorded == Decimal("0.12345679")
        assert raised.value.attempted == Decimal("0.123456781")
        assert rig.status() is OrderLifecycle.PARTIALLY_FILLED
        assert rig.events() == before
    finally:
        rig.close()


def test_at_or_above_over_scale_terminal_figure_is_the_precision_control() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.event(OrderLifecycle.PARTIALLY_FILLED, at=NOW, filled="0.12345678")

        outcome = rig.tracker.ingest(
            _pending_cancel_fill_update().model_copy(
                update={
                    "lifecycle": OrderLifecycle.CANCELLED,
                    "filled_quantity": Decimal("0.123456789"),
                }
            ),
            current_account_id=rig.account,
        )

        assert outcome.lifecycle_changed
        assert rig.status() is OrderLifecycle.CANCELLED
        assert rig.events()[-1].filled_quantity == Decimal("0.12345679")
    finally:
        rig.close()


def test_figureless_terminal_write_carries_the_recorded_fill_floor() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.event(OrderLifecycle.PARTIALLY_FILLED, at=NOW, filled="3")

        assert rig.repo.record_transition(
            intent_id="intent-1",
            event_key="terminal-figureless-cancelled",
            source="CANCEL",
            from_status=OrderLifecycle.PARTIALLY_FILLED,
            to_status=OrderLifecycle.CANCELLED,
            current_account_id=rig.account,
            occurred_at=NOW,
            enforce_from_status=True,
        )

        terminal_event = rig.events()[-1]
        assert terminal_event.to_status is OrderLifecycle.CANCELLED
        assert terminal_event.filled_quantity == Decimal("3")
        assert rig.tracker.recorded_filled_quantity(
            "intent-1", current_account_id=rig.account
        ) == Decimal("3")
    finally:
        rig.close()


def test_planned_nonterminal_transition_keeps_its_explicit_fill_floor() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.pending()
        witnessed = _pending_cancel_fill_update().model_copy(
            update={"filled_quantity": Decimal("3"), "remaining_quantity": Decimal("7")}
        )
        rig.tracker.ingest(witnessed, current_account_id=rig.account)
        before = rig.events()

        with pytest.raises(RecordedFillsAdvanced):
            rig.tracker.record_planned_transition(
                intent_id="intent-1",
                current_account_id=rig.account,
                from_status=OrderLifecycle.CANCEL_PENDING,
                to_status=OrderLifecycle.PARTIALLY_FILLED,
                event_key="planned:PARTIALLY_FILLED:2",
                source="RECONCILE",
                broker_order_id=9001,
                permanent_id=PERM_ID,
                client_id=17,
                filled_quantity=Decimal("2"),
                remaining_quantity=Decimal("8"),
                evidence={"venue_fill_figure": "2"},
                now=NOW,
            )

        assert rig.status() is OrderLifecycle.CANCEL_PENDING
        assert rig.events() == before
        assert rig.tracker.recorded_filled_quantity(
            "intent-1", current_account_id=rig.account
        ) == Decimal("3")
    finally:
        rig.close()


def test_ingest_accepts_equal_same_status_identity_evidence() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.pending()
        higher = _pending_cancel_fill_update().model_copy(
            update={"filled_quantity": Decimal("3"), "remaining_quantity": Decimal("7")}
        )
        assert rig.tracker.ingest(higher, current_account_id=rig.account).fill_progress_recorded
        equal_identity = higher.model_copy(update={"permanent_id": PERM_ID + 1})

        outcome = rig.tracker.ingest(equal_identity, current_account_id=rig.account)

        assert outcome.identity_refined
        assert rig.status() is OrderLifecycle.CANCEL_PENDING
        assert rig.events()[-1].filled_quantity == Decimal("3")
    finally:
        rig.close()


@pytest.mark.parametrize("late_fill", [False, True], ids=["already-witnessed", "during-cancel"])
def test_cancel_service_carries_figureless_terminal_fill_forward(
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
            reconciliation_readiness=rig.readiness,
        )

        result = service.cancel("intent-1", current_account_id=rig.account, now=NOW)

        assert result.lifecycle is OrderLifecycle.CANCELLED
        assert rig.status() is OrderLifecycle.CANCELLED
        assert rig.tracker.recorded_filled_quantity(
            "intent-1", current_account_id=rig.account
        ) == Decimal("1")
        terminal_event = rig.events()[-1]
        assert terminal_event.to_status is OrderLifecycle.CANCELLED
        assert terminal_event.filled_quantity == Decimal("1")
    finally:
        rig.close()


@pytest.mark.parametrize("ack_fill", [0, 5], ids=["lower-explicit", "equal-control"])
def test_official_cancel_ack_keeps_explicit_fill_semantics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ack_fill: int
) -> None:
    venue = sim._Venue("official_fake", monkeypatch, tmp_path)
    with sim.harness(venue, plans={sim._ref("A"): sim.FillPlan(partial_fraction=(1, 2))}) as h:
        intent = h.buy(ref=sim._ref("A"), intent_id="intent-1")
        h.confirm(intent)
        assert h.submit(intent).submitted
        h.bar(**sim.CROSSING)
        h.service.reconcile_on_restart_report(now=h.now)
        assert h.status("intent-1") is OrderLifecycle.PARTIALLY_FILLED
        assert h.tracker.recorded_filled_quantity(
            "intent-1", current_account_id=h.account
        ) == Decimal("5")

        def cancel(client: sim.FakeEClient, adapter_id: int) -> None:
            # Keep the real adapter and ownership checks; control only the venue acknowledgement.
            working = h.gateway._working()[h.gateway._sim_of[adapter_id]]
            result = h.gateway._run(h.sim.cancel_order(working.broker_order_id))
            assert result.requested
            client.emit(
                "orderStatus",
                adapter_id,
                "Cancelled",
                float(ack_fill),
                0.0,
                0.0,
                working.permanent_id,
                0,
                0.0,
                int(client.client_id or 0),
                "",
                0.0,
            )

        monkeypatch.setattr(h.gateway, "cancel_order", cancel)
        if ack_fill < 5:
            with pytest.raises(OrderMutationError) as raised:
                h.service.cancel("intent-1", now=h.now)

            cause = raised.value.__cause__
            assert isinstance(cause, RecordedFillsAdvanced)
            assert cause.recorded == Decimal("5")
            assert cause.attempted == Decimal("0")
            assert h.status("intent-1") is OrderLifecycle.CANCEL_PENDING
            assert h.tracker.recorded_filled_quantity(
                "intent-1", current_account_id=h.account
            ) == Decimal("5")
            assert not any(
                event.to_status is OrderLifecycle.CANCELLED
                for event in h.tracker_repo.events("intent-1", current_account_id=h.account)
            )
        else:
            result = h.service.cancel("intent-1", now=h.now)

            assert result.filled_quantity == Decimal("5")
            assert h.status("intent-1") is OrderLifecycle.CANCELLED
            terminal_event = h.tracker_repo.events("intent-1", current_account_id=h.account)[-1]
            assert terminal_event.filled_quantity == Decimal("5")


@pytest.mark.parametrize(("ack_fill", "expected_fill"), [("1", "1"), ("2", "2")])
def test_cancel_service_accepts_equal_or_higher_explicit_fill(
    ack_fill: str, expected_fill: str
) -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        partial = _pending_cancel_fill_update().model_copy(
            update={"lifecycle": OrderLifecycle.PARTIALLY_FILLED}
        )
        assert rig.tracker.ingest(partial, current_account_id=rig.account).lifecycle_changed

        async def cancel_order(broker_order_id: int) -> CancellationResult:
            return CancellationResult(
                broker_order_id=broker_order_id,
                requested=True,
                lifecycle=OrderLifecycle.CANCELLED,
                message="explicit cumulative fill",
                timestamp=NOW,
                filled_quantity=Decimal(ack_fill),
            )

        rig.venue.cancel_order = cancel_order
        service = OrderCancellationService(
            connection=rig.connection,
            intents=rig.intents,
            tracker=rig.tracker,
            tracker_repo=rig.repo,
            reconciliation_readiness=rig.readiness,
        )

        result = service.cancel("intent-1", current_account_id=rig.account, now=NOW)

        assert result.filled_quantity == Decimal(expected_fill)
        assert rig.status() is OrderLifecycle.CANCELLED
        assert rig.events()[-1].filled_quantity == Decimal(expected_fill)
        assert rig.tracker.recorded_filled_quantity(
            "intent-1", current_account_id=rig.account
        ) == Decimal(expected_fill)
    finally:
        rig.close()


@pytest.mark.parametrize(
    ("ack_fill", "expected_status"),
    [("0", 409), ("5", 200)],
    ids=["lower-explicit", "equal-control"],
)
def test_cancel_ack_fill_floor_is_typed_through_the_operator_route(
    ack_fill: str, expected_status: int, caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.event(OrderLifecycle.PARTIALLY_FILLED, at=NOW, filled="5")
        readiness = rig.readiness.snapshot()
        assert rig.readiness.complete(
            expected_generation=readiness.generation,
            status=ReconciliationStatus.RECONCILED,
            reason="test positive readiness baseline",
            reconciled_at=NOW,
        )
        readiness_before_cancel = rig.readiness.snapshot()
        assert readiness_before_cancel.ready

        async def cancel_order(broker_order_id: int) -> CancellationResult:
            return CancellationResult(
                broker_order_id=broker_order_id,
                requested=True,
                lifecycle=OrderLifecycle.CANCELLED,
                message="synthetic explicit cancel acknowledgement",
                timestamp=NOW,
                filled_quantity=Decimal(ack_fill),
            )

        rig.venue.cancel_order = cancel_order
        service = OrderCancellationService(
            connection=rig.connection,
            intents=rig.intents,
            tracker=rig.tracker,
            tracker_repo=rig.repo,
            reconciliation_readiness=rig.readiness,
        )
        management = SimpleNamespace(
            cancel=lambda intent_id: service.cancel(
                intent_id, current_account_id=rig.account, now=NOW
            ),
            get=lambda intent_id: rig.intents.get(intent_id, current_account_id=rig.account),
        )
        state = SimpleNamespace(runtime=SimpleNamespace(order_management=management))
        app = FastAPI()
        app.include_router(orders_route.router)
        app.dependency_overrides[require_writer] = lambda: state
        app.dependency_overrides[require_token] = lambda: None

        with (
            caplog.at_level(logging.WARNING, logger="chronos.orders.mutations"),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post("/orders/intent-1/cancel")

        assert response.status_code == expected_status
        if ack_fill == "0":
            assert response.json()["detail"] == (
                "cancel acknowledgement reports 0 filled, below the 5 already recorded; "
                "the order remains unresolved; reconcile before acting"
            )
            warnings = [
                record.getMessage()
                for record in caplog.records
                if record.name == "chronos.orders.mutations" and record.levelno == logging.WARNING
            ]
            assert warnings == [
                "cancel acknowledgement fill floor conflict: attempted=0 recorded=5"
            ]
            assert "synthetic explicit cancel acknowledgement" not in warnings[0]
            assert rig.status() is OrderLifecycle.CANCEL_PENDING
            readiness_after_cancel = rig.readiness.snapshot()
            assert readiness_after_cancel.generation > readiness_before_cancel.generation
            assert readiness_after_cancel.status is ReconciliationStatus.PENDING
            assert rig.tracker.recorded_filled_quantity(
                "intent-1", current_account_id=rig.account
            ) == Decimal("5")
        else:
            warnings = [
                record
                for record in caplog.records
                if record.name == "chronos.orders.mutations" and record.levelno == logging.WARNING
            ]
            assert warnings == []
            assert rig.status() is OrderLifecycle.CANCELLED
            assert rig.events()[-1].filled_quantity == Decimal("5")
            readiness_after_cancel = rig.readiness.snapshot()
            assert readiness_after_cancel.generation == readiness_before_cancel.generation
            assert readiness_after_cancel.ready
    finally:
        rig.close()
