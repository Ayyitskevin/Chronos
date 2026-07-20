"""Paper decision ledger: append, provenance fields, secrets, fail-closed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chronos.paperops.decision import PaperDecisionInput
from chronos.paperops.ledger import (
    DecisionEvent,
    DecisionLedger,
    DecisionLedgerError,
    verify_decision_ledger,
)
from chronos.paperops.reasons import DecisionKind, DecisionOutcome, PaperReasonCode
from chronos.paperops.records import sanitize_payload
from chronos.paperops.session import record_paper_decision


def _event(**overrides: object) -> DecisionEvent:
    base: dict[str, object] = {
        "kind": DecisionKind.REJECTION,
        "reason_code": PaperReasonCode.DATA_STALE,
        "outcome": DecisionOutcome.DENY,
        "strategy_id": "regime_trend_v1",
        "strategy_version": "1.0.0",
        "config_hash": "abc123",
        "data_timestamp_utc": "2024-06-03T15:00:00+00:00",
        "data_source": "ibkr_paper",
        "data_quality_label": "STALE",
        "decision_inputs": {"symbol": "SPY", "now_utc": "2024-06-03T15:00:05+00:00"},
        "payload": {"may_open": False, "detail": "stale"},
    }
    base.update(overrides)
    return DecisionEvent(**base)  # type: ignore[arg-type]


def test_append_records_provenance_fields(tmp_path: Path) -> None:
    ledger = DecisionLedger(tmp_path / "decisions.jsonl")
    record = ledger.append(_event(), at_utc="2024-06-03T15:00:06+00:00")
    assert record.sequence == 0
    assert record.strategy_id == "regime_trend_v1"
    assert record.strategy_version == "1.0.0"
    assert record.config_hash == "abc123"
    assert record.data_timestamp_utc == "2024-06-03T15:00:00+00:00"
    assert record.data_source == "ibkr_paper"
    assert record.reason_code == PaperReasonCode.DATA_STALE.value
    assert record.inputs_fingerprint
    assert len(record.record_hash) == 64

    ok, detail = verify_decision_ledger(ledger.path)
    assert ok, detail
    loaded = ledger.read_all()
    assert len(loaded) == 1
    assert loaded[0].record_hash == record.record_hash


def test_corrupt_trailing_record_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    ledger = DecisionLedger(path)
    ledger.append(_event())
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not valid json\n")
    with pytest.raises(DecisionLedgerError, match="unreadable"):
        DecisionLedger(path)
    ok, detail = verify_decision_ledger(path)
    assert not ok
    assert "unreadable" in detail or "JSON" in detail or "line" in detail


def test_tampered_payload_breaks_chain(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    ledger = DecisionLedger(path)
    ledger.append(_event())
    ledger.append(_event(reason_code=PaperReasonCode.CONTROLS_OK, outcome=DecisionOutcome.ALLOW))
    lines = path.read_text(encoding="utf-8").splitlines()
    raw = json.loads(lines[0])
    raw["payload"]["injected"] = "tamper"
    lines[0] = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, detail = verify_decision_ledger(path)
    assert not ok
    assert "hash mismatch" in detail or "chain" in detail


def test_sanitize_payload_drops_secret_keys_and_tokens() -> None:
    clean = sanitize_payload(
        {
            "symbol": "SPY",
            "api_key": "super-secret",
            "Authorization": "Bearer abc.def",
            "nested": {"password": "x", "ok": 1},
            "note": "Bearer sk-abcdefghijklmnopqrstuvwxyz",
        }
    )
    assert "api_key" not in clean
    assert "Authorization" not in clean
    assert "password" not in clean.get("nested", {})  # type: ignore[arg-type]
    assert clean["symbol"] == "SPY"
    assert clean["nested"]["ok"] == 1  # type: ignore[index]
    blob = json.dumps(clean)
    assert "super-secret" not in blob
    assert "Bearer" not in blob
    assert "sk-abcdef" not in blob


def test_append_rejects_missing_provenance(tmp_path: Path) -> None:
    ledger = DecisionLedger(tmp_path / "d.jsonl")
    with pytest.raises(DecisionLedgerError, match="config_hash"):
        ledger.append(_event(config_hash=""))


def _healthy_input(**overrides: object) -> PaperDecisionInput:
    base: dict[str, object] = {
        "strategy_id": "regime_trend_v1",
        "strategy_version": "1.0.0",
        "config_hash": "cfgdeadbeef",
        "symbol": "SPY",
        "side": "BUY",
        "quantity": 1,
        "limit_price": 100.0,
        "bid": 99.5,
        "ask": 100.5,
        "last": 100.0,
        "quote_utc": "2024-06-03T15:00:00+00:00",
        "data_source": "ibkr_paper",
        "quality_label": "LIVE",
        "max_quote_age_seconds": 30.0,
        "halted": False,
        "kill_switch_engaged": False,
        "account_equity_usd": 10000.0,
        "cash_usd": 10000.0,
        "position_notional_by_symbol": {},
        "open_position_count": 0,
        "realized_pnl_today_usd": 0.0,
        "recent_order_fingerprints": (),
        "order_fingerprint": "order-1",
        "max_aggregate_exposure_usd": 5000.0,
        "max_symbol_exposure_fraction": 0.5,
        "max_simultaneous_positions": 3,
        "max_daily_loss_usd": 500.0,
        "cooldown_seconds": 0.0,
        "now_utc": "2024-06-03T15:00:05+00:00",
        "risk_approved": True,
        "risk_reason": "ok",
    }
    base.update(overrides)
    return PaperDecisionInput(**base)  # type: ignore[arg-type]


def test_record_paper_decision_end_to_end(tmp_path: Path) -> None:
    ledger = DecisionLedger(tmp_path / "session.jsonl")
    recorded = record_paper_decision(ledger, _healthy_input(), at_utc="2024-06-03T15:00:06+00:00")
    assert recorded.result.may_open is True
    assert recorded.record.config_hash == "cfgdeadbeef"
    assert recorded.record.strategy_version == "1.0.0"
    assert "decision_inputs" in recorded.record.payload
    assert recorded.record.data_source == "ibkr_paper"
