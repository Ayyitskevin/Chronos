"""The read-only paper soak report over a seeded database."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from scripts.paper_soak_report import build_soak_report

from chronos.domain.enums import OrderLifecycle, OrderSide, ProductFamily
from chronos.persistence.database import Database
from chronos.persistence.order_repositories import (
    OrderIntentRecord,
    OrderIntentRepository,
    OrderTrackerRepository,
)
from chronos.utils.identifiers import account_fingerprint

_ACC = "DU1234567"
_NOW = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)


def _record(intent_id: str, key: str) -> OrderIntentRecord:
    return OrderIntentRecord(
        intent_id=intent_id,
        idempotency_key=key,
        account_fingerprint=account_fingerprint(_ACC),
        environment="paper",
        product_family=ProductFamily.OPTION,
        wheel_cycle_id=None,
        symbol="AAPL",
        con_id=111,
        local_symbol="AAPL  260821P00150000",
        action=OrderSide.SELL,
        open_close_effect="OPEN",
        quantity=Decimal("1"),
        order_type="LMT",
        limit_price=Decimal("1.20"),
        time_in_force="DAY",
        outside_rth=False,
        quote_snapshot_id=None,
        risk_snapshot_id=None,
        preview_id=None,
        confirmation_hash=None,
        order_ref=f"CHR-ORD-{intent_id}",
        status=OrderLifecycle.SUBMITTED,
        created_at=_NOW,
        confirmed_at=None,
        submitted_at=None,
        expires_at=None,
    )


def test_soak_report_counts_intents_events_and_resolutions() -> None:
    db = Database("sqlite:///:memory:")
    db.initialize()
    db.bind_scope(broker_mode="ibkr", environment="paper", account_id=_ACC)
    intents = OrderIntentRepository(db.sessions)
    tracker = OrderTrackerRepository(db.sessions)
    intents.create(_record("a", "key-a"), current_account_id=_ACC)
    tracker.record_transition(
        intent_id="a",
        event_key="a:submit",
        source="SUBMIT",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=OrderLifecycle.SUBMITTED,
        current_account_id=_ACC,
        occurred_at=_NOW,
    )
    # A SUBMISSION_UNKNOWN resolved by reconciliation.
    tracker.record_transition(
        intent_id="a",
        event_key="a:reconcile",
        source="RECONCILE",
        from_status=OrderLifecycle.SUBMISSION_UNKNOWN,
        to_status=OrderLifecycle.FILLED,
        current_account_id=_ACC,
        occurred_at=_NOW,
    )
    report = build_soak_report(db)
    db.dispose()

    assert report.total_intents == 1
    assert report.status_counts.get("FILLED") == 1
    assert report.event_source_counts.get("SUBMIT") == 1
    assert report.event_source_counts.get("RECONCILE") == 1
    assert report.submission_unknown_resolutions == 1
    assert "paper soak report" in report.render()
