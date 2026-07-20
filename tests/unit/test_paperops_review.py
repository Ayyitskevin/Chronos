"""Operator review surface over paper decision ledger."""

from __future__ import annotations

from pathlib import Path

from chronos.orders.live_block import LIVE_TRADING_BLOCKED
from chronos.paperops.decision import PaperDecisionInput
from chronos.paperops.ledger import DecisionLedger
from chronos.paperops.review import build_operator_review
from chronos.paperops.session import record_paper_decision, record_session_marker


def _input(**overrides: object) -> PaperDecisionInput:
    base: dict[str, object] = {
        "strategy_id": "regime_trend_v1",
        "strategy_version": "1.0.0",
        "config_hash": "cfg",
        "symbol": "SPY",
        "side": "BUY",
        "quantity": 1,
        "limit_price": 100.0,
        "bid": 99.5,
        "ask": 100.5,
        "last": 100.0,
        "quote_utc": "2024-06-03T15:00:00+00:00",
        "data_source": "fixture",
        "quality_label": "LIVE",
        "max_quote_age_seconds": 30.0,
        "account_equity_usd": 10000.0,
        "max_aggregate_exposure_usd": 5000.0,
        "max_symbol_exposure_fraction": 0.5,
        "max_simultaneous_positions": 3,
        "max_daily_loss_usd": 500.0,
        "now_utc": "2024-06-03T15:00:05+00:00",
        "order_fingerprint": "fp-1",
        "risk_approved": True,
    }
    base.update(overrides)
    return PaperDecisionInput(**base)  # type: ignore[arg-type]


def test_operator_review_names_considered_rejected_risk_data(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = DecisionLedger(path)
    record_session_marker(
        ledger,
        strategy_id="regime_trend_v1",
        strategy_version="1.0.0",
        config_hash="cfg",
        note="session start",
        at_utc="2024-06-03T14:59:00+00:00",
    )
    record_paper_decision(ledger, _input(), at_utc="2024-06-03T15:00:06+00:00")
    record_paper_decision(
        ledger,
        _input(order_fingerprint="fp-2", quality_label="DEMO", data_source="demo_broker"),
        at_utc="2024-06-03T15:00:07+00:00",
    )
    record_paper_decision(
        ledger,
        _input(order_fingerprint="fp-3", halted=True, halt_detail="stop"),
        at_utc="2024-06-03T15:00:08+00:00",
    )

    review = build_operator_review(path)
    text = review.render()
    assert review.ledger_ok is True
    assert review.considered >= 2
    assert review.rejected >= 1
    assert "considered" in text.lower()
    assert "rejected" in text.lower()
    assert "Risk" in text or "risk" in text
    assert "Data-health" in text or "data" in text.lower()
    assert "DEMO" in text or any("DEMO" in a for a in review.anomalies)
    assert LIVE_TRADING_BLOCKED in text
    assert review.live_trading_blocked is True


def test_corrupt_ledger_review_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text("not-json\n", encoding="utf-8")
    review = build_operator_review(path)
    assert review.ledger_ok is False
    assert "FAIL CLOSED" in review.render()
