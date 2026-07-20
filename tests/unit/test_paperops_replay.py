"""Deterministic paperops replay: match, diverge, incomplete fail-closed."""

from __future__ import annotations

import json
from pathlib import Path

from chronos.paperops.decision import PaperDecisionInput
from chronos.paperops.ledger import DecisionLedger
from chronos.paperops.reasons import PaperReasonCode
from chronos.paperops.replay import replay_ledger
from chronos.paperops.session import record_paper_decision


def _input(**overrides: object) -> PaperDecisionInput:
    base: dict[str, object] = {
        "strategy_id": "mean_reversion_v1",
        "strategy_version": "1.0.0",
        "config_hash": "cfg123",
        "symbol": "QQQ",
        "side": "BUY",
        "quantity": 2,
        "limit_price": 50.0,
        "bid": 49.9,
        "ask": 50.1,
        "last": 50.0,
        "quote_utc": "2024-06-03T14:00:00+00:00",
        "data_source": "fixture",
        "quality_label": "LIVE",
        "max_quote_age_seconds": 60.0,
        "account_equity_usd": 20000.0,
        "cash_usd": 20000.0,
        "position_notional_by_symbol": {},
        "open_position_count": 0,
        "order_fingerprint": "fp-a",
        "max_aggregate_exposure_usd": 10000.0,
        "max_symbol_exposure_fraction": 0.4,
        "max_simultaneous_positions": 5,
        "max_daily_loss_usd": 1000.0,
        "cooldown_seconds": 0.0,
        "now_utc": "2024-06-03T14:00:10+00:00",
        "risk_approved": True,
    }
    base.update(overrides)
    return PaperDecisionInput(**base)  # type: ignore[arg-type]


def test_clean_replay_matches(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = DecisionLedger(path)
    record_paper_decision(ledger, _input(), at_utc="2024-06-03T14:00:11+00:00")
    record_paper_decision(
        ledger,
        _input(order_fingerprint="fp-b", quality_label="DELAYED"),
        at_utc="2024-06-03T14:00:12+00:00",
    )
    report = replay_ledger(path)
    assert report.ok is True
    assert report.reason_code is PaperReasonCode.REPLAY_MATCH
    assert report.records_replayed >= 1
    assert report.mismatches == ()


def test_diverged_record_flags_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = DecisionLedger(path)
    record_paper_decision(ledger, _input(), at_utc="2024-06-03T14:00:11+00:00")
    # Tamper recorded outcome while keeping hash broken OR rewrite after verify path:
    # inject a second line that claims ALLOW while inputs would DENY (halted).
    # Simpler: append a valid deny record, then mutate outcome field and re-hash incorrectly
    # so verify fails — that tests corrupt. For mismatch, mutate payload may_open + reason
    # after recomputing is hard. Instead: load, change outcome, recompute hash incorrectly
    # by only changing outcome and re-writing with old hash => chain fails.
    #
    # Real mismatch path: record allow, then edit decision_inputs in payload so replay
    # re-evaluates differently but we also need valid chain. So rebuild line with new
    # inputs that deny, keep old outcome=allow, recompute hash for chain validity.
    from chronos.paperops.ledger import _hash_record_body

    lines = path.read_text(encoding="utf-8").splitlines()
    raw = json.loads(lines[0])
    # Force recorded outcome to stay allow but flip halt in decision_inputs so replay denies.
    raw["payload"]["decision_inputs"]["halted"] = True
    raw["payload"]["may_open"] = True  # lie: still claims allow
    raw["outcome"] = "allow"
    raw["reason_code"] = "RISK_APPROVED"
    payload_json = json.dumps(raw["payload"], sort_keys=True, separators=(",", ":"))
    raw["record_hash"] = _hash_record_body(
        sequence=int(raw["sequence"]),
        at_utc=str(raw["at_utc"]),
        kind=str(raw["kind"]),
        reason_code=str(raw["reason_code"]),
        outcome=str(raw["outcome"]),
        strategy_id=str(raw["strategy_id"]),
        strategy_version=str(raw["strategy_version"]),
        config_hash=str(raw["config_hash"]),
        data_timestamp_utc=raw.get("data_timestamp_utc"),
        data_source=str(raw["data_source"]),
        data_quality_label=str(raw["data_quality_label"]),
        inputs_fingerprint=str(raw["inputs_fingerprint"]),
        payload_json=payload_json,
        previous_hash=str(raw["previous_hash"]),
    )
    path.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

    report = replay_ledger(path)
    assert report.ok is False
    assert report.reason_code is PaperReasonCode.REPLAY_MISMATCH
    assert any(m.field in {"outcome", "may_open", "reason_code"} for m in report.mismatches)


def test_empty_ledger_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    report = replay_ledger(path)
    assert report.ok is False
    assert report.reason_code is PaperReasonCode.LEDGER_INCOMPLETE


def test_corrupt_ledger_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text("{broken\n", encoding="utf-8")
    report = replay_ledger(path)
    assert report.ok is False
    assert report.reason_code is PaperReasonCode.LEDGER_CORRUPT
