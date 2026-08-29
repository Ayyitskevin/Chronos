"""One long-lived asyncio loop for Streamlit-safe broker access."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime
from threading import Event, Lock, Thread
from typing import Any, Protocol, TypeVar

from chronos.broker.base import (
    Broker,
    BrokerRefusedBeforeSend,
)
from chronos.domain.enums import ConnectionState
from chronos.domain.models import ConnectionStatus
from chronos.utils.time import utc_now

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class BrokerConnectionObservation:
    """Last sanitized broker truth learned at the real connection seam."""

    generation: int = 0
    connected: bool | None = None
    connection_state: str | None = None
    observed_environment: str | None = None
    observed_at: datetime | None = None
    reason_code: str = "never_observed"


class ConnectionStateInvalidator(Protocol):
    """Fail-closed observer for broker-session lifecycle uncertainty."""

    def invalidate(self, reason: str) -> object: ...


class BrokerConnectionManager:
    """Own a broker adapter and serialize calls on a dedicated background loop."""

    def __init__(
        self,
        broker: Broker,
        *,
        call_timeout_seconds: float = 30.0,
        readiness: ConnectionStateInvalidator | None = None,
        observation_clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.broker = broker
        self._call_timeout_seconds = call_timeout_seconds
        self._readiness = readiness
        self._observation_clock = observation_clock
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._run_loop, name="chronos-broker-loop", daemon=True)
        self._ready = Event()
        self._lifecycle_lock = Lock()
        self._observation_lock = Lock()
        self._serialization_lock: asyncio.Lock | None = None
        self._started = False
        self._closing = False
        self._closed = False
        self._logger = logging.getLogger("chronos.broker.connection")
        self._observation = BrokerConnectionObservation()

    @property
    def running(self) -> bool:
        return self._started and self._thread.is_alive() and not self._closed

    def connection_observation(self) -> BrokerConnectionObservation:
        """Return the cached sanitized observation; never call the broker here."""

        with self._observation_lock:
            return self._observation

    def connection_status(self) -> ConnectionStatus:
        """Read broker status once and retain only its non-secret connection facts."""

        status = self.run(self.broker.connection_status())
        with self._observation_lock:
            self._observation = BrokerConnectionObservation(
                generation=self._observation.generation + 1,
                connected=status.connected and status.state is ConnectionState.CONNECTED,
                connection_state=status.state.value,
                observed_environment=status.environment.value,
                observed_at=self._observation_clock(),
                reason_code="status_observed",
            )
        return status

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("Broker connection manager is closed")
            if not self._started:
                self._thread.start()
                self._started = True
        if not self._ready.wait(timeout=self._call_timeout_seconds):
            raise TimeoutError("Broker event loop did not start")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._serialization_lock = asyncio.Lock()
        self._ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    async def _run_serialized(self, coroutine: Coroutine[Any, Any, T]) -> T:
        lock = self._serialization_lock
        if lock is None:
            coroutine.close()
            raise RuntimeError("Broker event loop is not ready")
        acquired = False
        try:
            async with lock:
                acquired = True
                return await coroutine
        finally:
            if not acquired:
                coroutine.close()

    def run(self, coroutine: Coroutine[Any, Any, T], *, timeout: float | None = None) -> T:
        """Run one broker coroutine thread-safely and return its result."""

        try:
            self.start()
            future: Future[T] = asyncio.run_coroutine_threadsafe(
                self._run_serialized(coroutine),
                self._loop,
            )
        except BaseException:
            coroutine.close()
            raise
        try:
            return future.result(timeout=timeout or self._call_timeout_seconds)
        except FutureTimeoutError:
            future.cancel()
            self._invalidate("broker call timed out; connection state is uncertain")
            raise
        except BrokerRefusedBeforeSend:
            # This exception is a narrow proof that no broker send started; it
            # says nothing adverse about connection health.
            raise
        except BaseException:
            self._invalidate("broker call failed; connection state is uncertain")
            raise

    def connect(self) -> None:
        already_healthy = False
        if self.running:
            try:
                status = self.connection_status()
            except BaseException:
                # The run call already invalidated for an uncertain failure. A
                # typed local safety refusal still needs explicit invalidation.
                self._invalidate("broker connection state is uncertain")
            else:
                already_healthy = status.connected and status.state is ConnectionState.CONNECTED
        if not already_healthy:
            # A genuinely new/recovered session never inherits prior evidence.
            self._invalidate("broker connection started; reconciliation required")
        self.run(self.broker.connect())

    def disconnect(self) -> None:
        # Invalidate before touching the broker so even a failed disconnect
        # cannot leave prior authorization evidence armed.
        self._invalidate("broker disconnected; reconciliation required after reconnect")
        if self.running:
            self.run(self.broker.disconnect())

    def _invalidate(self, reason: str) -> None:
        with self._observation_lock:
            self._observation = BrokerConnectionObservation(
                generation=self._observation.generation + 1,
                observed_at=self._observation_clock(),
                reason_code="connection_uncertain",
            )
        readiness = self._readiness
        if readiness is None:
            return
        try:
            readiness.invalidate(reason)
        except Exception:
            # Safety-observer failures must be visible but must not replace the
            # broker exception/lifecycle result the caller is already handling.
            self._logger.exception(
                "Failed to invalidate reconciliation readiness",
                extra={"event": "reconciliation_readiness_invalidation_failed"},
            )

    def close(self) -> None:
        """Disconnect and stop the loop; safe to call repeatedly."""

        with self._lifecycle_lock:
            if self._closed or self._closing:
                return
            self._closing = True
        try:
            self.disconnect()
        except Exception:
            self._logger.exception(
                "Broker disconnect failed during shutdown",
                extra={"event": "broker_shutdown_disconnect_failed"},
            )
        with self._lifecycle_lock:
            self._closed = True
            self._closing = False
            if self._started and self._thread.is_alive():
                self._loop.call_soon_threadsafe(self._loop.stop)
        if self._started and self._thread.is_alive():
            self._thread.join(timeout=self._call_timeout_seconds)
            if self._thread.is_alive():
                raise TimeoutError("Broker event loop did not stop cleanly")

    def __enter__(self) -> BrokerConnectionManager:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
