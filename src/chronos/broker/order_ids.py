"""Central, thread-safe IBKR order-ID allocation.

IBKR grants an initial order-ID floor through the ``nextValidId`` callback;
every order the client submits must use a strictly increasing ID from there.
This allocator is the single authority: no other module may mint an order ID.

Rules (owner spec):
- No allocation before the broker has seeded the floor (fail-closed).
- IDs are strictly monotonic under concurrency (lock-protected).
- Re-seeding never lowers the floor (a late or duplicate ``nextValidId``
  cannot cause ID reuse); it can only raise it.
- The sequence is never reset automatically.
"""

from __future__ import annotations

import threading


class OrderIdError(RuntimeError):
    """Order-ID allocation was attempted in an unsafe state."""


class OrderIdAllocator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next: int | None = None

    def seed(self, next_valid_id: int) -> None:
        """Record the broker-granted floor; never lowers an existing floor."""

        if next_valid_id < 0:
            raise OrderIdError(f"nextValidId must be non-negative, got {next_valid_id}")
        with self._lock:
            if self._next is None or next_valid_id > self._next:
                self._next = next_valid_id

    @property
    def seeded(self) -> bool:
        with self._lock:
            return self._next is not None

    def peek(self) -> int:
        with self._lock:
            if self._next is None:
                raise OrderIdError("order-ID allocator is not seeded (no nextValidId yet)")
            return self._next

    def allocate(self) -> int:
        """Return the next order ID and advance the sequence atomically."""

        with self._lock:
            if self._next is None:
                raise OrderIdError(
                    "refusing to allocate an order ID before the broker seeds nextValidId"
                )
            allocated = self._next
            self._next = allocated + 1
            return allocated
