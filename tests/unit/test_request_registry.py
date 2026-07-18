"""Request registry: correlation, timeout, error routing, thread handoff."""

from __future__ import annotations

import threading

import pytest

from chronos.broker.base import BrokerDataError
from chronos.broker.request_registry import RequestRegistry


def test_accumulate_and_finish() -> None:
    registry = RequestRegistry()
    request_id = registry.open()
    registry.add(request_id, "a")
    registry.add(request_id, "b")
    registry.finish(request_id)
    assert registry.wait_sync(request_id, timeout=1.0) == ["a", "b"]


def test_timeout_raises_never_partial() -> None:
    registry = RequestRegistry()
    request_id = registry.open()
    registry.add(request_id, "partial")
    with pytest.raises(BrokerDataError, match="timed out"):
        registry.wait_sync(request_id, timeout=0.05)


def test_error_fails_the_request_with_evidence() -> None:
    registry = RequestRegistry()
    request_id = registry.open()
    registry.fail(request_id, 200, "No security definition has been found")
    with pytest.raises(BrokerDataError, match="broker error 200"):
        registry.wait_sync(request_id, timeout=1.0)


def test_unknown_request_callbacks_are_ignored() -> None:
    registry = RequestRegistry()
    registry.add(9999, "late")  # no exception
    registry.finish(9999)
    registry.fail(9999, 1, "late error")


def test_result_consumed_exactly_once() -> None:
    registry = RequestRegistry()
    request_id = registry.open()
    registry.finish(request_id)
    registry.wait_sync(request_id, timeout=1.0)
    with pytest.raises(BrokerDataError, match="not pending"):
        registry.wait_sync(request_id, timeout=1.0)


def test_completion_from_another_thread() -> None:
    registry = RequestRegistry()
    request_id = registry.open()

    def callback_thread() -> None:
        registry.add(request_id, 42)
        registry.finish(request_id)

    thread = threading.Thread(target=callback_thread)
    thread.start()
    result = registry.wait_sync(request_id, timeout=2.0)
    thread.join()
    assert result == [42]


async def test_async_wait_completes() -> None:
    registry = RequestRegistry()
    request_id = registry.open()

    def callback_thread() -> None:
        registry.add(request_id, "x")
        registry.finish(request_id)

    thread = threading.Thread(target=callback_thread)
    thread.start()
    result = await registry.wait(request_id, timeout=2.0)
    thread.join()
    assert result == ["x"]


def test_ids_are_unique_and_increasing() -> None:
    registry = RequestRegistry(first_request_id=5)
    first = registry.open()
    second = registry.open()
    assert first == 5
    assert second == 6
