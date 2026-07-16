import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest
from tests.conftest import FIXED_NOW

from chronos.broker.connection import BrokerConnectionManager
from chronos.broker.demo import DemoBroker
from chronos.domain.enums import ConnectionState


def test_connection_manager_reuses_one_background_thread() -> None:
    broker = DemoBroker(clock=lambda: FIXED_NOW)
    manager = BrokerConnectionManager(broker)
    try:
        manager.connect()
        first_thread = manager._thread
        first = manager.run(broker.connection_status())
        second = manager.run(broker.connection_status())

        assert first.state is ConnectionState.CONNECTED
        assert second == first
        assert manager._thread is first_thread
        assert manager.running is True
    finally:
        manager.close()

    assert manager.running is False
    manager.close()


def test_closed_manager_fails_loud_without_leaking_coroutine() -> None:
    broker = DemoBroker(clock=lambda: FIXED_NOW)
    manager = BrokerConnectionManager(broker)
    manager.close()

    with pytest.raises(RuntimeError, match="closed"):
        manager.run(broker.connection_status())


def test_connection_manager_serializes_concurrent_calls() -> None:
    broker = DemoBroker(clock=lambda: FIXED_NOW)
    manager = BrokerConnectionManager(broker)
    state_lock = Lock()
    active = 0
    max_active = 0

    async def tracked_call() -> None:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        with state_lock:
            active -= 1

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(manager.run, tracked_call())
            second = executor.submit(manager.run, tracked_call())
            first.result()
            second.result()
    finally:
        manager.close()

    assert max_active == 1


def test_timed_out_call_is_cancelled_before_it_can_finish() -> None:
    broker = DemoBroker(clock=lambda: FIXED_NOW)
    manager = BrokerConnectionManager(broker)
    cancelled = Event()
    completed = Event()

    async def slow_call() -> None:
        try:
            await asyncio.sleep(1)
            completed.set()
        finally:
            cancelled.set()

    try:
        with pytest.raises(TimeoutError):
            manager.run(slow_call(), timeout=0.01)
        assert cancelled.wait(timeout=1)
    finally:
        manager.close()

    assert not completed.is_set()
