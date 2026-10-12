"""Daybreak r1 P2-1: a cancel the broker REFUSES must not leave the tracker at CANCEL_PENDING.

``OrderCancellationService.cancel`` records ``CANCEL_PENDING`` BEFORE it calls the broker (so an
in-flight cancel is durably visible) and used to ignore ``requested``. Once the official adapter
refuses a cancel it cannot prove is Chronos-owned (S1-F1: ``requested=False``, or
``BrokerRefusedBeforeSend`` when the ownership read fails), local state would claim a cancel was
pending when nothing was sent. The service now records a compensating event, back to the status
the order held, but only when the broker PROVES nothing was sent, and raises ``OrderMutationError``.
An ambiguous failure still leaves ``CANCEL_PENDING``.

Reuses the order-pipeline harness (in-memory database, FakeBroker double, no network).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from decimal import Decimal

import pytest
from tests.integration.test_order_pipeline import (
    _drive_to_confirmed,
    _Harness,
    _short_put_intent,
)
from tests.support.order_fakes import FIXED_NOW, PAPER_ACCOUNT, FakeBroker, paper_settings

from chronos.broker.base import BrokerConnectionError, BrokerRefusedBeforeSend
from chronos.domain.enums import OrderLifecycle
from chronos.domain.models import CancellationResult
from chronos.orders.mutations import OrderMutationError
from chronos.orders.tracker import OrderStatusUpdate


class _CancelBroker(FakeBroker):
    """A FakeBroker whose cancel answer is scripted, and which can act mid-cancel."""

    def __init__(self) -> None:
        super().__init__()
        self.mode = "accept"
        self.during: Callable[[], None] | None = None

    async def cancel_order(self, broker_order_id: int) -> CancellationResult:
        self.cancel_calls.append(broker_order_id)
        if self.during is not None:
            self.during()
        if self.mode == "typed":
            raise BrokerRefusedBeforeSend("could not prove ownership; no cancel was sent")
        if self.mode == "ambiguous":
            raise BrokerConnectionError("connection lost during the cancel; state unknown")
        if self.mode == "not_requested":
            return CancellationResult(
                broker_order_id=broker_order_id,
                requested=False,
                lifecycle=OrderLifecycle.REJECTED,
                message="not a Chronos order; refusing to cancel it (no cancel was sent)",
                timestamp=FIXED_NOW,
            )
        return CancellationResult(
            broker_order_id=broker_order_id,
            requested=True,
            lifecycle=OrderLifecycle.CANCELLED,
            message="fake cancel accepted",
            timestamp=FIXED_NOW,
        )


@pytest.fixture
def rig() -> Iterator[tuple[_Harness, _CancelBroker]]:
    broker = _CancelBroker()
    harness = _Harness(broker, paper_settings())
    try:
        intent = _short_put_intent()
        _drive_to_confirmed(harness, intent, FIXED_NOW)
        harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
        yield harness, broker
    finally:
        harness.close()


def _status(harness: _Harness) -> OrderLifecycle:
    stored = harness.service.get("intent-1")
    assert stored is not None
    return stored.status


def _trail(harness: _Harness) -> list[tuple[str, OrderLifecycle]]:
    events = harness.tracker_repo.events("intent-1", current_account_id=PAPER_ACCOUNT)
    return [(event.source, event.to_status) for event in events]


def _update(lifecycle: OrderLifecycle, filled: str, remaining: str) -> OrderStatusUpdate:
    return OrderStatusUpdate(
        intent_id="intent-1",
        broker_order_id=9001,
        lifecycle=lifecycle,
        filled_quantity=Decimal(filled),
        remaining_quantity=Decimal(remaining),
        occurred_at=FIXED_NOW,
    )


@pytest.mark.parametrize("mode", ["not_requested", "typed"])
def test_a_refused_cancel_does_not_leave_the_tracker_at_cancel_pending(
    rig: tuple[_Harness, _CancelBroker], mode: str
) -> None:
    harness, broker = rig
    broker.mode = mode
    assert _status(harness) is OrderLifecycle.SUBMITTED
    with pytest.raises(OrderMutationError, match="cancel refused"):
        harness.service.cancel("intent-1", now=FIXED_NOW)
    assert broker.cancel_calls == [9001]
    assert _status(harness) is OrderLifecycle.SUBMITTED, "the tracker claims a cancel is pending"
    sources = _trail(harness)
    assert sources[-2:] == [
        ("CANCEL", OrderLifecycle.CANCEL_PENDING),
        ("CANCEL", OrderLifecycle.SUBMITTED),
    ]
    events = harness.tracker_repo.events("intent-1", current_account_id=PAPER_ACCOUNT)
    assert events[-1].evidence["phase"] == "cancel_refused"


def test_an_ambiguous_cancel_failure_still_leaves_cancel_pending(
    rig: tuple[_Harness, _CancelBroker],
) -> None:
    """Nothing is proven here (the connection may have dropped after the send): unchanged."""

    harness, broker = rig
    broker.mode = "ambiguous"
    with pytest.raises(BrokerConnectionError):
        harness.service.cancel("intent-1", now=FIXED_NOW)
    assert _status(harness) is OrderLifecycle.CANCEL_PENDING


def test_a_retry_after_a_refusal_is_recorded_again_and_can_complete(
    rig: tuple[_Harness, _CancelBroker],
) -> None:
    """The event key differs per attempt, or a retry would send with no durable CANCEL_PENDING."""

    harness, broker = rig
    broker.mode = "typed"
    with pytest.raises(OrderMutationError):
        harness.service.cancel("intent-1", now=FIXED_NOW)
    assert _status(harness) is OrderLifecycle.SUBMITTED
    broker.mode = "accept"
    seen: list[OrderLifecycle] = []
    broker.during = lambda: seen.append(_status(harness))
    result = harness.service.cancel("intent-1", now=FIXED_NOW.replace(second=30))
    assert result.lifecycle is OrderLifecycle.CANCELLED
    assert seen == [OrderLifecycle.CANCEL_PENDING], (
        "the retry sent without a durable CANCEL_PENDING"
    )
    assert _status(harness) is OrderLifecycle.CANCELLED
    pending = [t for t in _trail(harness) if t[1] is OrderLifecycle.CANCEL_PENDING]
    assert len(pending) == 2


def test_a_partially_filled_order_returns_to_partially_filled(
    rig: tuple[_Harness, _CancelBroker],
) -> None:
    harness, broker = rig
    partial = _update(OrderLifecycle.PARTIALLY_FILLED, "1", "1")
    assert harness.tracker.ingest(partial, current_account_id=PAPER_ACCOUNT).lifecycle_changed
    broker.mode = "not_requested"
    with pytest.raises(OrderMutationError):
        harness.service.cancel("intent-1", now=FIXED_NOW)
    assert _status(harness) is OrderLifecycle.PARTIALLY_FILLED


def test_the_revert_never_clobbers_a_status_a_broker_callback_already_advanced(
    rig: tuple[_Harness, _CancelBroker],
) -> None:
    """Compare-and-set: the order filled while the refused cancel was in flight; FILLED stays."""

    harness, broker = rig
    broker.mode = "typed"
    fill = _update(OrderLifecycle.FILLED, "1", "0")
    broker.during = lambda: harness.tracker.ingest(fill, current_account_id=PAPER_ACCOUNT)
    with pytest.raises(OrderMutationError):
        harness.service.cancel("intent-1", now=FIXED_NOW)
    assert _status(harness) is OrderLifecycle.FILLED


def test_broker_updates_still_apply_after_the_revert(
    rig: tuple[_Harness, _CancelBroker],
) -> None:
    harness, broker = rig
    broker.mode = "typed"
    with pytest.raises(OrderMutationError):
        harness.service.cancel("intent-1", now=FIXED_NOW)
    outcome = harness.tracker.ingest(
        _update(OrderLifecycle.FILLED, "1", "0"), current_account_id=PAPER_ACCOUNT
    )
    assert outcome.lifecycle_changed is True
    assert _status(harness) is OrderLifecycle.FILLED
