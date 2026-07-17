"""Read-only monitoring snapshot: correctness and the no-broker guarantee."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

import chronos.monitoring.snapshot as snapshot_mod
from chronos.auditlog.log import AuditLog
from chronos.control.halt import HaltReason, HaltStore
from chronos.control.modes import TradingMode
from chronos.monitoring.snapshot import build_snapshot, render_markdown, render_text

NOW = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)


def test_snapshot_from_armed_state(tmp_path: Path) -> None:
    halt = tmp_path / "halt.json"
    HaltStore(halt).rearm("ready")
    audit = tmp_path / "audit.jsonl"
    log = AuditLog(audit)
    log.append("service_startup", {"outcome": "reconciled"})
    log.append("service_decision", {"symbol": "SPY", "direction": "ENTER_LONG"})

    snap = build_snapshot(
        mode=TradingMode.SHADOW,
        halt_file=halt,
        audit_file=audit,
        now_utc=NOW,
    )
    assert not snap.halted
    assert snap.capability == "NO_ORDERS"
    assert snap.live_capable is False
    assert snap.reconciliation_status == "reconciled"
    assert snap.audit_ok
    assert snap.audit_records == 2


def test_snapshot_reflects_halt(tmp_path: Path) -> None:
    halt = tmp_path / "halt.json"
    HaltStore(halt).halt(HaltReason.RECONCILIATION_MISMATCH, "mismatch at startup")
    snap = build_snapshot(
        mode=TradingMode.SHADOW,
        halt_file=halt,
        audit_file=tmp_path / "audit.jsonl",
        now_utc=NOW,
    )
    assert snap.halted
    assert snap.halt_reason == "RECONCILIATION_MISMATCH"
    assert any("HALTED" in w for w in snap.warnings)


def test_snapshot_missing_halt_reads_never_armed(tmp_path: Path) -> None:
    snap = build_snapshot(
        mode=TradingMode.SHADOW,
        halt_file=tmp_path / "absent.json",
        audit_file=tmp_path / "audit.jsonl",
        now_utc=NOW,
    )
    assert snap.halted  # fresh deployment reads as halted
    assert snap.halt_reason == "NEVER_ARMED"


def test_snapshot_flags_corrupt_audit(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    AuditLog(audit).append("service_startup", {"outcome": "reconciled"})
    with audit.open("a", encoding="utf-8") as handle:
        handle.write('{"broken')
    halt = tmp_path / "halt.json"
    HaltStore(halt).rearm("ready")
    snap = build_snapshot(mode=TradingMode.SHADOW, halt_file=halt, audit_file=audit, now_utc=NOW)
    assert not snap.audit_ok
    assert any("audit chain" in w for w in snap.warnings)


def test_data_freshness(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "SPY.csv").write_text(
        "date,open,high,low,close,volume\n"
        "2024-01-02,100,101,99,100.5,1000\n"
        "2024-01-03,100.5,102,100,101.5,1200\n"
    )
    halt = tmp_path / "halt.json"
    HaltStore(halt).rearm("ready")
    snap = build_snapshot(
        mode=TradingMode.SHADOW,
        halt_file=halt,
        audit_file=tmp_path / "audit.jsonl",
        data_dir=data,
        symbols=("SPY", "QQQ"),
        now_utc=NOW,
    )
    fresh = {f.symbol: f for f in snap.data_freshness}
    assert fresh["SPY"].latest_session == "2024-01-03"
    assert fresh["SPY"].bars == 2
    assert fresh["QQQ"].latest_session is None  # missing


def test_live_mode_is_never_live_capable(tmp_path: Path) -> None:
    # Even requesting LIVE, the lock is DENIED_LIVE_DISABLED -> not live-capable.
    halt = tmp_path / "halt.json"
    HaltStore(halt).rearm("ready")
    snap = build_snapshot(
        mode=TradingMode.LIVE,
        halt_file=halt,
        audit_file=tmp_path / "audit.jsonl",
        now_utc=NOW,
    )
    assert snap.capability == "DENIED_LIVE_DISABLED"
    assert snap.live_capable is False


def test_renderers_do_not_crash(tmp_path: Path) -> None:
    halt = tmp_path / "halt.json"
    HaltStore(halt).rearm("ready")
    snap = build_snapshot(
        mode=TradingMode.SHADOW, halt_file=halt, audit_file=tmp_path / "audit.jsonl", now_utc=NOW
    )
    assert "CHRONOS PLATFORM MONITOR" in render_text(snap)
    assert "# Chronos Platform Monitor" in render_markdown(snap)


def test_active_limits_from_policy(tmp_path: Path) -> None:
    policy = tmp_path / "risk.yaml"
    policy.write_text(
        "policy_version: '7'\nmax_daily_loss_usd: 150.0\nmax_open_orders: 3\n",
        encoding="utf-8",
    )
    halt = tmp_path / "halt.json"
    HaltStore(halt).rearm("ready")
    snap = build_snapshot(
        mode=TradingMode.SHADOW,
        halt_file=halt,
        audit_file=tmp_path / "audit.jsonl",
        risk_policy_path=policy,
        now_utc=NOW,
    )
    limits = dict(snap.active_limits)
    assert limits["Max daily loss (USD)"] == "150.0"
    assert limits["Max open orders"] == "3"
    assert snap.risk_policy_version == "7"


def test_no_ledger_configured_is_honest(tmp_path: Path) -> None:
    halt = tmp_path / "halt.json"
    HaltStore(halt).rearm("ready")
    snap = build_snapshot(
        mode=TradingMode.SHADOW,
        halt_file=halt,
        audit_file=tmp_path / "audit.jsonl",
        now_utc=NOW,
    )
    assert snap.ledger_tracked is False
    assert snap.open_orders == ()
    assert snap.net_positions == ()
    assert snap.recent_fills == ()
    assert "no ledger is configured" in snap.pnl_note


def test_ledger_open_orders_positions_and_fills(tmp_path: Path) -> None:
    from decimal import Decimal

    from chronos.domain.enums import OrderSide
    from chronos.execution.intents import IntentStatus, OrderIntent, TimeInForce
    from chronos.execution.sqlite_ledger import SqliteLedger

    ledger_path = tmp_path / "ledger.db"
    ledger = SqliteLedger(ledger_path)

    working = OrderIntent(
        strategy_id="s",
        strategy_version="1",
        symbol="SPY",
        side=OrderSide.BUY,
        quantity=10,
        limit_price=Decimal("500.00"),
        stop_price=None,
        time_in_force=TimeInForce.DAY,
        decision_timestamp_utc=NOW,
        source_bar_sequence_id="test:SPY:1d:working",
        proposal_reason="test",
    )
    # Recorded as SUBMITTED with no later transition: exercises the COALESCE to
    # initial_status and confirms a partial fill counts toward the net position.
    ledger.record_intent(working, IntentStatus.SUBMITTED)
    ledger.record_fill(working.intent_id, 4, Decimal("500.10"), Decimal("1.00"), NOW)

    filled = OrderIntent(
        strategy_id="s",
        strategy_version="1",
        symbol="QQQ",
        side=OrderSide.BUY,
        quantity=5,
        limit_price=Decimal("400.00"),
        stop_price=None,
        time_in_force=TimeInForce.DAY,
        decision_timestamp_utc=NOW,
        source_bar_sequence_id="test:QQQ:1d:filled",
        proposal_reason="test",
    )
    ledger.record_intent(filled, IntentStatus.PENDING_SUBMISSION)
    ledger.record_transition(filled.intent_id, IntentStatus.FILLED, NOW, "fill")
    ledger.record_fill(filled.intent_id, 5, Decimal("401.00"), Decimal("1.00"), NOW)
    ledger.close()

    halt = tmp_path / "halt.json"
    HaltStore(halt).rearm("ready")
    snap = build_snapshot(
        mode=TradingMode.SHADOW,
        halt_file=halt,
        audit_file=tmp_path / "audit.jsonl",
        ledger_path=ledger_path,
        now_utc=NOW,
    )

    assert snap.ledger_tracked is True
    # Only the SUBMITTED order is "open"; the FILLED one is not.
    assert [order.symbol for order in snap.open_orders] == ["SPY"]
    assert snap.open_orders[0].filled_quantity == 4
    # Net positions are fill-derived and signed by side; both are long here.
    positions = {p.symbol: p.net_shares for p in snap.net_positions}
    assert positions == {"SPY": 4, "QQQ": 5}
    assert len(snap.recent_fills) == 2
    assert "not reconstructed" in snap.pnl_note

    # The rendered views must include the new sections without crashing.
    text = render_text(snap)
    assert "open orders" in text
    assert "net positions" in text
    assert "Active limits" not in text  # no policy passed here
    assert "Net positions:" in render_markdown(snap)


def test_ledger_unreadable_is_warning_not_crash(tmp_path: Path) -> None:
    bogus = tmp_path / "ledger.db"
    bogus.write_bytes(b"not a sqlite database at all")
    halt = tmp_path / "halt.json"
    HaltStore(halt).rearm("ready")
    snap = build_snapshot(
        mode=TradingMode.SHADOW,
        halt_file=halt,
        audit_file=tmp_path / "audit.jsonl",
        ledger_path=bogus,
        now_utc=NOW,
    )
    assert snap.ledger_tracked is False
    assert any("ledger" in w for w in snap.warnings)


def _module_imports(module_file: str) -> list[str]:
    source = Path(module_file).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    return imported


def _assert_no_broker_imports(module_file: str) -> None:
    for name in _module_imports(module_file):
        assert "ib_async" not in name
        assert not name.startswith("chronos.broker")
        assert "brokers" not in name
        assert "ibkr" not in name.lower()


def test_monitoring_imports_no_broker_adapter() -> None:
    # The monitoring plane must render persisted state only: it must not import
    # any broker adapter (execution or wheel) or ib_async. The Streamlit page and
    # the ledger reader are held to the same guarantee — nothing in the monitor,
    # UI or ledger read, may reach the broker.
    import chronos.monitoring.ledger_view as ledger_view_mod
    import chronos.monitoring.streamlit_app as streamlit_mod

    _assert_no_broker_imports(snapshot_mod.__file__)
    _assert_no_broker_imports(streamlit_mod.__file__)
    _assert_no_broker_imports(ledger_view_mod.__file__)


def test_streamlit_config_snapshot_reads_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import chronos.monitoring.streamlit_app as streamlit_mod

    halt = tmp_path / "halt.json"
    HaltStore(halt).halt(HaltReason.RECONCILIATION_MISMATCH, "mismatch")
    monkeypatch.setenv("CHRONOS_MONITOR_MODE", "shadow")
    monkeypatch.setenv("CHRONOS_HALT_FILE", str(halt))
    monkeypatch.setenv("CHRONOS_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("CHRONOS_RISK_POLICY", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("CHRONOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CHRONOS_MONITOR_SYMBOLS", "SPY, QQQ")

    snap = streamlit_mod._config_snapshot()
    assert snap.mode == "shadow"
    assert snap.halted
    assert snap.halt_reason == "RECONCILIATION_MISMATCH"


def test_streamlit_config_snapshot_bad_mode_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import chronos.monitoring.streamlit_app as streamlit_mod

    monkeypatch.setenv("CHRONOS_MONITOR_MODE", "not-a-mode")
    monkeypatch.setenv("CHRONOS_HALT_FILE", str(tmp_path / "halt.json"))
    monkeypatch.setenv("CHRONOS_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("CHRONOS_DATA_DIR", raising=False)

    snap = streamlit_mod._config_snapshot()
    assert snap.mode == "shadow"  # invalid mode falls back to SHADOW
