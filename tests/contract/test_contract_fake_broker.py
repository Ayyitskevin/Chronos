"""XFIX-F r1: pin the contract fake's refusal fidelity to the real adapter."""

from __future__ import annotations

import pytest

from chronos.broker.base import BrokerRefusedBeforeSend
from chronos.domain.enums import ConnectionState, OrderLifecycle
from chronos.domain.models import CancellationResult
from tests.contract.conftest import (
    FOREIGN_ACCOUNT,
    REGISTRY,
    UNKNOWN_ORDER_ID,
    BrokerHarness,
    CountingGuard,
    _build_fake,
    book,
    connected,
    order_request,
)


@pytest.fixture(params=["fake", "official_fake"])
def adapter_harness(request: pytest.FixtureRequest) -> BrokerHarness:
    """The adapter uses only the existing socket-blocked FakeGateway fixture."""
    return REGISTRY[request.param]


async def test_fresh_adapter_starts_disconnected(adapter_harness: BrokerHarness) -> None:
    broker = adapter_harness.build()
    status = await broker.connection_status()
    assert status.connected is False
    assert status.state is ConnectionState.DISCONNECTED


async def test_transmitting_preview_raises_before_send(adapter_harness: BrokerHarness) -> None:
    async with connected(adapter_harness) as broker:
        before = await book(broker)
        with pytest.raises(BrokerRefusedBeforeSend):
            await broker.preview_order(order_request(adapter_harness, transmit=True))
        assert await book(broker) == before


@pytest.mark.parametrize("operation", ["preview", "submit"])
async def test_disconnected_order_entry_refuses_before_guard_or_state_change(
    adapter_harness: BrokerHarness, operation: str
) -> None:
    guard = CountingGuard()
    async with connected(adapter_harness) as broker:
        before = await book(broker)
        await broker.disconnect()
        with pytest.raises(BrokerRefusedBeforeSend):
            if operation == "preview":
                await broker.preview_order(order_request(adapter_harness, transmit=False))
            else:
                await broker.submit_order(
                    order_request(adapter_harness, transmit=True), send_guard=guard
                )
        assert guard.entries == 0
        await broker.connect()
        assert await book(broker) == before


def _assert_cancel_not_requested(result: CancellationResult, order_id: int) -> None:
    assert isinstance(result, CancellationResult)
    assert result.broker_order_id == order_id
    assert result.requested is False
    assert result.lifecycle is OrderLifecycle.REJECTED
    assert "no cancel was sent" in result.message


@pytest.mark.parametrize("target", ["unknown", "foreign"])
async def test_unowned_cancel_returns_a_refusal_result_without_changing_state(
    adapter_harness: BrokerHarness, target: str
) -> None:
    order_id = UNKNOWN_ORDER_ID if target == "unknown" else adapter_harness.foreign_order_id
    assert order_id is not None
    async with connected(adapter_harness) as broker:
        before = await book(broker)
        result = await broker.cancel_order(order_id)
        _assert_cancel_not_requested(result, order_id)
        assert await book(broker) == before


@pytest.mark.parametrize(
    "field,value",
    [("client_id", 99), ("order_ref", "MANUAL-1"), ("account_id", FOREIGN_ACCOUNT)],
)
async def test_each_foreign_identity_field_independently_refuses_cancel(
    field: str, value: object
) -> None:
    broker = _build_fake()
    owned_id = REGISTRY["fake"].owned_order_id
    # Change just one field on an otherwise owned seed: neither ownership half
    # can mask a missing check on the other.
    broker._open_orders = tuple(
        order.model_copy(update={field: value}) if order.broker_order_id == owned_id else order
        for order in broker._open_orders
    )
    await broker.connect()
    try:
        before = await book(broker)
        result = await broker.cancel_order(owned_id)
        _assert_cancel_not_requested(result, owned_id)
        assert broker.cancel_calls == []
        assert await book(broker) == before
    finally:
        await broker.disconnect()
