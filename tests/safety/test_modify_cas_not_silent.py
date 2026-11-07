"""REVIEW-TIDY P3-3: a modify whose status moved in flight is never silently unrecorded.

The broker call has already re-priced the order when the MODIFIED event is written. If the
status moved meanwhile, the old code wrote nothing, returned as if all went well and left one
warning. A benign duplicate event key also produced that warning ("status moved"), which was
untrue there. Now: a duplicate is told apart, a non-terminal move is retried once from the
current status, and a terminal move raises (the route turns it into a 409), exactly as the
cancel completion already does for a contradicting terminal status.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from chronos.domain.enums import OrderLifecycle
from chronos.orders.mutations import OrderModificationService, OrderMutationError
from chronos.orders.tracker import OrderTracker
from tests.safety.test_terminal_status_not_overwritten import (
    _ACCOUNT,
    _Connection,
    _stack,
    _submitted,
)

_NOW = datetime(2026, 7, 16, 14, 0, tzinfo=UTC)


class _Broker:
    def __init__(self, during: Any = None) -> None:
        self._during = during

    def modify_order(self, modification: Any) -> Any:
        from chronos.domain.models import OrderSubmission

        if self._during is not None:
            self._during()
        return OrderSubmission(
            correlation_id=modification.correlation_id,
            broker_order_id=modification.broker_order_id,
            client_id=17,
            lifecycle=OrderLifecycle.SUBMITTED,
            submitted_at=_NOW,
        )


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.events.append(str(getattr(record, "event", record.getMessage())))


@contextmanager
def _captured() -> Iterator[_Capture]:
    handler = _Capture()
    logger = logging.getLogger("chronos.orders.mutations")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def _service(intents: Any, tracker_repo: Any, broker: _Broker) -> OrderModificationService:
    return OrderModificationService(
        connection=_Connection(broker),  # type: ignore[arg-type]
        intents=intents,
        tracker=OrderTracker(intents, tracker_repo),
        tracker_repo=tracker_repo,
    )


def _sources(tracker_repo: Any, intent: Any) -> list[str]:
    return [e.source for e in tracker_repo.events(intent.intent_id, current_account_id=_ACCOUNT)]


def _move(tracker_repo: Any, intent: Any, to: OrderLifecycle, figure: str) -> Any:
    def land() -> None:
        assert tracker_repo.record_transition(
            intent_id=intent.intent_id,
            event_key=f"{intent.intent_id}:7:{to.value}:{figure}",
            source="CALLBACK",
            from_status=OrderLifecycle.SUBMITTED,
            to_status=to,
            current_account_id=_ACCOUNT,
            broker_order_id=7,
            filled_quantity=Decimal(figure),
            occurred_at=_NOW,
        )

    return land


def test_a_terminal_move_in_flight_raises_and_records_nothing_over_it() -> None:
    intents, tracker_repo, intent = _stack()
    _submitted(intents, tracker_repo, intent)
    broker = _Broker(_move(tracker_repo, intent, OrderLifecycle.FILLED, "10"))
    service = _service(intents, tracker_repo, broker)
    with pytest.raises(OrderMutationError, match=r"FILLED.*not recorded|not recorded.*FILLED"):
        service.modify(intent.intent_id, Decimal("399.00"), current_account_id=_ACCOUNT, now=_NOW)
    stored = intents.get(intent.intent_id, current_account_id=_ACCOUNT)
    assert stored is not None and stored.status is OrderLifecycle.FILLED
    assert "MODIFY" not in _sources(tracker_repo, intent)


def test_a_non_terminal_move_in_flight_is_recorded_from_the_current_status() -> None:
    intents, tracker_repo, intent = _stack()
    _submitted(intents, tracker_repo, intent)
    broker = _Broker(_move(tracker_repo, intent, OrderLifecycle.PARTIALLY_FILLED, "2"))
    service = _service(intents, tracker_repo, broker)
    service.modify(intent.intent_id, Decimal("399.00"), current_account_id=_ACCOUNT, now=_NOW)
    stored = intents.get(intent.intent_id, current_account_id=_ACCOUNT)
    assert stored is not None and stored.status is OrderLifecycle.PARTIALLY_FILLED
    events = tracker_repo.events(intent.intent_id, current_account_id=_ACCOUNT)
    modify = [e for e in events if e.source == "MODIFY"]
    assert len(modify) == 1
    assert modify[0].from_status is OrderLifecycle.PARTIALLY_FILLED
    assert modify[0].to_status is OrderLifecycle.PARTIALLY_FILLED
    assert modify[0].evidence["new_limit_price"] == "399.00"


def test_a_repeated_identical_modify_is_a_duplicate_not_a_moved_status() -> None:
    intents, tracker_repo, intent = _stack()
    _submitted(intents, tracker_repo, intent)
    service = _service(intents, tracker_repo, _Broker())
    service.modify(intent.intent_id, Decimal("399.00"), current_account_id=_ACCOUNT, now=_NOW)
    with _captured() as seen:
        service.modify(intent.intent_id, Decimal("399.00"), current_account_id=_ACCOUNT, now=_NOW)
    assert _sources(tracker_repo, intent).count("MODIFY") == 1
    assert "modify_event_duplicate" in seen.events
    assert "modify_status_moved" not in seen.events


def test_an_ordinary_modify_records_one_event_and_logs_nothing_odd() -> None:
    intents, tracker_repo, intent = _stack()
    _submitted(intents, tracker_repo, intent)
    service = _service(intents, tracker_repo, _Broker())
    with _captured() as seen:
        service.modify(intent.intent_id, Decimal("398.50"), current_account_id=_ACCOUNT, now=_NOW)
    assert _sources(tracker_repo, intent) == ["SUBMIT", "MODIFY"]
    assert seen.events == []
