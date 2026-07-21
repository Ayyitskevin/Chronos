"""Production-path decision ledger: open_paper_decision_ledger + OMS + fills.

Drives the same composition runtime uses (bootstrap → OrderManagementService
with decision_ledger + tracker fill audit) without requiring a live TWS.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from tests.support.order_fakes import (
    FIXED_NOW,
    PAPER_ACCOUNT,
    FakeBroker,
    option_contract,
    paper_settings,
)

from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.enums import (
    IBEnvironment,
    OrderIntent,
    OrderLifecycle,
    ProductFamily,
    ReconciliationStatus,
)
from chronos.domain.models import AccountSummary
from chronos.orders.intent import build_option_intent
from chronos.orders.mutations import OrderCancellationService, OrderModificationService
from chronos.orders.preview import OrderPreviewService
from chronos.orders.reconciliation_recovery import OrderRestartReconciler
from chronos.orders.risk import OrderRiskEngine, RiskEvidence
from chronos.orders.service import OrderManagementService
from chronos.orders.submission import OrderSubmissionBoundary
from chronos.orders.tracker import OrderStatusUpdate, OrderTracker
from chronos.paperops.bootstrap import open_paper_decision_ledger
from chronos.paperops.ledger import DecisionLedger, verify_decision_ledger
from chronos.paperops.review import build_operator_review
from chronos.persistence.database import Database
from chronos.persistence.order_repositories import (
    OrderConfirmationRepository,
    OrderIntentRepository,
    OrderTrackerRepository,
    RiskDecisionRepository,
)
from chronos.services.trading_hours import session_for


class _CannedEvidence:
    def gather(self, intent: object, *, now: datetime) -> RiskEvidence:
        del intent
        return RiskEvidence(
            account=AccountSummary(
                account_id=PAPER_ACCOUNT,
                net_liquidation=Decimal("100000"),
                total_cash=Decimal("80000"),
                buying_power=Decimal("160000"),
                as_of=now,
            ),
            reconciliation_status=ReconciliationStatus.RECONCILED,
            session=session_for(ProductFamily.OPTION, now=now, broker_confirms_open=True),
            wheel_eligible_action=OrderIntent.OPEN_SHORT_PUT,
        )


def _build_oms_like_runtime(
    ledger_path: Path,
) -> tuple[OrderManagementService, FakeBroker, Database]:
    """Mirror runtime._build_order_management ledger composition with fakes."""

    settings = paper_settings(
        enable_paper_decision_ledger=True,
        paper_decision_ledger_file=ledger_path,
    )
    # Production bootstrap entry used by runtime.
    ledger = open_paper_decision_ledger(settings)
    assert ledger is not None

    broker = FakeBroker()
    db = Database("sqlite:///:memory:")
    db.initialize()
    db.bind_scope(broker_mode="ibkr", environment="paper", account_id=PAPER_ACCOUNT)
    connection = BrokerConnectionManager(broker)
    connection.start()
    intents = OrderIntentRepository(db.sessions)
    tracker_repo = OrderTrackerRepository(db.sessions)
    confirmations = OrderConfirmationRepository(db.sessions)
    risk_decisions = RiskDecisionRepository(db.sessions)
    tracker = OrderTracker(intents, tracker_repo)
    boundary = OrderSubmissionBoundary(
        settings=settings,
        connection=connection,
        intents=intents,
        confirmations=confirmations,
        tracker=tracker_repo,
    )
    service = OrderManagementService(
        settings=settings,
        environment=IBEnvironment.PAPER,
        account_id=PAPER_ACCOUNT,
        evidence_provider=_CannedEvidence(),
        risk_engine=OrderRiskEngine(settings),
        preview_service=OrderPreviewService(connection),
        submission_boundary=boundary,
        modification=OrderModificationService(
            connection=connection,
            intents=intents,
            tracker=tracker,
            tracker_repo=tracker_repo,
        ),
        cancellation=OrderCancellationService(
            connection=connection,
            intents=intents,
            tracker=tracker,
            tracker_repo=tracker_repo,
        ),
        tracker=tracker,
        tracker_repo=tracker_repo,
        reconciler=OrderRestartReconciler(connection=connection, intents=intents, tracker=tracker),
        intents=intents,
        confirmations=confirmations,
        risk_decisions=risk_decisions,
        broker_environment_is_paper=True,
        decision_ledger=ledger,
    )
    assert service.decision_ledger_enabled is True
    return service, broker, db


@pytest.fixture
def runtime_like(tmp_path: Path) -> Iterator[tuple[OrderManagementService, FakeBroker, Path]]:
    path = tmp_path / "data" / "paper_decision_ledger.jsonl"
    service, broker, db = _build_oms_like_runtime(path)
    try:
        yield service, broker, path
    finally:
        db.dispose()


def test_runtime_bootstrap_enables_ledger_and_records_propose_submit(
    runtime_like: tuple[OrderManagementService, FakeBroker, Path],
) -> None:
    service, broker, path = runtime_like
    intent = build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=option_contract(),
        quantity=1,
        limit_price=Decimal("1.20"),
        correlation_id="CHR-ORD-" + "F" * 32,
        intent_id="intent-runtime-1",
    )
    proposal = service.propose(intent, now=FIXED_NOW)
    assert proposal.risk.approved
    service.preview(intent, now=FIXED_NOW)
    service.confirm(intent, risk_decision_id=proposal.risk.decision_id, now=FIXED_NOW)
    outcome = service.submit(intent, writer_lease_held=True, now=FIXED_NOW)
    assert outcome.submitted is True
    assert len(broker.submit_calls) == 1

    ok, detail = verify_decision_ledger(path)
    assert ok, detail
    text = path.read_text(encoding="utf-8")
    assert "propose" in text
    assert "submit" in text
    assert "RISK_APPROVED" in text or "ORDER_PROPOSED" in text
    assert PAPER_ACCOUNT not in text
    assert re.search(r"password|api_key|Bearer\s|client_secret", text, re.I) is None


def test_tracker_fill_and_partial_append_ledger_rows(
    runtime_like: tuple[OrderManagementService, FakeBroker, Path],
) -> None:
    service, _broker, path = runtime_like
    intent = build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=option_contract(),
        quantity=1,
        limit_price=Decimal("1.20"),
        correlation_id="CHR-ORD-" + "G" * 32,
        intent_id="intent-fill-1",
    )
    proposal = service.propose(intent, now=FIXED_NOW)
    service.preview(intent, now=FIXED_NOW)
    service.confirm(intent, risk_decision_id=proposal.risk.decision_id, now=FIXED_NOW)
    service.submit(intent, writer_lease_held=True, now=FIXED_NOW)

    # Partial then full fill via real tracker.ingest (production fill path).
    applied_partial = service._tracker.ingest(
        OrderStatusUpdate(
            intent_id="intent-fill-1",
            broker_order_id=9001,
            lifecycle=OrderLifecycle.PARTIALLY_FILLED,
            filled_quantity=Decimal("1"),
            remaining_quantity=Decimal("1"),
            occurred_at=FIXED_NOW,
        ),
        current_account_id=PAPER_ACCOUNT,
    )
    assert applied_partial is True

    applied_full = service._tracker.ingest(
        OrderStatusUpdate(
            intent_id="intent-fill-1",
            broker_order_id=9001,
            lifecycle=OrderLifecycle.FILLED,
            filled_quantity=Decimal("1"),
            remaining_quantity=Decimal("0"),
            occurred_at=FIXED_NOW,
        ),
        current_account_id=PAPER_ACCOUNT,
    )
    assert applied_full is True

    ok, detail = verify_decision_ledger(path)
    assert ok, detail
    text = path.read_text(encoding="utf-8")
    assert '"pipeline_stage":"fill"' in text or '"pipeline_stage": "fill"' in text
    assert "FILL_RECORDED" in text
    assert "PARTIALLY_FILLED" in text or "FILLED" in text

    fill_rows = [
        record
        for record in DecisionLedger(path).read_all()
        if record.payload.get("pipeline_stage") == "fill"
    ]
    assert fill_rows
    assert all(record.data_source == "order_pipeline" for record in fill_rows)
    assert all(record.data_quality_label == "N/A" for record in fill_rows)

    review = build_operator_review(path)
    assert review.ledger_ok is True
    assert review.live_trading_blocked is True
    rendered = review.render()
    assert "LIVE TRADING BLOCKED" in rendered


def test_open_paper_decision_ledger_is_runtime_entry() -> None:
    """Structural: runtime imports bootstrap.open_paper_decision_ledger."""

    import chronos.runtime as runtime_mod

    src = Path(runtime_mod.__file__).read_text(encoding="utf-8")
    assert "open_paper_decision_ledger" in src
    assert "decision_ledger=" in src or "decision_ledger =" in src
