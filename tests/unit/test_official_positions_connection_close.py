"""D-54: the official adapter's positions read is not complete when the connection closed mid-read.

Threat map probe 3: ``on_connection_closed`` fails every single-flight by setting ``done`` with no
error marker, and ``positions()`` used to check only that ``done`` was set, so one row followed by
a socket close read as a complete answer. ``open_orders()`` already refused that shape by also
checking ``bridge.connection_closed``; ``positions()`` now does the same. No EWrapper thread code
changes: the close still arrives through ``connectionClosed`` exactly as before.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from tests.support import fake_ibapi

from chronos.broker.base import BrokerConnectionError
from chronos.broker.official_ibkr import OfficialIBKRBroker
from chronos.domain.models import UnderlyingContract

_SPY = UnderlyingContract(con_id=111, symbol="SPY")


def _broker(monkeypatch: pytest.MonkeyPatch, *, close_mid_read: bool) -> Any:
    gateway = fake_ibapi.FakeGateway.seeded()
    fake_ibapi.install(monkeypatch, gateway)

    def positions(client: Any) -> None:
        client.emit("position", fake_ibapi.ACCOUNT, gateway.contract_of(_SPY), 10.0, 400.0)
        if close_mid_read:
            # The socket goes away after one row: positionEnd never arrives.
            client.emit("connectionClosed")
            return
        client.emit("positionEnd")

    monkeypatch.setattr(gateway, "positions", positions)
    return OfficialIBKRBroker(fake_ibapi.official_settings())


def _read(broker: Any) -> Any:
    async def drive() -> Any:
        await broker.connect()
        try:
            return await broker.positions()
        finally:
            await broker.disconnect()

    return asyncio.run(drive())


def test_a_positions_read_truncated_by_a_connection_close_raises_instead_of_reading_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _broker(monkeypatch, close_mid_read=True)
    with pytest.raises(BrokerConnectionError, match="positions request did not complete"):
        _read(broker)


def test_a_complete_positions_read_still_returns_its_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: the same row with ``positionEnd`` is returned unchanged."""

    rows = _read(_broker(monkeypatch, close_mid_read=False))
    assert [(row.contract.con_id, row.quantity) for row in rows] == [(111, Decimal(10))]
