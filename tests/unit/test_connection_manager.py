import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest
from tests.conftest import FIXED_NOW

from chronos.broker.base import BrokerDataError, BrokerRefusedBeforeSend, BrokerSafetyError
from chronos.broker.connection import BrokerConnectionManager
from chronos.broker.demo import DemoBroker
from chronos.domain.enums import ConnectionState, ReconciliationStatus
from chronos.orders.reconciliation_readiness import ReconciliationReadiness


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


def test_connection_status_caches_only_sanitized_broker_truth() -> None:
    broker = DemoBroker(clock=lambda: FIXED_NOW)
    manager = BrokerConnectionManager(broker, observation_clock=lambda: FIXED_NOW)
    try:
        manager.connect()
        status = manager.connection_status()
        observation = manager.connection_observation()

        assert status.account_id
        assert observation.connected is True
        assert observation.connection_state == "CONNECTED"
        assert observation.observed_at == FIXED_NOW
        assert observation.generation >= 2
        assert not hasattr(observation, "account_id")
    finally:
        manager.close()


def test_reading_cached_connection_observation_never_calls_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = DemoBroker(clock=lambda: FIXED_NOW)
    manager = BrokerConnectionManager(broker, observation_clock=lambda: FIXED_NOW)
    try:
        manager.connect()
        expected = manager.connection_status()

        def forbidden() -> object:
            raise AssertionError("cached observation must not call connection_status")

        monkeypatch.setattr(broker, "connection_status", forbidden)
        first = manager.connection_observation()
        second = manager.connection_observation()

        assert expected.connected is True
        assert second == first
    finally:
        manager.close()


def test_connection_invalidation_advances_and_unknowns_cached_truth() -> None:
    broker = DemoBroker(clock=lambda: FIXED_NOW)
    manager = BrokerConnectionManager(broker, observation_clock=lambda: FIXED_NOW)
    try:
        manager.connect()
        manager.connection_status()
        connected_generation = manager.connection_observation().generation

        manager.disconnect()
        disconnected = manager.connection_observation()

        assert disconnected.generation > connected_generation
        assert disconnected.connected is None
        assert disconnected.connection_state is None
        assert disconnected.reason_code == "connection_uncertain"
    finally:
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


def _mark_ready(readiness: ReconciliationReadiness) -> int:
    generation = readiness.snapshot().generation
    assert readiness.complete(
        expected_generation=generation,
        status=ReconciliationStatus.RECONCILED,
        reason="test broker and local parity",
        reconciled_at=FIXED_NOW,
    )
    return generation


def test_disconnect_and_reconnect_never_inherit_prior_readiness() -> None:
    readiness = ReconciliationReadiness()
    broker = DemoBroker(clock=lambda: FIXED_NOW)
    manager = BrokerConnectionManager(broker, readiness=readiness)
    try:
        manager.connect()
        ready_generation = _mark_ready(readiness)
        assert readiness.is_reconciled(expected_generation=ready_generation)

        manager.disconnect()
        disconnected = readiness.snapshot()
        assert disconnected.status is ReconciliationStatus.PENDING
        assert disconnected.generation > ready_generation

        manager.connect()
        reconnected = readiness.snapshot()
        assert reconnected.status is ReconciliationStatus.PENDING
        assert reconnected.generation > disconnected.generation
    finally:
        manager.close()


def test_failed_broker_call_invalidates_prior_readiness() -> None:
    readiness = ReconciliationReadiness()
    broker = DemoBroker(clock=lambda: FIXED_NOW)
    manager = BrokerConnectionManager(broker, readiness=readiness)

    async def fail() -> None:
        raise RuntimeError("socket state unknown")

    try:
        manager.connect()
        ready_generation = _mark_ready(readiness)
        with pytest.raises(RuntimeError, match="socket state unknown"):
            manager.run(fail())
        assert readiness.is_reconciled(expected_generation=ready_generation) is False
        assert readiness.snapshot().status is ReconciliationStatus.PENDING
    finally:
        manager.close()


def test_healthy_idempotent_connect_preserves_current_readiness() -> None:
    readiness = ReconciliationReadiness()
    broker = DemoBroker(clock=lambda: FIXED_NOW)
    manager = BrokerConnectionManager(broker, readiness=readiness)
    try:
        manager.connect()
        ready_generation = _mark_ready(readiness)

        manager.connect()

        assert readiness.is_reconciled(expected_generation=ready_generation)
        assert readiness.snapshot().generation == ready_generation
    finally:
        manager.close()


@pytest.mark.parametrize(
    "error",
    [
        BrokerDataError("broker evidence missing"),
        BrokerSafetyError("broker evidence violated a safety invariant"),
    ],
)
def test_broker_evidence_errors_invalidate_prior_readiness(error: Exception) -> None:
    readiness = ReconciliationReadiness()
    broker = DemoBroker(clock=lambda: FIXED_NOW)
    manager = BrokerConnectionManager(broker, readiness=readiness)

    async def fail() -> None:
        raise error

    try:
        manager.connect()
        ready_generation = _mark_ready(readiness)
        with pytest.raises(type(error)):
            manager.run(fail())
        assert readiness.is_reconciled(expected_generation=ready_generation) is False
    finally:
        manager.close()


def test_proven_pre_send_refusal_preserves_connection_readiness() -> None:
    readiness = ReconciliationReadiness()
    broker = DemoBroker(clock=lambda: FIXED_NOW)
    manager = BrokerConnectionManager(broker, readiness=readiness)

    async def refuse() -> None:
        raise BrokerRefusedBeforeSend("local refusal before network send")

    try:
        manager.connect()
        ready_generation = _mark_ready(readiness)
        with pytest.raises(BrokerRefusedBeforeSend):
            manager.run(refuse())
        assert readiness.is_reconciled(expected_generation=ready_generation)
    finally:
        manager.close()
