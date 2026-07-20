"""Soak DB ↔ decision-ledger reconcile (honest mismatch flags)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from scripts.paper_soak_report import build_soak_report

from chronos.config.settings import Settings
from chronos.domain.enums import OrderLifecycle, OrderSide, ProductFamily
from chronos.orders.live_block import LIVE_TRADING_BLOCKED
from chronos.paperops.ledger import DecisionEvent, DecisionLedger
from chronos.paperops.reasons import DecisionKind, DecisionOutcome, PaperReasonCode
from chronos.paperops.reconcile import (
    SoakSnapshot,
    reconcile_soak_and_ledger,
    summarize_ledger_stages,
)
from chronos.persistence.database import Database
from chronos.persistence.order_repositories import (
    OrderIntentRecord,
    OrderIntentRepository,
    OrderTrackerRepository,
)
from chronos.utils.identifiers import account_fingerprint

_ACC = "DU1234567"
_NOW = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(_env_file=None, symbol_allowlist=("AAPL", "SPY"))


def _intent(intent_id: str, key: str, status: OrderLifecycle) -> OrderIntentRecord:
    return OrderIntentRecord(
        intent_id=intent_id,
        idempotency_key=key,
        account_fingerprint=account_fingerprint(_ACC),
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
        risk_snapshot_id=None,
        preview_id=None,
        confirmation_hash=None,
        order_ref=f"CHR-ORD-{intent_id}",
        status=status,
        created_at=_NOW,
        confirmed_at=None,
        submitted_at=None,
        expires_at=None,
    )


def _seed_db(*, filled: bool = False) -> Database:
    db = Database("sqlite:///:memory:")
    db.initialize()
    db.bind_scope(broker_mode="ibkr", environment="paper", account_id=_ACC)
    intents = OrderIntentRepository(db.sessions)
    tracker = OrderTrackerRepository(db.sessions)
    status = OrderLifecycle.FILLED if filled else OrderLifecycle.SUBMITTED
    intents.create(_intent("a", "key-a", status), current_account_id=_ACC)
    tracker.record_transition(
        intent_id="a",
        event_key="a:submit",
        source="SUBMIT",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=OrderLifecycle.SUBMITTED,
        current_account_id=_ACC,
        occurred_at=_NOW,
    )
    if filled:
        tracker.record_transition(
            intent_id="a",
            event_key="a:fill",
            source="ORDER_STATUS",
            from_status=OrderLifecycle.SUBMITTED,
            to_status=OrderLifecycle.FILLED,
            current_account_id=_ACC,
            occurred_at=_NOW,
            filled_quantity=Decimal("1"),
        )
    return db


def _append_stage(
    ledger: DecisionLedger,
    *,
    stage: str,
    reason: PaperReasonCode,
    outcome: DecisionOutcome,
    kind: DecisionKind,
    seq_label: str,
) -> None:
    ledger.append(
        DecisionEvent(
            kind=kind,
            reason_code=reason,
            outcome=outcome,
            strategy_id="wheel:OPEN_SHORT_PUT",
            strategy_version="unknown",
            config_hash="cfg",
            data_timestamp_utc=_NOW.isoformat(),
            data_source="paper_pipeline",
            data_quality_label="LIVE",
            decision_inputs={
                "pipeline_stage": stage,
                "now_utc": _NOW.isoformat(),
                "order_fingerprint": seq_label,
                "strategy_id": "wheel:OPEN_SHORT_PUT",
                "strategy_version": "unknown",
                "config_hash": "cfg",
            },
            payload={
                "pipeline_stage": stage,
                "order_fingerprint": seq_label,
                "may_open": outcome is DecisionOutcome.ALLOW,
            },
        ),
        at_utc=_NOW.isoformat(),
    )


def test_matching_fixture_reports_both_halves(tmp_path: Path) -> None:
    db = _seed_db(filled=True)
    soak = SoakSnapshot.from_soak_report(build_soak_report(db))
    db.dispose()

    path = tmp_path / "ledger.jsonl"
    ledger = DecisionLedger(path)
    _append_stage(
        ledger,
        stage="propose",
        reason=PaperReasonCode.RISK_APPROVED,
        outcome=DecisionOutcome.ALLOW,
        kind=DecisionKind.RISK_DECISION,
        seq_label="a",
    )
    _append_stage(
        ledger,
        stage="submit",
        reason=PaperReasonCode.ORDER_PROPOSED,
        outcome=DecisionOutcome.ALLOW,
        kind=DecisionKind.STATE_TRANSITION,
        seq_label="a",
    )
    _append_stage(
        ledger,
        stage="fill",
        reason=PaperReasonCode.FILL_RECORDED,
        outcome=DecisionOutcome.INFORMATIONAL,
        kind=DecisionKind.PAPER_FILL,
        seq_label="a",
    )

    report = reconcile_soak_and_ledger(soak=soak, ledger_path=path, settings=_settings())
    text = report.render()
    assert report.ok is True
    assert report.flags == ()
    assert report.soak.total_intents == 1
    assert report.ledger.propose_count == 1
    assert report.ledger.submit_count == 1
    assert report.ledger.fill_count == 1
    assert "order intents: 1" in text
    assert "propose=" in text
    assert "LIVE TRADING BLOCKED" in text
    assert report.live_trading_blocked is True
    assert report.live_outcome == LIVE_TRADING_BLOCKED
    # Secrets / raw account id absent from report text.
    assert _ACC not in text
    assert "password" not in text.lower()
    assert "api_key" not in text.lower()


def test_missing_ledger_fails_closed(tmp_path: Path) -> None:
    soak = SoakSnapshot(
        total_intents=2,
        status_counts={"SUBMITTED": 2},
        event_source_counts={"SUBMIT": 2},
        submission_unknown_resolutions=0,
    )
    missing = tmp_path / "nope.jsonl"
    report = reconcile_soak_and_ledger(soak=soak, ledger_path=missing, settings=_settings())
    assert report.ok is False
    assert report.ledger.chain_ok is False
    assert any(f.startswith("LEDGER_MISSING") for f in report.flags)
    assert "LEDGER_MISSING" in report.render()


def test_corrupt_ledger_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text("{not-json\n", encoding="utf-8")
    soak = SoakSnapshot(
        total_intents=1,
        status_counts={"SUBMITTED": 1},
        event_source_counts={},
        submission_unknown_resolutions=0,
    )
    report = reconcile_soak_and_ledger(soak=soak, ledger_path=path, settings=_settings())
    assert report.ok is False
    assert report.ledger.chain_ok is False
    assert any("LEDGER_CORRUPT" in f or "INCOMPLETE" in f for f in report.flags)
    # Does not invent matching stage counts.
    assert report.ledger.total_records == 0
    assert report.ledger.stage_counts == {}


def test_db_activity_without_propose_audit_flags_incompleteness(tmp_path: Path) -> None:
    db = _seed_db(filled=False)
    soak = SoakSnapshot.from_soak_report(build_soak_report(db))
    db.dispose()
    path = tmp_path / "empty_stages.jsonl"
    path.write_text("", encoding="utf-8")  # valid empty chain
    report = reconcile_soak_and_ledger(soak=soak, ledger_path=path, settings=_settings())
    assert report.ok is False
    assert any("LEDGER_MISSING_PROPOSE_AUDIT" in f for f in report.flags)


def test_db_fills_without_ledger_fills_flags(tmp_path: Path) -> None:
    db = _seed_db(filled=True)
    soak = SoakSnapshot.from_soak_report(build_soak_report(db))
    db.dispose()
    path = tmp_path / "no_fill.jsonl"
    ledger = DecisionLedger(path)
    _append_stage(
        ledger,
        stage="propose",
        reason=PaperReasonCode.RISK_APPROVED,
        outcome=DecisionOutcome.ALLOW,
        kind=DecisionKind.RISK_DECISION,
        seq_label="a",
    )
    _append_stage(
        ledger,
        stage="submit",
        reason=PaperReasonCode.ORDER_PROPOSED,
        outcome=DecisionOutcome.ALLOW,
        kind=DecisionKind.STATE_TRANSITION,
        seq_label="a",
    )
    report = reconcile_soak_and_ledger(soak=soak, ledger_path=path, settings=_settings())
    assert report.ok is False
    assert any("LEDGER_MISSING_FILL_AUDIT" in f for f in report.flags)
    # Stage counts are real, not fabricated fills.
    assert report.ledger.fill_count == 0
    assert report.ledger.propose_count == 1


def test_summarize_ledger_stages_and_to_dict(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    ledger = DecisionLedger(path)
    _append_stage(
        ledger,
        stage="propose",
        reason=PaperReasonCode.RISK_DENIED,
        outcome=DecisionOutcome.DENY,
        kind=DecisionKind.REJECTION,
        seq_label="x",
    )
    summary = summarize_ledger_stages(path)
    assert summary.chain_ok is True
    assert summary.propose_count == 1
    report = reconcile_soak_and_ledger(
        soak=SoakSnapshot(0, {}, {}, 0),
        ledger_path=path,
        settings=_settings(),
    )
    payload = report.to_dict()
    assert payload["ledger"]["stage_counts"]["propose"] == 1
    assert "flags" in payload
    # JSON serializable
    json.dumps(payload)
