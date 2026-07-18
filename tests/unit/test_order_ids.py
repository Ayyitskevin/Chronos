"""Central order-ID allocator: seeding, monotonicity, thread safety."""

from __future__ import annotations

import threading

import pytest

from chronos.broker.order_ids import OrderIdAllocator, OrderIdError


def test_refuses_allocation_before_seed() -> None:
    allocator = OrderIdAllocator()
    assert allocator.seeded is False
    with pytest.raises(OrderIdError, match="before the broker seeds"):
        allocator.allocate()
    with pytest.raises(OrderIdError):
        allocator.peek()


def test_allocates_monotonically_from_seed() -> None:
    allocator = OrderIdAllocator()
    allocator.seed(100)
    assert allocator.allocate() == 100
    assert allocator.allocate() == 101
    assert allocator.peek() == 102


def test_reseed_never_lowers_the_floor() -> None:
    allocator = OrderIdAllocator()
    allocator.seed(100)
    allocator.allocate()  # -> 100, next 101
    allocator.seed(50)  # late duplicate nextValidId must not cause reuse
    assert allocator.allocate() == 101
    allocator.seed(500)  # a raise is honored
    assert allocator.allocate() == 500


def test_negative_seed_rejected() -> None:
    allocator = OrderIdAllocator()
    with pytest.raises(OrderIdError):
        allocator.seed(-1)


def test_concurrent_allocation_yields_unique_ids() -> None:
    allocator = OrderIdAllocator()
    allocator.seed(1)
    allocated: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(200):
            value = allocator.allocate()
            with lock:
                allocated.append(value)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(allocated) == 1600
    assert len(set(allocated)) == 1600  # no duplicates under contention
