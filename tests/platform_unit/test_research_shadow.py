"""Coverage for the SHADOW scan.

The scan runs the production decision path (strategy -> sizing -> risk engine)
over the latest closed bars and reports what the system *would* do. The load-
bearing guarantees: the mode lock is NO_ORDERS and cannot submit, every report
is appended to the tamper-evident audit log, and missing/blocking data is
reported as skipped rather than silently dropped.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from chronos.auditlog.log import AuditLog, ChainState, verify_chain
from chronos.control.halt import HaltStore
from chronos.research.runner import STRATEGY_FACTORIES
from chronos.research.shadow import shadow_scan
from chronos.risk.policy import RiskPolicy

NOW = datetime(2024, 4, 2, 21, 0, tzinfo=UTC)


def _write_uptrend_csv(path: Path, *, n: int = 60) -> None:
    lines = ["date,open,high,low,close,volume"]
    session = date(2024, 1, 1)
    prev_close = 100.0
    for i in range(n):
        while session.weekday() >= 5:
            session += timedelta(days=1)
        close = 100.0 + 0.5 * i
        open_ = prev_close
        high = max(open_, close) + 1.0
        low = min(open_, close) - 1.0
        lines.append(f"{session.isoformat()},{open_},{high},{low},{close},1000000")
        prev_close = close
        session += timedelta(days=1)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _permissive_policy() -> RiskPolicy:
    return RiskPolicy.model_validate(
        {
            "policy_version": "test",
            "allowed_symbols": ("SPY",),
            "allowed_strategy_ids": ("baseline_buy_hold",),
            "allow_long_entries": True,
            "max_bot_capital_usd": 3000,
            "max_position_notional_usd": 3000,
            "max_aggregate_exposure_usd": 3000,
            "max_symbol_exposure_fraction": 1.0,
            "max_risk_per_trade_fraction": 0.10,
            "max_simultaneous_positions": 1,
            "max_open_orders": 2,
            "max_daily_loss_usd": 300,
            "max_weekly_loss_usd": 600,
            "max_drawdown_fraction": 0.5,
            "max_consecutive_losses": 10,
            "max_quote_age_seconds": 300,
            "max_bar_age_seconds": 5 * 86400,
            "max_price_deviation_fraction": 0.05,
            "allow_overnight_positions": True,
        }
    )


def test_shadow_scan_reports_and_audits_every_decision(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_uptrend_csv(data_dir / "SPY.csv")
    halt = HaltStore(tmp_path / "halt.json")
    halt.rearm("ready")
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLog(audit_path)

    reports = shadow_scan(
        strategies={"baseline_buy_hold": STRATEGY_FACTORIES["baseline_buy_hold"]()},
        symbols=("SPY",),
        data_dir=data_dir,
        risk_policy=_permissive_policy(),
        halt_store=halt,
        audit_log=audit,
        equity_usd=3000.0,
        now_utc=NOW,
    )

    assert len(reports) == 1
    report = reports[0]
    assert report["symbol"] == "SPY"
    assert report["strategy"] == "baseline_buy_hold"
    # SHADOW is structurally incapable of submitting.
    assert report["capability"] == "NO_ORDERS"
    # Every report is appended to the hash-chained audit log.
    assert verify_chain(audit_path).state is ChainState.VALID
    contents = audit_path.read_text(encoding="utf-8")
    assert "shadow_scan" in contents


def test_shadow_scan_skips_missing_data(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()  # deliberately empty
    halt = HaltStore(tmp_path / "halt.json")
    halt.rearm("ready")
    audit = AuditLog(tmp_path / "audit.jsonl")

    reports = shadow_scan(
        strategies={"baseline_buy_hold": STRATEGY_FACTORIES["baseline_buy_hold"]()},
        symbols=("SPY",),
        data_dir=data_dir,
        risk_policy=_permissive_policy(),
        halt_store=halt,
        audit_log=audit,
        equity_usd=3000.0,
        now_utc=NOW,
    )
    assert len(reports) == 1
    assert "skipped" in reports[0]
    assert "no data file" in str(reports[0]["skipped"])


def test_shadow_scan_produces_intent_and_risk_decision(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_uptrend_csv(data_dir / "SPY.csv")
    halt = HaltStore(tmp_path / "halt.json")
    halt.rearm("ready")
    audit = AuditLog(tmp_path / "audit.jsonl")

    reports = shadow_scan(
        strategies={"baseline_buy_hold": STRATEGY_FACTORIES["baseline_buy_hold"]()},
        symbols=("SPY",),
        data_dir=data_dir,
        risk_policy=_permissive_policy(),
        halt_store=halt,
        audit_log=audit,
        equity_usd=3000.0,
        now_utc=NOW,
    )
    report = reports[0]
    # buy-and-hold proposes a long entry from flat, which sizes to an intent
    # and reaches the risk engine.
    assert report["proposal"] is not None
    assert report["intent"] is not None
    assert report["risk_decision"] is not None
