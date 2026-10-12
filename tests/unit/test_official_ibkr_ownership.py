"""S1-F1 / S1-F2: the real ``OfficialIBKRBroker`` proves ownership before it cancels, and records
ownership only where an order is really submitted.

S1-F1 (Daybreak P1): ``cancel_order`` used to send ``cancelOrder`` for any id and report
``requested=True``. It must now prove the order is Chronos-owned first (this process submitted
it, OR the venue's open-order book lists it with a ``CHR-`` ``orderRef`` and the configured
account) and otherwise refuse: ``requested=False``, no ``cancelOrder`` on the wire.

S1-F2 (Daybreak P1): ``start_order_ack`` used to note the id as own for submit, preview AND
cancel, so any id the process merely tried to cancel or preview was classified own forever. Only
a real submit, recorded immediately before ``placeOrder`` and after every pre-send guard, may.

Everything runs in process: the FakeGateway (``tests/support/fake_ibapi.py``, S1b) stands in for
the TWS client, and the submit cases use the same stub-app pattern as ``test_official_ibkr.py``.
No network, no ``ibapi``, no adapter-level transmit through the gateway (that path is S2's), and
every ``ALLOW_*`` setting is false. What the fake encodes about IB is the author's reading of the
API ("assumed, verify at K4"); a green run proves the adapter against that model.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from tests.support import fake_ibapi

import chronos.broker.official_ibkr as official_module
from chronos.broker.base import BrokerRefusedBeforeSend
from chronos.broker.callbacks import CallbackBridge
from chronos.broker.official_ibkr import OfficialIBKRBroker
from chronos.broker.order_ids import OrderIdAllocator
from chronos.broker.request_registry import RequestRegistry
from chronos.domain.enums import IBEnvironment, OrderIntent, OrderLifecycle, OrderSide
from chronos.domain.models import CryptoContract, OrderRequest


class _Spy:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def __call__(self, reason: str) -> None:
        self.reasons.append(reason)


class _Wire:
    """What actually reached the (fake) TWS client: cancels sent, order-book reads made."""

    def __init__(self) -> None:
        self.cancels: list[int] = []
        self.book_reads = 0


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[fake_ibapi.FakeGateway]:
    seeded = fake_ibapi.FakeGateway.seeded()
    fake_ibapi.install(monkeypatch, seeded)
    yield seeded


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> _Wire:
    """Record ``cancelOrder`` and ``reqAllOpenOrders`` at the fake client (no edit to the fake)."""

    seen = _Wire()
    original_cancel = fake_ibapi.FakeEClient.cancelOrder
    original_book = fake_ibapi.FakeEClient.reqAllOpenOrders

    def cancel(self: Any, orderId: int, manualCancelOrderTime: str = "") -> None:
        seen.cancels.append(int(orderId))
        original_cancel(self, orderId, manualCancelOrderTime)

    def book(self: Any) -> None:
        seen.book_reads += 1
        original_book(self)

    monkeypatch.setattr(fake_ibapi.FakeEClient, "cancelOrder", cancel)
    monkeypatch.setattr(fake_ibapi.FakeEClient, "reqAllOpenOrders", book)
    return seen


def _broker(spy: _Spy | None = None) -> OfficialIBKRBroker:
    return OfficialIBKRBroker(fake_ibapi.official_settings(), on_connection_uncertain=spy)


def _own_ids(broker: OfficialIBKRBroker) -> set[int]:
    return set(broker.bridge.unsolicited._own_order_ids)


def _preview_request() -> OrderRequest:
    from tests.support.order_fakes import option_contract

    return OrderRequest(
        correlation_id="s1f-preview",
        account_id=fake_ibapi.ACCOUNT,
        contract=option_contract(),
        intent=OrderIntent.OPEN_SHORT_PUT,
        side=OrderSide.SELL,
        quantity=Decimal("1"),
        limit_price=Decimal("1.25"),
        order_ref="CHR-S1F-PREVIEW",
        transmit=False,
    )


# --- S1-F1: cancel proves ownership before it sends -----------------------------------------


async def test_a_same_client_foreign_order_is_not_cancelled(
    gateway: fake_ibapi.FakeGateway, wire: _Wire
) -> None:
    """A previous session / another process under OUR client id: the venue would cancel it."""

    other = gateway.add_working_order(
        ref="OTHER-1", symbol="MSFT", quantity="1", limit="50.00", client_id=fake_ibapi.CLIENT_ID
    )
    broker = _broker()
    await broker.connect()
    try:
        result = await broker.cancel_order(other)
        book = await broker.open_orders()
    finally:
        await broker.disconnect()
    assert wire.cancels == [], "a cancelOrder reached the client for an order Chronos does not own"
    assert result.requested is False
    assert result.lifecycle is not OrderLifecycle.CANCELLED
    assert "not a Chronos order" in result.message
    assert other in {order.broker_order_id for order in book}  # the venue still has it
    assert gateway.sim.order_lifecycle(other) is not OrderLifecycle.CANCELLED


async def test_another_clients_manual_order_is_not_cancelled_either(
    gateway: fake_ibapi.FakeGateway, wire: _Wire
) -> None:
    broker = _broker()
    await broker.connect()
    try:
        result = await broker.cancel_order(fake_ibapi.FOREIGN_ID)
        book = await broker.open_orders()
    finally:
        await broker.disconnect()
    assert wire.cancels == []
    assert (result.requested, "not a Chronos order" in result.message) == (False, True)
    assert fake_ibapi.FOREIGN_ID in {order.broker_order_id for order in book}
    assert gateway.errors_sent == []  # the venue was never asked, so it never had to refuse


async def test_an_unknown_id_is_refused_without_a_send(
    gateway: fake_ibapi.FakeGateway, wire: _Wire
) -> None:
    broker = _broker()
    await broker.connect()
    try:
        result = await broker.cancel_order(424242)
    finally:
        await broker.disconnect()
    assert wire.cancels == []
    assert (result.requested, result.lifecycle) == (False, OrderLifecycle.REJECTED)
    assert "no such open order" in result.message
    assert gateway.errors_sent == []


async def test_a_chronos_order_this_process_never_submitted_is_still_cancelled_via_the_book(
    gateway: fake_ibapi.FakeGateway, wire: _Wire
) -> None:
    """Path B (a restart): the venue lists a CHR- order for our account, so it is ours.

    Green before and after the fix: it pins that the proof does not over-refuse.
    """

    broker = _broker()
    await broker.connect()
    try:
        result = await broker.cancel_order(fake_ibapi.OWNED_ID)
        book = await broker.open_orders()
    finally:
        await broker.disconnect()
    assert wire.cancels == [fake_ibapi.OWNED_ID]
    assert (result.requested, result.lifecycle) == (True, OrderLifecycle.CANCELLED)
    assert fake_ibapi.OWNED_ID not in {order.broker_order_id for order in book}


async def test_an_order_this_process_submitted_is_cancelled_without_reading_the_book(
    gateway: fake_ibapi.FakeGateway, wire: _Wire
) -> None:
    """Path A: the own-id set is the fast path. Green before and after (a mutant that always
    reads the book is caught by ``book_reads == 0``)."""

    broker = _broker()
    await broker.connect()
    try:
        broker.bridge.unsolicited.note_own_order(fake_ibapi.OWNED_ID)  # as a real submit would
        result = await broker.cancel_order(fake_ibapi.OWNED_ID)
    finally:
        await broker.disconnect()
    assert wire.book_reads == 0
    assert wire.cancels == [fake_ibapi.OWNED_ID]
    assert result.requested is True


async def test_if_the_venue_book_cannot_be_read_the_cancel_is_refused_before_send(
    gateway: fake_ibapi.FakeGateway, wire: _Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership unproven (no own-set entry, no book): fail closed, nothing sent."""

    monkeypatch.setattr(official_module, "_REQUEST_TIMEOUT_S", 0.2)
    monkeypatch.setattr(fake_ibapi.FakeEClient, "reqAllOpenOrders", lambda self: None)  # silent
    broker = _broker()
    await broker.connect()
    try:
        with pytest.raises(BrokerRefusedBeforeSend, match="prove ownership"):
            await broker.cancel_order(fake_ibapi.OWNED_ID)
    finally:
        await broker.disconnect()
    assert wire.cancels == []


async def test_a_chronos_looking_order_on_another_account_is_not_ours(
    gateway: fake_ibapi.FakeGateway, wire: _Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fake_ibapi.FakeGateway._order_of

    def other_account(self: Any, order: Any, adapter_id: int) -> Any:
        ib = original(self, order, adapter_id)
        ib.account = "DU0000000"
        return ib

    monkeypatch.setattr(fake_ibapi.FakeGateway, "_order_of", other_account)
    broker = _broker()
    await broker.connect()
    try:
        result = await broker.cancel_order(fake_ibapi.OWNED_ID)
    finally:
        await broker.disconnect()
    assert wire.cancels == []
    assert result.requested is False


async def test_a_refused_foreign_cancel_does_not_block_a_later_owned_cancel(
    gateway: fake_ibapi.FakeGateway, wire: _Wire
) -> None:
    """A venue-proven Chronos order is cancelled even when a foreign cancel was refused first."""

    broker = _broker()
    await broker.connect()
    try:
        refused = await broker.cancel_order(fake_ibapi.FOREIGN_ID)
        accepted = await broker.cancel_order(fake_ibapi.OWNED_ID)
    finally:
        await broker.disconnect()
    assert (refused.requested, accepted.requested) == (False, True)
    assert wire.cancels == [fake_ibapi.OWNED_ID]


# --- S1-F2: only a real submit records ownership ----------------------------------------------


async def test_a_refused_cancel_and_a_preview_register_no_ownership(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    other = gateway.add_working_order(
        ref="OTHER-1", symbol="MSFT", quantity="1", limit="50.00", client_id=fake_ibapi.CLIENT_ID
    )
    broker = _broker()
    await broker.connect()
    try:
        await broker.cancel_order(other)
        assert _own_ids(broker) == set()
        await broker.preview_order(_preview_request())
        assert _own_ids(broker) == set()
    finally:
        await broker.disconnect()


async def test_a_proven_cancel_of_a_previous_session_order_registers_no_ownership(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    broker = _broker()
    await broker.connect()
    try:
        await broker.cancel_order(fake_ibapi.OWNED_ID)
        assert _own_ids(broker) == set()
    finally:
        await broker.disconnect()


async def test_a_same_client_foreign_fill_invalidates_readiness_after_a_refused_cancel(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    """The S1-F2 scenario end to end (the S1b strict xfail's case, repeated here so this file
    stands on its own)."""

    other = gateway.add_working_order(
        ref="OTHER-1", symbol="MSFT", quantity="1", limit="50.00", client_id=fake_ibapi.CLIENT_ID
    )
    spy = _Spy()
    broker = _broker(spy)
    await broker.connect()
    try:
        await broker.cancel_order(other)
        spy.reasons.clear()
        gateway.fill_sim_order(other, Decimal("1"))
    finally:
        await broker.disconnect()
    assert any("unsolicited" in reason for reason in spy.reasons)


# --- S1-F2 (submit side), through the same stub-app pattern as test_official_ibkr.py -----------


class _FakeContract:
    conId = 0
    exchange = ""


class _FakeOrder:
    def __init__(self) -> None:
        self.action = ""
        self.orderType = ""
        self.totalQuantity: object = None
        self.lmtPrice = 0.0
        self.tif = ""
        self.outsideRth = False
        self.account = ""
        self.orderRef = ""
        self.whatIf = False
        self.transmit = False


class _AckingApp:
    """A stub client: connected, records what was own at the instant of ``placeOrder``, and acks."""

    def __init__(self, broker: OfficialIBKRBroker) -> None:
        self.broker = broker
        self.place_calls: list[int] = []
        self.own_at_send: list[bool] = []

    def isConnected(self) -> bool:
        return True

    def placeOrder(self, order_id: int, contract: object, order: object) -> None:
        del contract, order
        self.own_at_send.append(order_id in _own_ids(self.broker))
        self.place_calls.append(order_id)
        self.broker.bridge.on_order_status(order_id, "Submitted", 0.0, 1.0, 0.0, 1)


class _Guard:
    def __init__(self, authorized: bool) -> None:
        self._authorized = authorized

    @contextmanager
    def at_send(self) -> Iterator[bool]:
        yield self._authorized


def _stub_broker(monkeypatch: pytest.MonkeyPatch) -> tuple[OfficialIBKRBroker, _AckingApp, _Spy]:
    monkeypatch.setattr(official_module, "_load_ibapi", lambda: (None, None, _FakeContract, None))
    monkeypatch.setattr(official_module, "_load_order_class", lambda: _FakeOrder)
    spy = _Spy()
    broker = object.__new__(OfficialIBKRBroker)
    broker._settings = SimpleNamespace(  # type: ignore[assignment]
        ib_environment=IBEnvironment.PAPER,
        ib_port=7497,
        ib_client_id=17,
        ib_account_id="DU7654321",
        allow_outside_rth=False,
    )
    broker._live_kill_switch = None
    broker.bridge = CallbackBridge(RequestRegistry(), on_connection_uncertain=spy, client_id=17)
    broker.bridge.managed_accounts = ("DU7654321",)
    broker.order_ids = OrderIdAllocator()
    broker.order_ids.seed(9001)
    app = _AckingApp(broker)
    broker._app = app
    return broker, app, spy


def _submit_request() -> OrderRequest:
    ref = "CHR-ORD-" + "S" * 32
    return OrderRequest(
        correlation_id=ref,
        account_id="DU7654321",
        contract=CryptoContract(con_id=1, symbol="ETH"),
        intent=OrderIntent.OPEN_LONG_CRYPTO,
        side=OrderSide.BUY,
        quantity=Decimal("0.1"),
        limit_price=Decimal("3000"),
        order_ref=ref,
        transmit=True,
    )


def test_a_real_submit_records_ownership_before_placeorder_and_its_fills_are_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Green before and after: it pins the PLACEMENT of the record (before the send) so a fill
    cannot race it, and the positive control that a genuinely submitted order is not unsolicited."""

    broker, app, spy = _stub_broker(monkeypatch)
    asyncio.run(broker.submit_order(_submit_request(), send_guard=_Guard(True)))
    assert app.place_calls == [9001]
    assert app.own_at_send == [True], "ownership was not recorded before placeOrder"
    assert _own_ids(broker) == {9001}
    assert broker.bridge.unsolicited.observe_order_status(client_id=17, order_id=9001) is False
    assert spy.reasons == []


def test_a_submit_refused_by_the_send_guard_records_no_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, app, _spy = _stub_broker(monkeypatch)
    with pytest.raises(BrokerRefusedBeforeSend, match="authorization changed"):
        asyncio.run(broker.submit_order(_submit_request(), send_guard=_Guard(False)))
    assert app.place_calls == []
    assert _own_ids(broker) == set(), "an id that was never sent was recorded as own"
    # the id was allocated but nothing was placed: a status for it is unsolicited, not own
    assert broker.bridge.unsolicited.observe_order_status(client_id=17, order_id=9001) is True


def test_a_submit_refused_by_the_kill_switch_records_no_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, app, _spy = _stub_broker(monkeypatch)

    class _Engaged:
        def is_engaged(self) -> bool:
            return False

        @contextmanager
        def at_send(self) -> Iterator[bool]:
            yield False  # engaged between the build and the send

    broker._live_kill_switch = _Engaged()  # type: ignore[assignment]
    with pytest.raises(BrokerRefusedBeforeSend, match="kill switch engaged"):
        asyncio.run(broker.submit_order(_submit_request(), send_guard=_Guard(True)))
    assert app.place_calls == []
    assert _own_ids(broker) == set()
