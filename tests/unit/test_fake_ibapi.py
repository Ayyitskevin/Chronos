"""S1b: the FakeGateway that lets the REAL ``OfficialIBKRBroker`` run with no gateway.

These tests pin the fake itself (``tests/support/fake_ibapi.py``) and the session behaviour of
the unmodified adapter over it: the handshake, every read, previews, cancels, live fills, the
reader thread's lifetime, and that the stand-in can only ever be reached from a test (patched
loaders, a socket tripwire, ``ibapi`` never imported). Every IB behaviour the fake encodes is the
author's reading of the TWS API ("assumed, verify at K4"); a green run proves the adapter against
that model, not against IB. No case here submits a transmitting order through the adapter: that
path is S2's (Kevin's word).
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from tests.support import fake_ibapi
from tests.support.order_fakes import option_contract

import chronos.broker.official_ibkr as official_module
from chronos.broker.base import BrokerError
from chronos.broker.official_ibkr import OfficialIBKRBroker
from chronos.domain.enums import OrderIntent, OrderLifecycle, OrderSide
from chronos.domain.models import OrderRequest


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[fake_ibapi.FakeGateway]:
    seeded = fake_ibapi.FakeGateway.seeded()
    fake_ibapi.install(monkeypatch, seeded)
    yield seeded


class _Spy:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def __call__(self, reason: str) -> None:
        self.reasons.append(reason)


def _broker(spy: _Spy | None = None) -> OfficialIBKRBroker:
    return OfficialIBKRBroker(fake_ibapi.official_settings(), on_connection_uncertain=spy)


def _reader_alive() -> bool:
    return any(t.name == fake_ibapi.READER_THREAD and t.is_alive() for t in threading.enumerate())


def _preview_request(*, transmit: bool = False) -> OrderRequest:
    return OrderRequest(
        correlation_id="s1b-preview",
        account_id=fake_ibapi.ACCOUNT,
        contract=option_contract(),
        intent=OrderIntent.OPEN_SHORT_PUT,
        side=OrderSide.SELL,
        quantity=Decimal("1"),
        limit_price=Decimal("1.25"),
        order_ref="CHR-S1B-PREVIEW",
        transmit=transmit,
    )


# --- tests only: the injection seam, its negative control, no network ----------------------


def test_without_the_installer_the_adapter_cannot_reach_any_ibapi() -> None:
    """Negative control (reader P3-5a): no installer, no ibapi here, so construction refuses."""

    assert "ibapi" not in sys.modules
    with pytest.raises(BrokerError, match=r"ibapi|TWS API|install"):
        _broker()


def test_the_installer_patches_only_the_broker_loaders_and_restores_them() -> None:
    import chronos.histdata.official_client as histdata_client
    import chronos.histdata.official_options_client as options_client

    originals = (
        official_module._load_ibapi,
        official_module._load_order_class,
        histdata_client._load_ibapi,
        options_client._load_ibapi,
    )
    with pytest.MonkeyPatch.context() as patch:
        fake_ibapi.install(patch, fake_ibapi.FakeGateway.seeded())
        assert official_module._load_ibapi is not originals[0]
        assert official_module._load_order_class is not originals[1]
        assert histdata_client._load_ibapi is originals[2]  # reader P3-5b: untouched
        assert options_client._load_ibapi is originals[3]
        with pytest.raises(AssertionError, match="network"):
            socket.create_connection(("127.0.0.1", 7497))
    assert (
        official_module._load_ibapi,
        official_module._load_order_class,
        histdata_client._load_ibapi,
        options_client._load_ibapi,
    ) == originals
    assert "ibapi" not in sys.modules


# --- the session: handshake, reader thread, reads -------------------------------------------


async def test_the_adapter_connects_and_its_reader_thread_ends_on_disconnect(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    broker = _broker()
    await broker.connect()
    status = await broker.connection_status()
    assert (status.connected, status.account_id) == (True, fake_ibapi.ACCOUNT)
    assert status.managed_accounts == (fake_ibapi.ACCOUNT,)
    assert _reader_alive()
    await broker.disconnect()
    assert not _reader_alive()  # the named reader thread, not a global thread count (P2-1b)
    assert (await broker.connection_status()).connected is False


async def test_reads_return_the_simulated_venue_state(gateway: fake_ibapi.FakeGateway) -> None:
    broker = _broker()
    await broker.connect()
    try:
        summary = await broker.account_summary()
        positions = await broker.positions()
        executions = await broker.executions()
        orders = await broker.open_orders()
        server_time = await broker.server_time()
    finally:
        await broker.disconnect()
    # The seed: BUY 10 AAPL @100 half-filled at 99.90 (+1.00 commission); MANUAL-1 MSFT working.
    assert summary.total_cash == Decimal("100000") - Decimal("499.50") - Decimal("1.00")
    (position,) = positions
    assert (position.contract.symbol, position.quantity) == ("AAPL", Decimal("5"))
    assert position.average_cost == Decimal("100.1")  # (499.50 + 1.00) / 5, IB's per-unit basis
    (execution,) = executions
    assert (execution.price, execution.quantity, execution.commission) == (
        Decimal("99.9"),
        Decimal("5"),
        Decimal("1.00"),
    )
    assert {o.broker_order_id: (o.client_id, o.lifecycle) for o in orders} == {
        fake_ibapi.OWNED_ID: (17, OrderLifecycle.PARTIALLY_FILLED),
        fake_ibapi.FOREIGN_ID: (fake_ibapi.MANUAL_CLIENT_ID, OrderLifecycle.SUBMITTED),
    }
    assert server_time == gateway.sim_now().replace(microsecond=0)


async def test_executions_since_is_inclusive_at_second_resolution(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    broker = _broker()
    await broker.connect()
    try:
        (execution,) = await broker.executions()
        at = await broker.executions(since=execution.timestamp)
        after = await broker.executions(since=execution.timestamp + timedelta(seconds=1))
    finally:
        await broker.disconnect()
    assert [e.execution_id for e in at] == [execution.execution_id]
    assert after == ()


async def test_the_fakes_time_convention_is_what_the_adapter_parses_and_writes(
    gateway: fake_ibapi.FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fable r1 P2-1: the fake's execution times and filter bound, through the ADAPTER's code."""

    (sim_execution,) = gateway._run(gateway.sim.executions())
    rendered = gateway._execution_of(sim_execution).time
    assert official_module._parse_execution_time(rendered) == sim_execution.timestamp.replace(
        microsecond=0
    )

    seen: list[str] = []
    original = gateway.executions

    def capture(client: Any, req_id: int, execution_filter: Any) -> None:
        seen.append(execution_filter.time)
        original(client, req_id, execution_filter)

    monkeypatch.setattr(gateway, "executions", capture)
    since = sim_execution.timestamp.replace(microsecond=0)
    broker = _broker()
    await broker.connect()
    try:
        await broker.executions(since=since)
    finally:
        await broker.disconnect()
    (bound,) = seen
    parsed = datetime.strptime(bound, fake_ibapi.FILTER_TIME_FORMAT).replace(
        tzinfo=fake_ibapi.EXECUTION_TIME_ZONE
    )
    assert parsed == since


async def test_login_zone_execution_times_would_shift_the_adapters_instants(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    """Negative control for the convention, and the shape of candidate S1-F3 (a K4 check).

    If a real gateway renders tz-less times in the login zone (America/New_York, EDT in
    October), the adapter reads them as UTC (four hours early), and its UTC filter bound is read
    by the gateway in the login zone (four hours late), so ``since`` loses the fill."""

    gateway.execution_time_zone = ZoneInfo("America/New_York")
    (sim_execution,) = gateway._run(gateway.sim.executions())
    truth = sim_execution.timestamp.replace(microsecond=0)
    broker = _broker()
    await broker.connect()
    try:
        (read,) = await broker.executions()
        since_truth = await broker.executions(since=truth)
    finally:
        await broker.disconnect()
    assert truth - read.timestamp == timedelta(hours=4)
    assert since_truth == ()


# --- previews and cancels ---------------------------------------------------------------------


async def test_a_preview_is_priced_by_the_sim_and_changes_nothing(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    broker = _broker()
    await broker.connect()
    try:
        before = await broker.open_orders()
        preview = await broker.preview_order(_preview_request())
        after = await broker.open_orders()
    finally:
        await broker.disconnect()
    assert preview.accepted is True
    assert preview.estimated_commission == Decimal("1.00")  # 1 contract x 0.65 -> the 1.00 minimum
    assert preview.initial_margin_change is None  # IB's unset-double sentinel, cleaned (P3-4iii)
    assert after == before
    assert gateway.sim.sent_order_count == 2  # the two seeded orders only


async def test_cancelling_the_owned_order_cancels_it(gateway: fake_ibapi.FakeGateway) -> None:
    """The fake reports PendingCancel then Cancelled; the adapter's first ack wins (fail-closed).

    Also the positive control for the wire record the S1-F1 mechanism pin relies on."""

    broker = _broker()
    await broker.connect()
    try:
        result = await broker.cancel_order(fake_ibapi.OWNED_ID)
        orders = await broker.open_orders()
    finally:
        await broker.disconnect()
    assert (result.requested, result.lifecycle) == (True, OrderLifecycle.CANCEL_PENDING)
    assert fake_ibapi.OWNED_ID not in {o.broker_order_id for o in orders}
    assert gateway.cancel_requests == [(fake_ibapi.CLIENT_ID, fake_ibapi.OWNED_ID)]


async def test_another_clients_order_is_not_cancelled_by_the_gateway(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    """The fake's venue refuses a cancel of another client's order (10147) and keeps it.

    S1-F1 is fixed: the adapter proves ownership first and refuses before sending, so the
    gateway is not even asked (its send is pinned by the next case, not by this one)."""

    broker = _broker()
    await broker.connect()
    try:
        result = await broker.cancel_order(fake_ibapi.FOREIGN_ID)
        orders = await broker.open_orders()
    finally:
        await broker.disconnect()
    assert fake_ibapi.FOREIGN_ID in {o.broker_order_id for o in orders}  # the venue kept it
    assert result.requested is False  # was (True, CANCEL_PENDING): R-3, the adapter claimed a send
    assert gateway.cancel_requests == []  # the fake's own record: no cancelOrder reached it


async def test_a_foreign_cancel_never_reaches_the_wire(gateway: fake_ibapi.FakeGateway) -> None:
    """S1-F1 (mechanism), lifted: the send itself is gone, not just the requested=True result.

    S1b r1 pinned it as a strict xfail (Daybreak r1 P2-1); the fix turns it into a pass."""

    broker = _broker()
    await broker.connect()
    try:
        await broker.cancel_order(fake_ibapi.FOREIGN_ID)
    finally:
        await broker.disconnect()
    assert gateway.cancel_requests == []


@pytest.mark.parametrize("style", ["plain", "json", "timestamped"])
async def test_every_error_arity_completes_the_order_flight(
    gateway: fake_ibapi.FakeGateway, style: str
) -> None:
    """The adapter accepts 3-arg, 4-arg (json) and timestamp-prefixed errors (P3-4i).

    Retargeted by S1-F1: a cancel of an unknown id no longer reaches the gateway (ownership is
    proven first), so the error callback is now elicited by a preview of a contract the venue has
    no definition for (200), which still goes through placeOrder and the order-ack flight.
    """

    gateway.error_style = style
    broker = _broker()
    await broker.connect()
    request = _preview_request().model_copy(update={"contract": option_contract(con_id=999_999)})
    try:
        started = time.monotonic()
        preview = await broker.preview_order(request)
    finally:
        await broker.disconnect()
    assert time.monotonic() - started < 5  # completed by the error, not by the 20 s timeout
    assert preview.accepted is False
    assert gateway.errors_sent[-1][1] == fake_ibapi.CODE_NO_SECURITY


# --- live fills: the dispatch barrier and the unsolicited classification -----------------------


async def test_a_live_fill_returns_only_after_its_last_callback_was_dispatched(
    gateway: fake_ibapi.FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reader P2-1a: an EMPTY queue is not a dispatched callback; the barrier must wait for both."""

    broker = _broker()
    await broker.connect()
    try:
        original = broker.bridge.on_commission_report

        def slow(report: Any) -> None:
            time.sleep(0.2)  # dispatched last; an empty-queue barrier would release before it
            original(report)

        monkeypatch.setattr(broker.bridge, "on_commission_report", slow)
        new_ids = gateway.fill_sim_order(fake_ibapi.OWNED_ID, Decimal("5"))
        (new_id,) = new_ids
        assert broker.bridge.commission_for(new_id)[0] == Decimal("1.00")  # already cached
    finally:
        await broker.disconnect()


async def test_a_fill_for_an_order_this_session_never_sent_invalidates_readiness(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    """Negative control (reader P3-3): the classifier is real here because a spy hook is wired."""

    spy = _Spy()
    broker = _broker(spy)
    await broker.connect()
    try:
        gateway.fill_sim_order(fake_ibapi.OWNED_ID, Decimal("5"))  # placed in a previous session
    finally:
        await broker.disconnect()
    assert any("unsolicited" in reason for reason in spy.reasons)


async def test_another_clients_fill_still_invalidates_after_a_refused_cancel(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    """The cancel marks the id own (reader P3-2), but the client-id check still catches client 0."""

    spy = _Spy()
    broker = _broker(spy)
    await broker.connect()
    try:
        await broker.cancel_order(fake_ibapi.FOREIGN_ID)
        spy.reasons.clear()
        gateway.fill_sim_order(fake_ibapi.FOREIGN_ID, Decimal("1"))
    finally:
        await broker.disconnect()
    assert any("unsolicited" in reason for reason in spy.reasons)


async def test_a_same_client_foreign_order_fill_still_invalidates_after_a_cancel(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    other = gateway.add_working_order(
        ref="OTHER-1", symbol="MSFT", quantity="1", limit="50.00", client_id=17
    )
    spy = _Spy()
    broker = _broker(spy)
    await broker.connect()
    try:
        await broker.cancel_order(other)  # SimBroker keeps non-CHR orders; the gateway refuses it
        spy.reasons.clear()
        gateway.fill_sim_order(other, Decimal("1"))
    finally:
        await broker.disconnect()
    assert any("unsolicited" in reason for reason in spy.reasons)


# --- the gateway itself -------------------------------------------------------------------------


def test_two_gateways_are_independent() -> None:
    first, second = fake_ibapi.FakeGateway.seeded(), fake_ibapi.FakeGateway.seeded()
    first.fill_sim_order(fake_ibapi.OWNED_ID, Decimal("5"))
    assert first.sim is not second.sim
    assert first.sim.order_lifecycle(fake_ibapi.OWNED_ID) is OrderLifecycle.FILLED
    assert second.sim.order_lifecycle(fake_ibapi.OWNED_ID) is OrderLifecycle.PARTIALLY_FILLED


async def test_two_clients_on_one_gateway_see_one_venue(gateway: fake_ibapi.FakeGateway) -> None:
    first, second = _broker(), _broker()
    await first.connect()
    await second.connect()
    try:
        await first.cancel_order(fake_ibapi.OWNED_ID)
        seen = {o.broker_order_id for o in await second.open_orders()}
    finally:
        await first.disconnect()
        await second.disconnect()
    assert fake_ibapi.OWNED_ID not in seen


def test_a_transmitting_place_order_reaches_the_sim_at_the_client_level() -> None:
    """The fake's own send path, driven directly (no adapter: adapter sends are S2)."""

    gateway = fake_ibapi.FakeGateway.seeded()
    recorder = fake_ibapi.RecordingWrapper()
    client = fake_ibapi.FakeEClient.bound_to(gateway)(recorder)
    client.connect("127.0.0.1", 7497, 17)
    reader = threading.Thread(target=client.run, daemon=True)
    reader.start()
    try:
        order = fake_ibapi.FakeOrder()
        order.action, order.orderType, order.totalQuantity = "SELL", "LMT", Decimal("1")
        order.lmtPrice, order.account, order.orderRef, order.transmit = (
            1.25,
            fake_ibapi.ACCOUNT,
            "CHR-X",
            True,
        )
        contract = fake_ibapi.FakeContract()
        contract.conId = option_contract().con_id
        client.placeOrder(1003, contract, order)
        client.wait_dispatched()
    finally:
        client.disconnect()
        reader.join(timeout=5)
    names = [name for name, _ in recorder.calls]
    assert names[names.index("openOrder") + 1] == "orderStatus"  # openOrder first, then status
    assert gateway.sim.sent_order_count == 3
