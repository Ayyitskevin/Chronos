"""One long-lived asyncio loop for Streamlit-safe broker access."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Event, Lock, Thread
from typing import Any, TypeVar

from chronos.broker.base import Broker

T = TypeVar("T")


class BrokerConnectionManager:
    """Own a broker adapter and serialize calls on a dedicated background loop."""

    def __init__(self, broker: Broker, *, call_timeout_seconds: float = 30.0) -> None:
        self.broker = broker
        self._call_timeout_seconds = call_timeout_seconds
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._run_loop, name="chronos-broker-loop", daemon=True)
        self._ready = Event()
        self._lifecycle_lock = Lock()
        self._serialization_lock: asyncio.Lock | None = None
        self._started = False
        self._closing = False
        self._closed = False
        self._logger = logging.getLogger("chronos.broker.connection")

    @property
    def running(self) -> bool:
        return self._started and self._thread.is_alive() and not self._closed

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
            raise

    def connect(self) -> None:
        self.run(self.broker.connect())

    def disconnect(self) -> None:
        if self.running:
            self.run(self.broker.disconnect())

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
