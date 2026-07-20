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


def test_empty_order_fingerprint_restart_cannot_reauthorize(tmp_path: Path) -> None:
    """Empty order_fingerprint must still be durable via content-hash identity.

    evaluate uses content hash when order_fingerprint is blank; that effective
    value must be persisted and rehydrated so restart cannot re-allow.
    Identical decision inputs (including now_utc) must not re-authorize.
    """

    path = tmp_path / "empty_fp.jsonl"
    # Fixed evaluation clock so content hash is stable across restart.
    fixed_now = (NOW + timedelta(seconds=1)).isoformat()
    first = record_paper_decision(
        DecisionLedger(path),
        _healthy(
            order_fingerprint="",  # blank — effective id is content hash
            recent_order_fingerprints=(),
            last_order_at_utc=None,
            now_utc=fixed_now,
        ),
        at_utc=(NOW + timedelta(seconds=2)).isoformat(),
    )
    assert first.result.may_open is True
    effective = first.result.effective_order_fingerprint
    assert effective, "effective_order_fingerprint must be non-empty"
    assert first.record.payload.get("order_fingerprint") == effective
    assert first.record.payload.get("effective_order_fingerprint") == effective

    memory = rehydrate_control_memory(path)
    assert effective in memory.recent_order_fingerprints, (
        f"rehydrate missed effective fp {effective!r}; saw {memory.recent_order_fingerprints!r}"
    )

    # Restart: *identical* decision inputs (empty order_fingerprint + same now).
    retry = record_paper_decision(
        DecisionLedger(path),
        _healthy(
            order_fingerprint="",
            recent_order_fingerprints=(),
            last_order_at_utc=None,
            now_utc=fixed_now,
        ),
        at_utc=(NOW + timedelta(seconds=21)).isoformat(),
    )
    assert retry.result.may_open is False, (
        "restart re-authorized empty-fingerprint open; durable identity lost "
        f"(first_fp={effective!r} retry_fp={retry.result.effective_order_fingerprint!r})"
    )
    assert PaperReasonCode.DUPLICATE_ORDER in retry.result.controls.reason_codes


def _worker_same_fingerprint(args: tuple[str, str, str]) -> bool:
    """Record one decision with a shared order fingerprint; return may_open."""

    path_str, fingerprint, at_utc = args
    recorded = record_paper_decision(
        DecisionLedger(Path(path_str)),
        _healthy(
            order_fingerprint=fingerprint,
            recent_order_fingerprints=(),
            last_order_at_utc=None,
            now_utc=at_utc,
            # Unique now_utc per worker would change content hash; keep same
            # fingerprint field so identity is the explicit order_fingerprint.
        ),
        at_utc=at_utc,
        rehydrate_controls=True,
    )
    return recorded.result.may_open


def test_concurrent_same_fingerprint_at_most_one_allow(tmp_path: Path) -> None:
    """Two concurrent record_paper_decision calls with the same fingerprint
    must not both allow (critical section covers rehydrate→evaluate→append).
    """

    path = tmp_path / "double_allow.jsonl"
    fingerprint = "shared-fp-race"
    # Same now_utc so content is identical; identity is order_fingerprint.
    at = (NOW + timedelta(seconds=3)).isoformat()
    jobs = [(str(path), fingerprint, at) for _ in range(8)]
    with concurrent.futures.ProcessPoolExecutor(max_workers=4) as pool:
        allows = list(pool.map(_worker_same_fingerprint, jobs))

    allow_count = sum(1 for a in allows if a)
    assert allow_count == 1, (
        f"expected exactly one ALLOW under concurrent same-fingerprint race, "
        f"got {allow_count} allows out of {len(allows)}: {allows}"
    )
    ok, detail = verify_decision_ledger(path)
    assert ok, detail
    records = DecisionLedger(path).read_all()
    assert len(records) == 8
    allow_records = [r for r in records if r.outcome == "allow"]
    assert len(allow_records) == 1
