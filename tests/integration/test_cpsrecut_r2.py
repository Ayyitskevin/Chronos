from __future__ import annotations

import logging

import pytest

from chronos.broker.callbacks import CallbackBridge
from chronos.broker.request_registry import RequestRegistry


def test_late_completed_order_stream_is_quarantined_from_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge = CallbackBridge(RequestRegistry())
    timed_out = bridge.start_completed_orders()
    bridge.release_completed_orders(timed_out)
    retry = bridge.start_completed_orders()

    with caplog.at_level(logging.WARNING, logger="chronos.broker.callbacks"):
        bridge.on_completed_order(
            "FOREIGN-CONTRACT-987654321",
            "FOREIGN-ORDER-987654321",
            "FOREIGN-STATE-987654321",
        )
        bridge.on_completed_orders_end()

    assert retry.items == []
    assert not retry.done.is_set()
    assert "retired request generation 1" in caplog.text
    assert "FOREIGN" not in caplog.text
    assert "987654321" not in caplog.text

    current_row = (object(), object(), object())
    bridge.on_completed_order(*current_row)
    bridge.on_completed_orders_end()

    assert retry.items == [current_row]
    assert retry.done.is_set()
