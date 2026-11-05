from __future__ import annotations

import logging

import pytest

from chronos.broker.base import BrokerDataError
from chronos.broker.callbacks import CallbackBridge
from chronos.broker.request_registry import RequestRegistry


def test_timed_out_completed_order_stream_is_refused_until_reconnect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """K-20261103-003: an id-less timeout taints the session until reconnect."""

    bridge = CallbackBridge(RequestRegistry())
    timed_out = bridge.start_completed_orders()
    bridge.release_completed_orders(timed_out)

    with pytest.raises(BrokerDataError, match="reconnect"):
        bridge.start_completed_orders()

    with caplog.at_level(logging.WARNING, logger="chronos.broker.callbacks"):
        bridge.on_completed_order(
            "FOREIGN-CONTRACT-987654321",
            "FOREIGN-ORDER-987654321",
            "FOREIGN-STATE-987654321",
        )
        bridge.on_completed_orders_end()

    assert timed_out.items == []
    assert not timed_out.done.is_set()
    assert "reconnect" in caplog.text
    assert "FOREIGN" not in caplog.text
    assert "987654321" not in caplog.text

    bridge.reset_for_reconnect()
    retry = bridge.start_completed_orders()
    current_row = (object(), object(), object())
    bridge.on_completed_order(*current_row)
    bridge.on_completed_orders_end()

    assert retry.items == [current_row]
    assert retry.done.is_set()
