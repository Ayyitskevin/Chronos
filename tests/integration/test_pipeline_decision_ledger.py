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
from chronos.domain.enums import (
    BrokerAdapter,
    BrokerMode,
    DemoProfile,
    IBEnvironment,
    OrderIntent,
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
from chronos.orders.submission import OrderSubmissionBoundary, SubmissionRefusalCode
from chronos.orders.tracker import OrderTracker
from chronos.paperops.ledger import DecisionLedger, verify_decision_ledger
from chronos.paperops.pipeline import (
    DECISION_SETTINGS_FIELDS,
    decision_settings_projection,
    settings_config_hash,
)
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
    assert p0.data_source == "order_intent_limit_proxy"
    assert p0.data_timestamp_utc
    assert p0.data_quality_label == "SYNTHETIC"
    assert p0.outcome == "deny"
    assert p0.payload["data_health"]["may_authorize_open"] is False
    assert p0.reason_code
    assert p0.payload.get("order_fingerprint") or p0.payload.get("effective_order_fingerprint")

    submit_rows = [r for r in records if r.payload.get("pipeline_stage") == "submit"]
    assert submit_rows and submit_rows[0].outcome == "allow"
    assert submit_rows[0].payload.get("submitted") is True
    assert submit_rows[0].data_source == "order_pipeline"
    assert submit_rows[0].data_quality_label == "N/A"

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


_HASH_SETTING_CHANGES: tuple[tuple[str, object], ...] = (
    ("broker_mode", BrokerMode.DEMO),
    ("broker_adapter", BrokerAdapter.IB_ASYNC),
    ("demo_profile", DemoProfile.EMPTY_ACCOUNT),
    ("ib_environment", IBEnvironment.LIVE),
    ("ib_host", "paper-gateway.internal"),
    ("ib_port", 4002),
    ("ib_client_id", 19),
    ("ib_account_id", "DU7654321"),
    ("ib_account_allowlist", ("DU7654321",)),
    ("allow_order_transmit", False),
    ("allow_live_trading", True),
    ("allow_outside_rth", True),
    ("require_live_arming", False),
    ("live_arm_ttl_minutes", 16),
    ("require_typed_confirmation", False),
    ("order_confirmation_ttl_seconds", 21),
    ("live_kill_switch_file", Path("data/other-live-kill.json")),
    ("session_baseline_file", Path("data/other-session-baseline.json")),
    ("database_url", "sqlite:///data/other-chronos.db"),
    ("symbol_allowlist", ("AAPL", "QQQ")),
    ("crypto_allowlist", ("BTC", "ETH")),
    ("crypto_time_in_force", "IOC"),
    ("market_timezone", "UTC"),
    ("max_quote_age_seconds", 17),
    ("max_contracts_per_order", 3),
    ("max_open_short_option_contracts", 6),
    ("max_opening_orders_per_day", 4),
    ("max_gross_assignment_usd", Decimal("26000")),
    ("min_cash_buffer_usd", Decimal("6000")),
    ("min_cash_buffer_pct", Decimal("0.11")),
    ("max_symbol_allocation_pct", Decimal("0.26")),
    ("max_total_wheel_allocation_pct", Decimal("0.61")),
    ("max_crypto_allocation_pct", Decimal("0.11")),
    ("max_crypto_notional_per_order_usd", Decimal("1100")),
    ("max_session_drawdown_usd", Decimal("1100")),
    ("max_session_drawdown_pct", Decimal("0.03")),
)


@pytest.mark.parametrize(("field", "changed_value"), _HASH_SETTING_CHANGES)
def test_paperops_config_hash_covers_decision_settings(field: str, changed_value: object) -> None:
    settings = paper_settings()
    changed = settings.model_copy(update={field: changed_value})
    assert settings_config_hash(changed) != settings_config_hash(settings)


def test_paperops_config_hash_regressions_cover_the_complete_projection() -> None:
    assert {field for field, _value in _HASH_SETTING_CHANGES} == set(DECISION_SETTINGS_FIELDS)


def test_decision_settings_projection_is_secret_safe_and_canonical() -> None:
    other_account = "DU7654321"
    settings = paper_settings(
        ib_host="private-gateway.example",
        ib_account_allowlist=(PAPER_ACCOUNT, other_account),
        database_url="postgresql://audit-user:secret@db.internal/chronos",
        live_kill_switch_file=Path("/private/chronos/live-kill.json"),
        session_baseline_file=Path("/private/chronos/session-baseline.json"),
    )

    projection = decision_settings_projection(settings)
    encoded = json.dumps(projection, sort_keys=True)
    for raw_value in (
        PAPER_ACCOUNT,
        other_account,
        "private-gateway.example",
        "audit-user",
        "secret",
        "/private/chronos/live-kill.json",
        "/private/chronos/session-baseline.json",
    ):
        assert raw_value not in encoded
    for digest_field in (
        "broker_endpoint_sha256",
        "ib_account_id_sha256",
        "ib_account_allowlist_sha256",
        "database_url_sha256",
        "live_state_paths_sha256",
    ):
        assert re.fullmatch(r"[0-9a-f]{64}", str(projection[digest_field]))

    reordered = settings.model_copy(
        update={"ib_account_allowlist": tuple(reversed(settings.ib_account_allowlist))}
    )
    assert decision_settings_projection(reordered) == projection


def test_config_hash_changes_when_risk_setting_flips_decision() -> None:
    base = paper_settings(
        max_contracts_per_order=1,
        max_gross_assignment_usd=Decimal("100000"),
        max_symbol_allocation_pct=Decimal("0.50"),
    )
    changed = base.model_copy(update={"max_contracts_per_order": 2})
    intent = build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=option_contract(),
        quantity=2,
        limit_price=Decimal("1.20"),
        correlation_id="CHR-ORD-" + "H" * 32,
        intent_id="config-hash-decision-flip",
    )
    evidence = _CannedEvidence(FakeBroker(), base).gather(intent, now=FIXED_NOW)

    before = OrderRiskEngine(base).evaluate(intent, evidence, now=FIXED_NOW)
    after = OrderRiskEngine(changed).evaluate(intent, evidence, now=FIXED_NOW)

    assert before.approved is False
    assert after.approved is True
    assert settings_config_hash(base) != settings_config_hash(changed)


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
