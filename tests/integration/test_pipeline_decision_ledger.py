"""Wire paperops decision ledger into the real OrderManagementService path.

Drives propose → preview → confirm → submit (and risk refusal) with a fake
broker and an injected DecisionLedger. Proves audit rows are written, verify
succeeds, secrets are absent, and live remains blocked.
"""

from __future__ import annotations

import json
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
from chronos.config.settings import Settings
from chronos.domain.enums import IBEnvironment, OrderIntent, ProductFamily, ReconciliationStatus
from chronos.domain.models import AccountSummary
from chronos.orders.intent import build_option_intent
from chronos.orders.mutations import OrderCancellationService, OrderModificationService
from chronos.orders.preview import OrderPreviewService
from chronos.orders.reconciliation_recovery import OrderRestartReconciler
from chronos.orders.risk import OrderRiskEngine, RiskEvidence
from chronos.orders.service import OrderManagementService
from chronos.orders.submission import OrderSubmissionBoundary, SubmissionRefusalCode
from chronos.orders.tracker import OrderTracker
from chronos.paperops.ledger import DecisionLedger, verify_decision_ledger
from chronos.paperops.replay import replay_ledger
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
    def __init__(self, broker: FakeBroker, settings: Settings) -> None:
        self._broker = broker
        self._settings = settings

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


class _LedgerHarness:
    def __init__(self, ledger_path: Path) -> None:
        self.ledger_path = ledger_path
        self.ledger = DecisionLedger(ledger_path)
        self.broker = FakeBroker()
        self.settings = paper_settings()
        self.db = Database("sqlite:///:memory:")
        self.db.initialize()
        self.db.bind_scope(broker_mode="ibkr", environment="paper", account_id=PAPER_ACCOUNT)
        self.connection = BrokerConnectionManager(self.broker)
        self.connection.start()
        intents = OrderIntentRepository(self.db.sessions)
        tracker_repo = OrderTrackerRepository(self.db.sessions)
        confirmations = OrderConfirmationRepository(self.db.sessions)
        risk_decisions = RiskDecisionRepository(self.db.sessions)
        tracker = OrderTracker(intents, tracker_repo)
        boundary = OrderSubmissionBoundary(
            settings=self.settings,
            connection=self.connection,
            intents=intents,
            confirmations=confirmations,
            tracker=tracker_repo,
        )
        evidence = _CannedEvidence(self.broker, self.settings)

        self.service = OrderManagementService(
            settings=self.settings,
            environment=IBEnvironment.PAPER,
            account_id=PAPER_ACCOUNT,
            evidence_provider=evidence,
            risk_engine=OrderRiskEngine(self.settings),
            preview_service=OrderPreviewService(self.connection),
            submission_boundary=boundary,
            modification=OrderModificationService(
                connection=self.connection,
                intents=intents,
                tracker=tracker,
                tracker_repo=tracker_repo,
            ),
            cancellation=OrderCancellationService(
                connection=self.connection,
                intents=intents,
                tracker=tracker,
                tracker_repo=tracker_repo,
            ),
            tracker=tracker,
            tracker_repo=tracker_repo,
            reconciler=OrderRestartReconciler(
                connection=self.connection, intents=intents, tracker=tracker
            ),
            intents=intents,
            confirmations=confirmations,
            risk_decisions=risk_decisions,
            broker_environment_is_paper=True,
            decision_ledger=self.ledger,
        )
        assert self.service.decision_ledger_enabled is True

    def close(self) -> None:
        self.connection.close()
        self.db.dispose()


@pytest.fixture
def ledger_harness(tmp_path: Path) -> Iterator[_LedgerHarness]:
    h = _LedgerHarness(tmp_path / "pipeline_decisions.jsonl")
    try:
        yield h
    finally:
        h.close()


def _intent(intent_id: str = "intent-ledger-1") -> object:
    return build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=option_contract(),
        quantity=1,
        limit_price=Decimal("1.20"),
        correlation_id="CHR-ORD-" + "B" * 32,
        intent_id=intent_id,
    )


def test_happy_path_writes_propose_and_submit_ledger_rows(ledger_harness: _LedgerHarness) -> None:
    h = ledger_harness
    intent = _intent()
    proposal = h.service.propose(intent, now=FIXED_NOW)  # type: ignore[arg-type]
    assert proposal.risk.approved
    h.service.preview(intent, now=FIXED_NOW)  # type: ignore[arg-type]
    h.service.confirm(
        intent,  # type: ignore[arg-type]
        risk_decision_id=proposal.risk.decision_id,
        now=FIXED_NOW,
    )
    outcome = h.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
    assert outcome.submitted is True

    ok, detail = verify_decision_ledger(h.ledger_path)
    assert ok, detail
    records = h.ledger.read_all()
    assert len(records) >= 2
    stages = [r.payload.get("pipeline_stage") for r in records]
    assert "propose" in stages
    assert "submit" in stages

    propose_rows = [r for r in records if r.payload.get("pipeline_stage") == "propose"]
    assert propose_rows
    p0 = propose_rows[0]
    assert p0.strategy_version == "unknown"
    assert p0.config_hash
    assert p0.data_source
    assert p0.data_timestamp_utc
    assert p0.reason_code
    assert p0.payload.get("order_fingerprint") or p0.payload.get("effective_order_fingerprint")

    submit_rows = [r for r in records if r.payload.get("pipeline_stage") == "submit"]
    assert submit_rows and submit_rows[0].outcome == "allow"
    assert submit_rows[0].payload.get("submitted") is True

    # Secrets must not appear in the ledger file.
    text = h.ledger_path.read_text(encoding="utf-8")
    for pattern in (
        r"password",
        r"api_key",
        r"Bearer\s",
        r"sk-[a-zA-Z0-9]{10,}",
        r"client_secret",
    ):
        assert re.search(pattern, text, re.I) is None, f"secret-like pattern {pattern!r} in ledger"

    # Raw account id should not be dumped (we only flag presence).
    assert PAPER_ACCOUNT not in text


def test_risk_refusal_records_deny_with_reason_code(tmp_path: Path) -> None:
    # 5-contract order breaches MAX_CONTRACTS_PER_ORDER (same as order_pipeline).
    h = _LedgerHarness(tmp_path / "refuse.jsonl")
    try:
        intent = build_option_intent(
            account_id=PAPER_ACCOUNT,
            intent=OrderIntent.OPEN_SHORT_PUT,
            contract=option_contract(),
            quantity=5,
            limit_price=Decimal("1.20"),
            correlation_id="CHR-ORD-" + "C" * 32,
            intent_id="intent-refuse-1",
        )
        proposal = h.service.propose(intent, now=FIXED_NOW)
        assert proposal.risk.approved is False

        ok, detail = verify_decision_ledger(h.ledger_path)
        assert ok, detail
        records = h.ledger.read_all()
        assert len(records) >= 1
        row = records[0]
        assert row.outcome == "deny"
        assert row.reason_code  # stable reason present
        assert row.payload.get("pipeline_stage") == "propose"
        assert row.payload.get("may_open") is False or row.outcome == "deny"
        assert "RISK" in row.reason_code or row.reason_code == "RISK_DENIED"
    finally:
        h.close()


def test_submit_refusal_recorded(ledger_harness: _LedgerHarness) -> None:
    h = ledger_harness
    intent = _intent("intent-readonly-1")
    proposal = h.service.propose(intent, now=FIXED_NOW)  # type: ignore[arg-type]
    h.service.preview(intent, now=FIXED_NOW)  # type: ignore[arg-type]
    h.service.confirm(
        intent,  # type: ignore[arg-type]
        risk_decision_id=proposal.risk.decision_id,
        now=FIXED_NOW,
    )
    outcome = h.service.submit(intent, writer_lease_held=False, now=FIXED_NOW)  # type: ignore[arg-type]
    assert outcome.submitted is False
    assert outcome.refusal is SubmissionRefusalCode.READ_ONLY_LEASE

    records = h.ledger.read_all()
    submit_rows = [r for r in records if r.payload.get("pipeline_stage") == "submit"]
    assert submit_rows
    assert submit_rows[0].outcome == "deny"
    assert submit_rows[0].payload.get("submitted") is False
    assert submit_rows[0].reason_code


def test_operator_review_and_verify_on_fixture(ledger_harness: _LedgerHarness) -> None:
    h = ledger_harness
    intent = _intent("intent-review-1")
    proposal = h.service.propose(intent, now=FIXED_NOW)  # type: ignore[arg-type]
    h.service.preview(intent, now=FIXED_NOW)  # type: ignore[arg-type]
    h.service.confirm(
        intent,  # type: ignore[arg-type]
        risk_decision_id=proposal.risk.decision_id,
        now=FIXED_NOW,
    )
    h.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]

    review = build_operator_review(h.ledger_path)
    text = review.render()
    assert review.ledger_ok is True
    assert "considered" in text.lower() or review.considered >= 1
    assert review.live_trading_blocked is True
    assert "LIVE TRADING BLOCKED" in text

    # Replay: propose rows are replayable; submit stage may not fully re-eval.
    # verify is the hard gate for pipeline fixtures.
    ok, detail = verify_decision_ledger(h.ledger_path)
    assert ok, detail
    # Replay should not crash; match on propose-shaped rows is best-effort.
    report = replay_ledger(h.ledger_path)
    assert report.reason_code.value in {
        "REPLAY_MATCH",
        "REPLAY_MISMATCH",
        "LEDGER_INCOMPLETE",
    }
    # At least the chain is valid JSON lines.
    for line in h.ledger_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            json.loads(line)


def test_live_still_blocked_under_pipeline_settings(ledger_harness: _LedgerHarness) -> None:
    from chronos.orders.live_block import LIVE_TRADING_BLOCKED, assert_live_trading_blocked

    decision = assert_live_trading_blocked(ledger_harness.settings)
    assert decision.outcome == LIVE_TRADING_BLOCKED
    assert ledger_harness.settings.live_transmission_possible is False
    assert ledger_harness.settings.allow_live_trading is False
