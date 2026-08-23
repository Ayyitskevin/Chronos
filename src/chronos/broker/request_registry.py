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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from chronos.broker.base import BrokerDataError

ItemNormalizer = Callable[[Any], tuple[Any, bool]]
ItemSortKey = Callable[[Any], Any]


@dataclass(slots=True)
class _Pending:
    items: list[Any] = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)
    max_items: int | None = None
    normalizer: ItemNormalizer | None = None
    sort_key: ItemSortKey | None = None
    truncated: bool = False
    completed_at: datetime | None = None
    error: BrokerDataError | None = None
    error_code: int | None = None


class RequestRegistry:
    """Issue request IDs and correlate their callback streams."""

    def __init__(self, *, first_request_id: int = 1000) -> None:
        self._lock = threading.Lock()
        self._next_id = first_request_id
        self._pending: dict[int, _Pending] = {}

    @property
    def pending_count(self) -> int:
        """Return the live pending-request count for cleanup observability."""

        with self._lock:
            return len(self._pending)

    def open(
        self,
        *,
        max_items: int | None = None,
        normalizer: ItemNormalizer | None = None,
        sort_key: ItemSortKey | None = None,
    ) -> int:
        """Issue a fresh request ID with an optionally bounded accumulation.

        ``normalizer`` runs at callback ingress and returns ``(value,
        truncated)``.  A configured ``sort_key`` retains the deterministic
        smallest ``max_items`` values instead of an arrival-order prefix.  Any
        overflow is evidence, never success: the waiter receives a typed error
        carrying the retained values and ``truncated=True``.
        """

        if max_items is not None and max_items <= 0:
            raise ValueError("request registry max_items must be positive")

        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = _Pending(
                max_items=max_items,
                normalizer=normalizer,
                sort_key=sort_key,
            )
            return request_id

    def add(self, request_id: int, item: Any) -> None:
        """Accumulate one callback item (callback thread)."""

        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or pending.done.is_set():
                return
            normalizer = pending.normalizer
        try:
            normalized, item_truncated = (
                normalizer(item) if normalizer is not None else (item, False)
            )
        except Exception as error:
            self.fail_exception(
                request_id,
                BrokerDataError(
                    f"request callback item could not be normalized: {type(error).__name__}"
                ),
            )
            return

        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or pending.done.is_set():
                return
            pending.truncated = pending.truncated or item_truncated
            if pending.max_items is None or len(pending.items) < pending.max_items:
                pending.items.append(normalized)
                return
            pending.truncated = True
            if pending.sort_key is None:
                return
            sort_key = pending.sort_key
            worst_index = max(
                range(len(pending.items)),
                key=lambda index: sort_key(pending.items[index]),
            )
            if sort_key(normalized) < sort_key(pending.items[worst_index]):
                pending.items[worst_index] = normalized

    def finish(self, request_id: int) -> None:
        """Mark a request complete (callback thread). Unknown IDs are ignored."""

        with self._lock:
            pending = self._pending.get(request_id)
            if pending is not None:
                pending.completed_at = datetime.now(tz=UTC)
                pending.done.set()

    def fail(self, request_id: int, code: int, message: str) -> None:
        """Fail a request with broker error evidence (callback thread)."""

        self.fail_exception(
            request_id,
            BrokerDataError(f"broker error {code} for request {request_id}: {message}"),
            error_code=code,
        )

    def fail_exception(
        self,
        request_id: int,
        error: BrokerDataError,
        *,
        error_code: int | None = None,
    ) -> None:
        """Fail a request without discarding its bounded callback prefix."""

        with self._lock:
            pending = self._pending.get(request_id)
            if pending is not None and not pending.done.is_set():
                pending.error = error
                pending.error_code = error_code
                pending.done.set()

    def fail_all_exception(
        self,
        error_factory: Callable[[int], BrokerDataError],
        *,
        error_code: int | None = None,
    ) -> tuple[int, ...]:
        """Atomically fail every live request with an independent typed error.

        A global broker error has no request ID, so routing it one request at a
        time can race a completion callback and sharing one exception would let
        evidence from one request overwrite another.  The factory is invoked
        once per request while the registry lock is held and must not re-enter
        the registry.
        """

        failed: list[int] = []
        with self._lock:
            for request_id in sorted(self._pending):
                pending = self._pending[request_id]
                if pending.done.is_set():
                    continue
                pending.error = error_factory(request_id)
                pending.error_code = error_code
                pending.done.set()
                failed.append(request_id)
        return tuple(failed)

    def _take(self, request_id: int, timed_out: bool, timeout: float) -> list[Any]:
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            raise BrokerDataError(f"request {request_id} is not pending (already consumed?)")
        observed = tuple(
            sorted(pending.items, key=pending.sort_key)
            if pending.sort_key is not None
            else pending.items
        )
        if pending.error is not None:
            self._attach_evidence(
                pending.error,
                request_id=request_id,
                observed=observed,
                observed_at=datetime.now(tz=UTC),
                truncated=pending.truncated,
                completed=False,
                failure_kind="broker-error",
                error_code=pending.error_code,
            )
            raise pending.error
        if timed_out and not pending.done.is_set():
            error = BrokerDataError(
                f"request {request_id} timed out after {timeout:.1f}s without completing; "
                "refusing to return partial broker data"
            )
            self._attach_evidence(
                error,
                request_id=request_id,
                observed=observed,
                observed_at=datetime.now(tz=UTC),
                truncated=pending.truncated,
                completed=False,
                failure_kind="timeout",
            )
            raise error
        if pending.truncated:
            error = BrokerDataError(
                f"request {request_id} exceeded its bounded callback evidence limit"
            )
            self._attach_evidence(
                error,
                request_id=request_id,
                observed=observed,
                observed_at=pending.completed_at or datetime.now(tz=UTC),
                truncated=True,
                completed=True,
                failure_kind="overflow",
            )
            raise error
        return list(observed)

    @staticmethod
    def _attach_evidence(
        error: BrokerDataError,
        *,
        request_id: int,
        observed: tuple[Any, ...],
        observed_at: datetime,
        truncated: bool,
        completed: bool,
        failure_kind: str,
        error_code: int | None = None,
    ) -> None:
        vars(error).update(
            request_id=request_id,
            observed_values=observed,
            observed_at=observed_at,
            truncated=truncated,
            request_completed=completed,
            failure_kind=failure_kind,
            broker_error_code=error_code,
        )

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

    def interrupt(self, request_id: int, *, error: BrokerDataError) -> BrokerDataError:
        """Consume a cancelled waiter while preserving its bounded evidence.

        ``asyncio.wait_for`` cancels an adapter coroutine at its own deadline,
        which can be shorter than the registry timeout.  The callback wait in
        the executor cannot be cancelled, so simply propagating cancellation
        would leak the pending entry and discard its prefix.  Callers raise the
        returned error from the ``CancelledError`` after translating any raw
        observations into their domain receipt.
        """

        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            self._attach_evidence(
                error,
                request_id=request_id,
                observed=(),
                observed_at=datetime.now(tz=UTC),
                truncated=False,
                completed=False,
                failure_kind="interrupted",
            )
            return error
        was_completed = pending.done.is_set()
        pending.done.set()

        observed = tuple(
            sorted(pending.items, key=pending.sort_key)
            if pending.sort_key is not None
            else pending.items
        )
        if pending.error is not None:
            self._attach_evidence(
                pending.error,
                request_id=request_id,
                observed=observed,
                observed_at=datetime.now(tz=UTC),
                truncated=pending.truncated,
                completed=False,
                failure_kind="broker-error",
                error_code=pending.error_code,
            )
            return pending.error

        self._attach_evidence(
            error,
            request_id=request_id,
            observed=observed,
            observed_at=pending.completed_at or datetime.now(tz=UTC),
            truncated=pending.truncated,
            completed=was_completed,
            failure_kind="interrupted",
        )
        return error

    def abandon(self, request_id: int) -> None:
        """Drop a pending request (e.g. after cancelling the market-data line)."""

        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is not None:
            pending.done.set()
