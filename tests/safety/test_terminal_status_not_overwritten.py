"""RECHECK-355 P3-4: neither modify nor a re-propose may overwrite a terminal status.

Latent today (real adapters refuse modify; routes mint fresh ids) but both writers had no
guard: a modify whose broker call overlapped a FILLED callback put the status back to
SUBMITTED, and ``set_status`` would turn a FILLED intent into REJECTED with no event row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from chronos.domain.enums import IBEnvironment, OrderLifecycle
from chronos.domain.models import OrderSubmission
from chronos.orders.mutations import OrderModificationService, OrderMutationError
from chronos.orders.tracker import OrderTracker
from chronos.persistence.order_repositories import OrderIntentRepository, OrderTrackerRepository
from tests.safety.test_opening_cap_exercised import _ACCOUNT, _MORNING, _database, _intent
from tests.support.persistence_keys import TEST_PERSISTENCE_KEY

_NOW = datetime(2026, 7, 16, 14, 0, tzinfo=UTC)


def _stack() -> tuple[OrderIntentRepository, OrderTrackerRepository, Any]:
    db = _database()
    intents = OrderIntentRepository(db.sessions)
    tracker_repo = OrderTrackerRepository(db.sessions)
    intent = _intent()
    intents.create(
        intent.to_intent_record(
            environment=IBEnvironment.PAPER,
            status=OrderLifecycle.DRAFT,
            created_at=_MORNING,
            expires_at=None,
            day_bucket="2026-07-16",
            key=TEST_PERSISTENCE_KEY,
        ),
        current_account_id=_ACCOUNT,
    )
    return intents, tracker_repo, intent


def _submitted(intents: OrderIntentRepository, tracker_repo: OrderTrackerRepository, intent: Any):
    assert tracker_repo.record_transition(
        intent_id=intent.intent_id,
        event_key=f"{intent.intent_id}:7:SUBMITTED",
        source="SUBMIT",
        from_status=OrderLifecycle.DRAFT,
        to_status=OrderLifecycle.SUBMITTED,
        current_account_id=_ACCOUNT,
        broker_order_id=7,
        occurred_at=_NOW,
    )


class _Broker:
    """modify_order lands a FILLED callback WHILE the broker call is in flight."""

    def __init__(self, during: Any) -> None:
        self._during = during

    def modify_order(self, modification: Any) -> OrderSubmission:
        self._during()
        return OrderSubmission(
            correlation_id=modification.correlation_id,
            broker_order_id=modification.broker_order_id,
            client_id=17,
            lifecycle=OrderLifecycle.SUBMITTED,
            submitted_at=_NOW,
        )


class _Connection:
    def __init__(self, broker: _Broker) -> None:
        self.broker = broker

    def run(self, value: Any) -> Any:
        return value


def test_a_modify_that_overlaps_a_fill_does_not_put_the_status_back() -> None:
    intents, tracker_repo, intent = _stack()
    _submitted(intents, tracker_repo, intent)

    def fill_lands() -> None:
        assert tracker_repo.record_transition(
            intent_id=intent.intent_id,
            event_key=f"{intent.intent_id}:7:FILLED:10",
            source="CALLBACK",
            from_status=OrderLifecycle.SUBMITTED,
            to_status=OrderLifecycle.FILLED,
            current_account_id=_ACCOUNT,
            broker_order_id=7,
            filled_quantity=Decimal("10"),
            occurred_at=_NOW,
        )

    service = OrderModificationService(
        connection=_Connection(_Broker(fill_lands)),  # type: ignore[arg-type]
        intents=intents,
        tracker=OrderTracker(intents, tracker_repo),
        tracker_repo=tracker_repo,
    )
    with pytest.raises(OrderMutationError, match="not recorded"):  # REVIEW-TIDY P3-3: not silent
        service.modify(intent.intent_id, Decimal("399.00"), current_account_id=_ACCOUNT, now=_NOW)
    stored = intents.get(intent.intent_id, current_account_id=_ACCOUNT)
    assert stored is not None and stored.status is OrderLifecycle.FILLED


def test_a_modify_on_a_still_working_order_records_its_event() -> None:
    intents, tracker_repo, intent = _stack()
    _submitted(intents, tracker_repo, intent)
    service = OrderModificationService(
        connection=_Connection(_Broker(lambda: None)),  # type: ignore[arg-type]
        intents=intents,
        tracker=OrderTracker(intents, tracker_repo),
        tracker_repo=tracker_repo,
    )
    service.modify(intent.intent_id, Decimal("399.00"), current_account_id=_ACCOUNT, now=_NOW)
    sources = [e.source for e in tracker_repo.events(intent.intent_id, current_account_id=_ACCOUNT)]
    assert sources == ["SUBMIT", "MODIFY"]
    stored = intents.get(intent.intent_id, current_account_id=_ACCOUNT)
    assert stored is not None and stored.status is OrderLifecycle.SUBMITTED


@pytest.mark.parametrize(
    "terminal", [OrderLifecycle.FILLED, OrderLifecycle.CANCELLED, OrderLifecycle.REJECTED]
)
def test_set_status_refuses_to_overwrite_a_terminal_status(terminal: OrderLifecycle) -> None:
    intents, tracker_repo, intent = _stack()
    _submitted(intents, tracker_repo, intent)
    assert tracker_repo.record_transition(
        intent_id=intent.intent_id,
        event_key=f"{intent.intent_id}:7:{terminal.value}",
        source="CALLBACK",
        from_status=OrderLifecycle.SUBMITTED,
        to_status=terminal,
        current_account_id=_ACCOUNT,
        broker_order_id=7,
        occurred_at=_NOW,
    )
    with pytest.raises(ValueError, match="terminal"):
        intents.set_status(
            intent.intent_id,
            OrderLifecycle.REJECTED
            if terminal is not OrderLifecycle.REJECTED
            else OrderLifecycle.VALIDATED,
            current_account_id=_ACCOUNT,
            at=_NOW,
        )
    stored = intents.get(intent.intent_id, current_account_id=_ACCOUNT)
    assert stored is not None and stored.status is terminal


def test_set_status_still_advances_a_non_terminal_intent_and_allows_a_same_status_repeat() -> None:
    intents, _tracker_repo, intent = _stack()
    intents.set_status(
        intent.intent_id, OrderLifecycle.VALIDATED, current_account_id=_ACCOUNT, at=_NOW
    )
    intents.set_status(
        intent.intent_id, OrderLifecycle.REJECTED, current_account_id=_ACCOUNT, at=_NOW
    )
    intents.set_status(  # a terminal repeating itself is not an overwrite
        intent.intent_id, OrderLifecycle.REJECTED, current_account_id=_ACCOUNT, at=_NOW
    )
    stored = intents.get(intent.intent_id, current_account_id=_ACCOUNT)
    assert stored is not None and stored.status is OrderLifecycle.REJECTED
