"""Restart-safe control memory + concurrent ledger append (AC4).

Proves:
1. After a recorded open, a process restart with empty ephemeral control
   state still refuses the same order fingerprint (rehydrate from ledger).
2. Concurrent DecisionLedger writers serialize under flock and never leave
   a chain that verify() would accept as success while broken.
"""

from __future__ import annotations

import concurrent.futures
from datetime import UTC, datetime, timedelta
from pathlib import Path

from chronos.paperops.control_memory import rehydrate_control_memory
from chronos.paperops.decision import PaperDecisionInput, evaluate_paper_decision
from chronos.paperops.ledger import DecisionEvent, DecisionLedger, verify_decision_ledger
from chronos.paperops.reasons import DecisionKind, DecisionOutcome, PaperReasonCode
from chronos.paperops.session import record_paper_decision

NOW = datetime(2024, 6, 3, 15, 0, 0, tzinfo=UTC)


def _healthy(**overrides: object) -> PaperDecisionInput:
    base: dict[str, object] = {
        "strategy_id": "regime_trend_v1",
        "strategy_version": "1.0.0",
        "config_hash": "cfg-restart",
        "symbol": "SPY",
        "side": "BUY",
        "quantity": 1,
        "limit_price": 100.0,
        "bid": 99.5,
        "ask": 100.5,
        "last": 100.0,
        "quote_utc": NOW.isoformat(),
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
        # Ephemeral (would be lost on restart if not rehydrated from ledger):
        "recent_order_fingerprints": (),
        "last_order_at_utc": None,
        "order_fingerprint": "order-fp-durable-1",
        "max_aggregate_exposure_usd": 5000.0,
        "max_symbol_exposure_fraction": 0.5,
        "max_simultaneous_positions": 3,
        "max_daily_loss_usd": 500.0,
        "cooldown_seconds": 0.0,
        "now_utc": (NOW + timedelta(seconds=1)).isoformat(),
        "risk_approved": True,
        "risk_reason": "ok",
    }
    base.update(overrides)
    return PaperDecisionInput(**base)  # type: ignore[arg-type]


def test_restart_cannot_reauthorize_duplicate_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"

    # Process A: first open is authorized and recorded.
    ledger_a = DecisionLedger(path)
    first = record_paper_decision(
        ledger_a,
        _healthy(),
        at_utc=(NOW + timedelta(seconds=2)).isoformat(),
        rehydrate_controls=True,
    )
    assert first.result.may_open is True

    # Process B (restart): brand-new ledger handle, empty ephemeral control
    # state — only the durable ledger memory remains.
    ledger_b = DecisionLedger(path)
    memory = rehydrate_control_memory(path)
    assert "order-fp-durable-1" in memory.recent_order_fingerprints

    retry = record_paper_decision(
        ledger_b,
        _healthy(
            # Explicitly empty ephemeral state (simulates restart amnesia).
            recent_order_fingerprints=(),
            last_order_at_utc=None,
            now_utc=(NOW + timedelta(seconds=10)).isoformat(),
        ),
        at_utc=(NOW + timedelta(seconds=11)).isoformat(),
        rehydrate_controls=True,
    )
    assert retry.result.may_open is False
    assert PaperReasonCode.DUPLICATE_ORDER in retry.result.controls.reason_codes
    assert retry.result.primary_reason is PaperReasonCode.DUPLICATE_ORDER


def test_restart_enforces_cooldown_from_ledger(tmp_path: Path) -> None:
    path = tmp_path / "cooldown.jsonl"
    ledger = DecisionLedger(path)
    first = record_paper_decision(
        ledger,
        _healthy(
            order_fingerprint="cd-1",
            cooldown_seconds=60.0,
            now_utc=(NOW + timedelta(seconds=1)).isoformat(),
        ),
        at_utc=(NOW + timedelta(seconds=2)).isoformat(),
    )
    assert first.result.may_open is True

    # Restart with a *different* fingerprint (not a duplicate) but still inside
    # the cooldown window carried only by durable last_order_at_utc.
    restarted = DecisionLedger(path)
    second = record_paper_decision(
        restarted,
        _healthy(
            order_fingerprint="cd-2",
            recent_order_fingerprints=(),
            last_order_at_utc=None,
            cooldown_seconds=60.0,
            now_utc=(NOW + timedelta(seconds=5)).isoformat(),
        ),
        at_utc=(NOW + timedelta(seconds=6)).isoformat(),
        rehydrate_controls=True,
    )
    assert second.result.may_open is False
    assert PaperReasonCode.COOLDOWN_ACTIVE in second.result.controls.reason_codes


def test_without_rehydrate_empty_memory_would_bypass_but_default_path_does_not(
    tmp_path: Path,
) -> None:
    """Document the hazard: rehydrate_controls=False can amnesia; default is True."""

    path = tmp_path / "hazard.jsonl"
    ledger = DecisionLedger(path)
    record_paper_decision(ledger, _healthy(), at_utc=(NOW + timedelta(seconds=2)).isoformat())

    # Naive pure evaluation with empty fingerprints (no rehydrate) would allow —
    # this is why session.record_paper_decision defaults rehydrate_controls=True.
    naive = evaluate_paper_decision(_healthy(recent_order_fingerprints=(), last_order_at_utc=None))
    assert naive.may_open is True

    # Shipped path after restart must deny.
    after = record_paper_decision(
        DecisionLedger(path),
        _healthy(recent_order_fingerprints=(), last_order_at_utc=None),
        at_utc=(NOW + timedelta(seconds=3)).isoformat(),
        rehydrate_controls=True,
    )
    assert after.result.may_open is False


def _worker_append(args: tuple[str, int, str]) -> tuple[int, str]:
    """Multiprocess worker: append one event via a fresh DecisionLedger."""

    path_str, worker_id, at_utc = args
    path = Path(path_str)
    ledger = DecisionLedger(path)
    event = DecisionEvent(
        kind=DecisionKind.SESSION_MARKER,
        reason_code=PaperReasonCode.RECORDED,
        outcome=DecisionOutcome.INFORMATIONAL,
        strategy_id="race_worker",
        strategy_version="1",
        config_hash=f"cfg-{worker_id}",
        data_timestamp_utc=None,
        data_source="race_test",
        data_quality_label="N/A",
        decision_inputs={"worker": worker_id},
        payload={"worker": worker_id},
    )
    record = ledger.append(event, at_utc=at_utc)
    return record.sequence, record.record_hash


def test_concurrent_appends_serialize_to_intact_chain(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.jsonl"
    n = 12
    jobs = [(str(path), i, (NOW + timedelta(seconds=i)).isoformat()) for i in range(n)]
    with concurrent.futures.ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_worker_append, jobs))

    assert len(results) == n
    sequences = sorted(seq for seq, _ in results)
    assert sequences == list(range(n)), f"expected contiguous sequences, got {sequences}"

    ok, detail = verify_decision_ledger(path)
    assert ok, f"concurrent writers left a broken chain: {detail}"
    ledger = DecisionLedger(path)
    records = ledger.read_all()
    assert len(records) == n
    assert [r.sequence for r in records] == list(range(n))


def test_rehydrate_fails_closed_on_corrupt_ledger(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.jsonl"
    path.write_text("{broken\n", encoding="utf-8")
    import pytest

    from chronos.paperops.ledger import DecisionLedgerError

    with pytest.raises(DecisionLedgerError, match="corrupt"):
        rehydrate_control_memory(path)
