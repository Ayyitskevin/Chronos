"""CANCEL_PENDING settle, end to end over the FakeGateway: the REAL adapter and the REAL reconciler.

Nothing here reaches a network: ``fake_ibapi.install`` patches the adapter's loaders, forbids
sockets, and every ALLOW_* flag stays false. The seeded owned order is the gateway's own
(``CHR-CONTRACT-SEED``, AAPL BUY 10 @ 100, 5 filled, client 17); the persisted intent is seeded
to match it exactly, then the cancel goes through the adapter's real ``cancel_order``.

This is also where the two B1 cases that live only on ``claude/b1-ready`` are re-created on this
base (their mapping is recorded in RESULT.md): the two-step cancel (PendingCancel then Cancelled)
that used to leave CANCEL_PENDING forever, and the cancelled-with-a-partial-fill variant.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal

import pytest
from tests.support import fake_ibapi
from tests.support.settle_rig import NOW, Rig, issues, make_rig

from chronos.broker.official_ibkr import OfficialIBKRBroker
from chronos.domain.enums import OrderLifecycle

CP = OrderLifecycle.CANCEL_PENDING
CANCELLED = OrderLifecycle.CANCELLED

REF = "CHR-CONTRACT-SEED"
PERM = 900001001


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[fake_ibapi.FakeGateway]:
    seeded = fake_ibapi.FakeGateway.seeded()
    fake_ibapi.install(monkeypatch, seeded)
    yield seeded


@pytest.fixture
def world(gateway: fake_ibapi.FakeGateway) -> Iterator[tuple[Rig, OfficialIBKRBroker]]:
    adapter = OfficialIBKRBroker(
        fake_ibapi.official_settings(), order_ownership=fake_ibapi.InstalledGatewayLedger()
    )
    rig = make_rig(adapter, account=fake_ibapi.ACCOUNT)
    rig.connection.run(adapter.connect())
    rig.seed(
        ref=REF,
        quantity=Decimal("10"),
        permanent_id=PERM,
        client_id=fake_ibapi.CLIENT_ID,
        broker_id=fake_ibapi.OWNED_ID,
        symbol="AAPL",
        con_id=999,
        limit=Decimal("100.0"),
    )
    try:
        yield rig, adapter
    finally:
        rig.connection.run(adapter.disconnect())
        rig.close()


def _cancel(rig: Rig, adapter: OfficialIBKRBroker) -> None:
    rig.pending()  # the cancel service's mark, then the real cancel on the wire
    result = rig.connection.run(adapter.cancel_order(fake_ibapi.OWNED_ID))
    assert result.requested is True


def _at(rig: Rig, when: datetime = NOW):  # type: ignore[no-untyped-def]
    return rig.reconcile(when)


# --- the adapter read ------------------------------------------------------------------------


async def test_a_cancelled_order_comes_back_as_a_completed_record(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    adapter = OfficialIBKRBroker(
        fake_ibapi.official_settings(), order_ownership=fake_ibapi.InstalledGatewayLedger()
    )
    await adapter.connect()
    try:
        assert await adapter.completed_orders() == ()  # nothing has completed yet
        await adapter.cancel_order(fake_ibapi.OWNED_ID)
        (record,) = await adapter.completed_orders()
    finally:
        await adapter.disconnect()
    assert record.lifecycle is CANCELLED
    assert (record.order_ref, record.permanent_id, record.client_id) == (REF, PERM, 17)
    assert (record.account_id, record.side.value) == (fake_ibapi.ACCOUNT, "BUY")
    assert (record.quantity, record.filled_quantity, record.limit_price) == (
        Decimal("10"),
        Decimal("5"),
        Decimal("100.0"),
    )
    assert record.broker_order_id == 0  # IB may omit the order id on a completed row


async def test_a_completed_row_that_carries_its_order_id_keeps_it(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    gateway.completed_order_ids = True
    adapter = OfficialIBKRBroker(
        fake_ibapi.official_settings(), order_ownership=fake_ibapi.InstalledGatewayLedger()
    )
    await adapter.connect()
    try:
        await adapter.cancel_order(fake_ibapi.OWNED_ID)
        (record,) = await adapter.completed_orders()
    finally:
        await adapter.disconnect()
    assert record.broker_order_id == fake_ibapi.OWNED_ID


async def test_an_order_that_vanished_without_a_record_is_absent_from_completed_orders(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    adapter = OfficialIBKRBroker(
        fake_ibapi.official_settings(), order_ownership=fake_ibapi.InstalledGatewayLedger()
    )
    await adapter.connect()
    try:
        gateway.vanish_without_record(fake_ibapi.OWNED_ID)
        assert fake_ibapi.OWNED_ID not in {o.broker_order_id for o in await adapter.open_orders()}
        assert await adapter.completed_orders() == ()
    finally:
        await adapter.disconnect()


async def test_the_request_is_api_only_and_does_not_return_manual_orders(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    adapter = OfficialIBKRBroker(
        fake_ibapi.official_settings(), order_ownership=fake_ibapi.InstalledGatewayLedger()
    )
    await adapter.connect()
    try:
        gateway._run(gateway.sim.cancel_order(fake_ibapi.FOREIGN_ID))  # a manual order ends
        assert await adapter.completed_orders() == ()
    finally:
        await adapter.disconnect()
    assert gateway.completed_requests == 1


async def test_an_unreadable_completed_row_is_dropped_with_a_notice(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    """A figure above the order size is incoherent: no record, never a settle on it."""

    adapter = OfficialIBKRBroker(
        fake_ibapi.official_settings(), order_ownership=fake_ibapi.InstalledGatewayLedger()
    )
    await adapter.connect()
    try:
        row = fake_ibapi.FakeOrder()
        row.orderType, row.lmtPrice, row.action, row.orderRef = "LMT", 100.0, "BUY", REF
        row.totalQuantity, row.filledQuantity = Decimal("10"), Decimal("11")
        assert adapter._completed_record(object(), row, object()) is None
        assert any("[completed-order]" in note for note in adapter.bridge.notices)
    finally:
        await adapter.disconnect()


# --- B1R-F4 and the settle, end to end -------------------------------------------------------


def test_a_two_step_cancel_settles_after_reconciliation(
    world: tuple[Rig, OfficialIBKRBroker],
) -> None:
    """PendingCancel then Cancelled left the intent CANCEL_PENDING forever (B1R-F4)."""

    rig, adapter = world
    _cancel(rig, adapter)
    report = _at(rig)
    assert rig.status() is CANCELLED, issues(report)
    last = rig.events()[-1]
    assert last.source == "RECONCILE" and last.filled_quantity == Decimal("5")
    assert last.remaining_quantity == Decimal("5")
    assert report.permits_new_opening_order and report.remaining_active == ()


def test_the_cancelled_with_a_partial_fill_case_settles_at_the_venue_figure(
    world: tuple[Rig, OfficialIBKRBroker],
) -> None:
    rig, adapter = world
    _cancel(rig, adapter)
    rig.reconcile()
    assert rig.events()[-1].evidence["unwitnessed_upper_bound"] == "0"
    assert [t for t in rig.trail() if t[1] is CANCELLED] == [("RECONCILE", CANCELLED)]


def test_an_order_that_vanished_without_a_record_stays_cancel_pending(
    world: tuple[Rig, OfficialIBKRBroker], gateway: fake_ibapi.FakeGateway
) -> None:
    """Absence alone never settles: the gateway dropped the order and kept no record."""

    rig, _ = world
    rig.pending()
    gateway.vanish_without_record(fake_ibapi.OWNED_ID)
    report = _at(rig)
    assert rig.status() is CP
    assert any("no terminal record" in reason for reason in issues(report))
    assert not report.permits_new_opening_order


def test_a_second_pass_after_the_settle_changes_nothing(
    world: tuple[Rig, OfficialIBKRBroker],
) -> None:
    rig, adapter = world
    _cancel(rig, adapter)
    rig.reconcile()
    before = rig.events()
    rig.reconcile()
    assert rig.events() == before


# --- Daybreak P3-1: an unanswered completed-orders read must not poison later passes ----------


def test_P3_1_a_timed_out_completed_orders_read_is_a_typed_hold_until_reconnect(
    world: tuple[Rig, OfficialIBKRBroker],
    gateway: fake_ibapi.FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stream has no request id, so the unanswered flight is never overlapped: every later
    pass on the same connection holds with the typed reason (it used to raise RuntimeError out
    of the whole pass); a reconnect clears it and the next pass settles."""

    from chronos.broker import official_ibkr as official_ibkr_module

    rig, adapter = world
    monkeypatch.setattr(official_ibkr_module, "_REQUEST_TIMEOUT_S", 0.5)
    _cancel(rig, adapter)
    answer = gateway.completed_orders
    monkeypatch.setattr(gateway, "completed_orders", lambda client, api_only: None)  # silent
    for _ in range(2):
        report = _at(rig)
        assert rig.status() is CP
        assert any("completed-orders evidence is not available" in r for r in issues(report))
    monkeypatch.setattr(gateway, "completed_orders", answer)
    report = _at(rig)  # same connection: the old flight is still outstanding
    assert rig.status() is CP
    assert any("completed-orders evidence is not available" in r for r in issues(report))
    rig.connection.run(adapter.disconnect())
    rig.connection.run(adapter.connect())
    report = _at(rig)
    assert rig.status() is CANCELLED, issues(report)
