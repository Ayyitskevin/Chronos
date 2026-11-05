from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from tests.support import fake_ibapi
from tests.support.settle_rig import NOW, PERM_ID, ScriptedVenue, issues, make_rig, venue_order

from chronos.broker.official_ibkr import OfficialIBKRBroker
from chronos.domain.enums import OrderLifecycle
from chronos.orders.tracker import OrderStatusUpdate

CP = OrderLifecycle.CANCEL_PENDING
CANCELLED = OrderLifecycle.CANCELLED


def test_identity_event_during_completed_read_cannot_settle() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.pending()
        rig.venue.completed = [venue_order(CANCELLED, "0")]

        def add_conflict() -> None:
            rig.venue.during_completed = None
            rig.broker_id = 9002
            rig.event(CP, at=NOW, filled="0", source="IDENTITY")

        rig.venue.during_completed = add_conflict
        report = rig.reconcile()

        assert rig.status() is CP, (rig.status(), rig.trail(), issues(report))
        assert report.unresolved
        assert any("identity history advanced" in reason for reason in issues(report))
    finally:
        rig.close()


@pytest.mark.parametrize("refine_on_read", [1, 2])
def test_tracker_identity_refinement_during_completed_read_cannot_settle(
    refine_on_read: int,
) -> None:
    """The settlement CAS must cover history; callback-consumer prechecks are irrelevant."""

    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.pending()
        rig.venue.completed = [venue_order(CANCELLED, "0")]
        reads = 0

        def refine_identity() -> None:
            nonlocal reads
            reads += 1
            if reads != refine_on_read:
                return
            outcome = rig.tracker.ingest(
                OrderStatusUpdate(
                    intent_id="intent-1",
                    broker_order_id=9002,
                    permanent_id=PERM_ID + 1,
                    client_id=17,
                    lifecycle=CP,
                    filled_quantity=Decimal("0"),
                    remaining_quantity=Decimal("10"),
                    occurred_at=NOW,
                ),
                current_account_id=rig.account,
            )
            assert outcome.identity_refined
            rig.venue.during_completed = None

        rig.venue.during_completed = refine_identity
        report = rig.reconcile()

        assert rig.status() is CP
        assert report.unresolved
        assert any("identity history advanced" in reason for reason in issues(report))
        assert [event.to_status for event in rig.events()].count(CANCELLED) == 0
    finally:
        rig.close()


def test_foreign_account_identifiers_are_not_reported() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.pending()
        rig.venue.open = [
            venue_order(
                OrderLifecycle.SUBMITTED,
                "0",
                account="DU7654321",
                ref="CHR-OTHER-SYNTHETIC",
                broker_id=987654321,
            )
        ]

        reason = " ".join(issues(rig.reconcile()))

        assert rig.status() is CP
        assert "987654321" not in reason
        assert "CHR-OTHER-SYNTHETIC" not in reason
        assert "DU7654321" not in reason
        assert "identity conflict" in reason
    finally:
        rig.close()


def test_broker_error_text_cannot_report_a_foreign_identifier() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.pending()
        rig.venue.completed_error = RuntimeError("synthetic foreign broker order 987654321")

        reason = " ".join(issues(rig.reconcile()))

        assert rig.status() is CP
        assert "987654321" not in reason
        assert "synthetic foreign broker order" not in reason
    finally:
        rig.close()


def test_completed_orders_send_error_releases_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = fake_ibapi.FakeGateway.seeded()
    fake_ibapi.install(monkeypatch, gateway)
    broker = OfficialIBKRBroker(fake_ibapi.official_settings())

    class App:
        def reqCompletedOrders(self, _api_only: bool) -> None:
            raise RuntimeError("synthetic send error")

    app = App()
    monkeypatch.setattr(broker, "_require_app", lambda: app)

    async def immediate_wait(*_args: object) -> bool:
        return True

    monkeypatch.setattr(asyncio.BaseEventLoop, "run_in_executor", immediate_wait)

    async def go() -> None:
        with pytest.raises(RuntimeError, match="synthetic send error"):
            await broker.completed_orders()
        monkeypatch.setattr(
            app, "reqCompletedOrders", lambda _api_only: broker.bridge.on_completed_orders_end()
        )
        assert await broker.completed_orders() == ()

    asyncio.run(go())


def test_completed_orders_timeout_requires_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """K-20261103-003: timeout refuses another read until a fresh connection session."""

    from chronos.broker import official_ibkr as implementation
    from chronos.broker.base import BrokerDataError, CompletedOrdersReconnectRequired

    gateway = fake_ibapi.FakeGateway.seeded()
    fake_ibapi.install(monkeypatch, gateway)
    monkeypatch.setattr(implementation, "_REQUEST_TIMEOUT_S", 0.05)
    broker = OfficialIBKRBroker(fake_ibapi.official_settings())

    class App:
        def reqCompletedOrders(self, _api_only: bool) -> None:
            return None

    app = App()
    monkeypatch.setattr(broker, "_require_app", lambda: app)
    waits = iter((False, True))

    async def immediate_wait(*_args: object) -> bool:
        return next(waits)

    monkeypatch.setattr(asyncio.BaseEventLoop, "run_in_executor", immediate_wait)

    async def go() -> None:
        with pytest.raises(CompletedOrdersReconnectRequired, match="reconnect"):
            await broker.completed_orders()
        with pytest.raises(BrokerDataError, match="reconnect"):
            await broker.completed_orders()
        broker.bridge.reset_for_reconnect()
        monkeypatch.setattr(
            app, "reqCompletedOrders", lambda _api_only: broker.bridge.on_completed_orders_end()
        )
        assert await broker.completed_orders() == ()

    asyncio.run(go())
