"""Request registry: correlation, timeout, error routing, thread handoff."""

from __future__ import annotations

import threading

import pytest

from chronos.broker.base import BrokerDataError
from chronos.broker.market_data import MarketDataPermissionError
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
    with pytest.raises(BrokerDataError, match="timed out") as raised:
        registry.wait_sync(request_id, timeout=0.05)

    assert raised.value.observed_values == ("partial",)
    assert raised.value.request_completed is False
    assert raised.value.failure_kind == "timeout"
    assert raised.value.observed_at.tzinfo is not None


def test_error_fails_the_request_with_evidence() -> None:
    registry = RequestRegistry()
    request_id = registry.open()
    registry.add(request_id, "partial")
    registry.fail(request_id, 200, "No security definition has been found")
    with pytest.raises(BrokerDataError, match="broker error 200") as raised:
        registry.wait_sync(request_id, timeout=1.0)

    assert raised.value.observed_values == ("partial",)
    assert raised.value.broker_error_code == 200
    assert raised.value.failure_kind == "broker-error"


def test_bounded_request_retains_deterministic_prefix_and_reports_overflow() -> None:
    registry = RequestRegistry()
    request_id = registry.open(
        max_items=2,
        normalizer=lambda value: (int(value), False),
        sort_key=lambda value: value,
    )
    for value in (9, 2, 7, 1):
        registry.add(request_id, value)
    registry.finish(request_id)

    with pytest.raises(BrokerDataError, match="bounded callback evidence limit") as raised:
        registry.wait_sync(request_id, timeout=1.0)

    assert raised.value.observed_values == (1, 2)
    assert raised.value.truncated is True
    assert raised.value.request_completed is True
    assert raised.value.failure_kind == "overflow"


def test_typed_failure_retains_bounded_callback_prefix() -> None:
    registry = RequestRegistry()
    request_id = registry.open(max_items=2)
    registry.add(request_id, "first")
    error = MarketDataPermissionError("permission denied")
    registry.fail_exception(request_id, error, error_code=10089)

    with pytest.raises(MarketDataPermissionError) as raised:
        registry.wait_sync(request_id, timeout=1.0)

    assert raised.value is error
    assert raised.value.observed_values == ("first",)
    assert raised.value.broker_error_code == 10089


def test_global_typed_failure_is_independent_and_retains_each_prefix() -> None:
    registry = RequestRegistry()
    first_id = registry.open(max_items=2)
    second_id = registry.open(max_items=2)
    registry.add(first_id, "first-prefix")
    registry.add(second_id, "second-prefix")

    failed = registry.fail_all_exception(
        lambda request_id: MarketDataPermissionError(
            f"global permission failure for request {request_id}"
        ),
        error_code=10089,
    )

    assert failed == (first_id, second_id)
    with pytest.raises(MarketDataPermissionError) as first:
        registry.wait_sync(first_id, timeout=1.0)
    with pytest.raises(MarketDataPermissionError) as second:
        registry.wait_sync(second_id, timeout=1.0)
    assert first.value is not second.value
    assert first.value.observed_values == ("first-prefix",)
    assert second.value.observed_values == ("second-prefix",)
    assert first.value.broker_error_code == second.value.broker_error_code == 10089


def test_completion_at_timeout_boundary_does_not_mask_terminal_state() -> None:
    registry = RequestRegistry()
    completed_id = registry.open()
    registry.add(completed_id, "complete")
    registry.finish(completed_id)
    assert registry._take(completed_id, timed_out=True, timeout=1.0) == ["complete"]

    failed_id = registry.open()
    failure = MarketDataPermissionError("permission denied")
    registry.fail_exception(failed_id, failure, error_code=10089)
    with pytest.raises(MarketDataPermissionError) as raised:
        registry._take(failed_id, timed_out=True, timeout=1.0)
    assert raised.value is failure


def test_interrupted_waiter_is_consumed_with_its_bounded_prefix() -> None:
    registry = RequestRegistry()
    request_id = registry.open(max_items=2)
    registry.add(request_id, "first")
    registry.add(request_id, "second")

    error = registry.interrupt(
        request_id,
        error=BrokerDataError("outer deadline interrupted request"),
    )

    assert error.observed_values == ("first", "second")
    assert error.failure_kind == "interrupted"
    assert error.request_completed is False
    with pytest.raises(BrokerDataError, match="not pending"):
        registry.wait_sync(request_id, timeout=0.01)


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
