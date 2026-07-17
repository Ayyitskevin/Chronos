"""Request-ID correlation for the callback-driven official IBKR API.

The official TWS API answers requests on a reader thread via EWrapper
callbacks keyed by ``reqId``. This registry is the thread-safe rendezvous:
the asking side opens a pending request, the callback side accumulates items
and finishes (or fails) it, and the asking side awaits completion with a
timeout. Everything here is standard-library only and fully testable without
the IBKR package.

Fail-closed rules: a timeout raises (never returns partial data silently); a
broker error routed to a request fails that request with the code and text;
finishing an unknown request is ignored (late/duplicate callbacks are
tolerated, matching TWS behavior after cancellation).
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any

from chronos.broker.base import BrokerDataError


@dataclass(slots=True)
class _Pending:
    items: list[Any] = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)
    error_code: int | None = None
    error_message: str | None = None


class RequestRegistry:
    """Issue request IDs and correlate their callback streams."""

    def __init__(self, *, first_request_id: int = 1000) -> None:
        self._lock = threading.Lock()
        self._next_id = first_request_id
        self._pending: dict[int, _Pending] = {}

    def open(self) -> int:
        """Issue a fresh request ID with an empty pending accumulation."""

        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = _Pending()
            return request_id

    def add(self, request_id: int, item: Any) -> None:
        """Accumulate one callback item (callback thread)."""

        with self._lock:
            pending = self._pending.get(request_id)
        if pending is not None and not pending.done.is_set():
            pending.items.append(item)

    def finish(self, request_id: int) -> None:
        """Mark a request complete (callback thread). Unknown IDs are ignored."""

        with self._lock:
            pending = self._pending.get(request_id)
        if pending is not None:
            pending.done.set()

    def fail(self, request_id: int, code: int, message: str) -> None:
        """Fail a request with broker error evidence (callback thread)."""

        with self._lock:
            pending = self._pending.get(request_id)
        if pending is not None and not pending.done.is_set():
            pending.error_code = code
            pending.error_message = message
            pending.done.set()

    def _take(self, request_id: int, timed_out: bool, timeout: float) -> list[Any]:
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            raise BrokerDataError(f"request {request_id} is not pending (already consumed?)")
        if timed_out:
            raise BrokerDataError(
                f"request {request_id} timed out after {timeout:.1f}s without completing; "
                "refusing to return partial broker data"
            )
        if pending.error_code is not None:
            raise BrokerDataError(
                f"broker error {pending.error_code} for request {request_id}: "
                f"{pending.error_message or 'no detail'}"
            )
        return pending.items

    def wait_sync(self, request_id: int, *, timeout: float) -> list[Any]:
        """Block until the request completes; raise on timeout or error."""

        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            raise BrokerDataError(f"request {request_id} is not pending (already consumed?)")
        completed = pending.done.wait(timeout)
        return self._take(request_id, timed_out=not completed, timeout=timeout)

    async def wait(self, request_id: int, *, timeout: float) -> list[Any]:
        """Await completion without blocking the event loop."""

        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            raise BrokerDataError(f"request {request_id} is not pending (already consumed?)")
        loop = asyncio.get_running_loop()
        completed = await loop.run_in_executor(None, pending.done.wait, timeout)
        return self._take(request_id, timed_out=not completed, timeout=timeout)

    def abandon(self, request_id: int) -> None:
        """Drop a pending request (e.g. after cancelling the market-data line)."""

        with self._lock:
            self._pending.pop(request_id, None)
