"""OrderTracker fill_audit sink is invoked on real fill transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from chronos.domain.enums import OrderLifecycle, OrderSide, ProductFamily
from chronos.orders.tracker import OrderStatusUpdate, OrderTracker
from chronos.persistence.database import Database
from chronos.persistence.order_repositories import (
    OrderIntentRecord,
    OrderIntentRepository,
    OrderTrackerRepository,
)
from chronos.utils.identifiers import account_fingerprint

NOW = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)
ACCOUNT = "DU1234567"


def _seed_submitted_intent(db: Database) -> None:
    intents = OrderIntentRepository(db.sessions)
    record = OrderIntentRecord(
        intent_id="intent-audit-1",
        idempotency_key="idem-1",
        account_fingerprint=account_fingerprint(ACCOUNT),
        environment="paper",
        product_family=ProductFamily.OPTION,
        wheel_cycle_id=None,
        symbol="AAPL",
        con_id=111,
        local_symbol="AAPL",
        action=OrderSide.SELL,
        open_close_effect="OPEN",
        quantity=Decimal("1"),
        order_type="LMT",
        limit_price=Decimal("1.20"),
        time_in_force="DAY",
        outside_rth=False,
        quote_snapshot_id=None,
        risk_snapshot_id="risk-1",
        preview_id="prv-1",
        confirmation_hash="hash",
        order_ref="CHR-ORD-X",
        status=OrderLifecycle.SUBMITTED,
        created_at=NOW,
        confirmed_at=NOW,
        submitted_at=NOW,
        expires_at=NOW,
    )
    intents.create(record, current_account_id=ACCOUNT)


def test_fill_audit_called_on_partial_and_filled() -> None:
    db = Database("sqlite:///:memory:")
    db.initialize()
    db.bind_scope(broker_mode="ibkr", environment="paper", account_id=ACCOUNT)
    _seed_submitted_intent(db)
    intents = OrderIntentRepository(db.sessions)
    tracker_repo = OrderTrackerRepository(db.sessions)
    seen: list[tuple[str, str]] = []

    def audit(intent: OrderIntentRecord, update: OrderStatusUpdate, *, applied: bool) -> None:
        assert applied is True
        seen.append((intent.intent_id, update.lifecycle.value))

    tracker = OrderTracker(intents, tracker_repo, fill_audit=audit)
    assert tracker.ingest(
        OrderStatusUpdate(
            intent_id="intent-audit-1",
            broker_order_id=1,
            lifecycle=OrderLifecycle.PARTIALLY_FILLED,
            filled_quantity=Decimal("1"),
            remaining_quantity=Decimal("1"),
            occurred_at=NOW,
        ),
        current_account_id=ACCOUNT,
    )
    assert tracker.ingest(
        OrderStatusUpdate(
            intent_id="intent-audit-1",
            broker_order_id=1,
            lifecycle=OrderLifecycle.FILLED,
            filled_quantity=Decimal("1"),
            remaining_quantity=Decimal("0"),
            occurred_at=NOW,
        ),
        current_account_id=ACCOUNT,
    )
    assert seen == [
        ("intent-audit-1", "PARTIALLY_FILLED"),
        ("intent-audit-1", "FILLED"),
    ]
    db.dispose()


def test_bind_fill_audit_late() -> None:
    db = Database("sqlite:///:memory:")
    db.initialize()
    db.bind_scope(broker_mode="ibkr", environment="paper", account_id=ACCOUNT)
    _seed_submitted_intent(db)
    intents = OrderIntentRepository(db.sessions)
    tracker_repo = OrderTrackerRepository(db.sessions)
    tracker = OrderTracker(intents, tracker_repo)
    hits: list[str] = []
    tracker.bind_fill_audit(lambda intent, update, *, applied: hits.append(update.lifecycle.value))
    tracker.ingest(
        OrderStatusUpdate(
            intent_id="intent-audit-1",
            broker_order_id=2,
            lifecycle=OrderLifecycle.FILLED,
            filled_quantity=Decimal("1"),
            remaining_quantity=Decimal("0"),
            occurred_at=NOW,
        ),
        current_account_id=ACCOUNT,
    )
    assert hits == ["FILLED"]
    db.dispose()
