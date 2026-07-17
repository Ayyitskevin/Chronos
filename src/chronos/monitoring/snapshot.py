"""Build a read-only monitoring snapshot from persisted platform state.

Everything here is a pure read of files the platform already wrote (halt
state, audit log, risk policy) plus config-derived mode information. No broker
adapter is imported and no network call is made — the snapshot is exactly as
trustworthy as the files on disk, and no more.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from chronos.auditlog.log import verify_chain
from chronos.control.halt import HaltStore
from chronos.control.modes import ExecutionCapability, TradingMode, resolve_mode_lock
from chronos.marketdata.csv_provider import load_daily_csv
from chronos.monitoring.ledger_view import (
    FillView,
    OpenOrderView,
    PositionView,
    read_ledger_view,
)
from chronos.risk.policy import RiskPolicy, load_risk_policy

# The operator-relevant subset of the risk policy, in display order. Surfacing
# the active limits is read-only and cannot change them (the loaded policy is
# frozen); it just makes the guardrails legible next to the state they bound.
_ACTIVE_LIMIT_FIELDS: tuple[tuple[str, str], ...] = (
    ("max_bot_capital_usd", "Max bot capital (USD)"),
    ("max_position_notional_usd", "Max position notional (USD)"),
    ("max_aggregate_exposure_usd", "Max aggregate exposure (USD)"),
    ("max_simultaneous_positions", "Max simultaneous positions"),
    ("max_open_orders", "Max open orders"),
    ("max_daily_loss_usd", "Max daily loss (USD)"),
    ("max_weekly_loss_usd", "Max weekly loss (USD)"),
    ("max_drawdown_fraction", "Max drawdown fraction"),
    ("max_consecutive_losses", "Max consecutive losses"),
)


@dataclass(frozen=True, slots=True)
class DataFreshness:
    symbol: str
    latest_session: str | None
    bars: int


@dataclass(frozen=True, slots=True)
class MonitoringSnapshot:
    generated_at_utc: str
    mode: str
    capability: str
    live_capable: bool
    mode_banner: str
    halted: bool
    halt_reason: str | None
    halt_detail: str
    reconciliation_status: str
    audit_ok: bool
    audit_detail: str
    audit_records: int
    recent_decisions: tuple[dict[str, object], ...]
    data_freshness: tuple[DataFreshness, ...]
    risk_policy_version: str | None
    risk_policy_hash: str | None
    active_limits: tuple[tuple[str, str], ...]
    ledger_tracked: bool
    open_orders: tuple[OpenOrderView, ...]
    net_positions: tuple[PositionView, ...]
    recent_fills: tuple[FillView, ...]
    pnl_note: str
    code_commit: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _code_commit(repo_root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except Exception:  # pragma: no cover - git may be absent
        return "unknown"


def _read_audit(audit_file: Path, *, tail: int) -> tuple[bool, str, int, list[dict[str, object]]]:
    ok, detail = verify_chain(audit_file)
    records: list[dict[str, object]] = []
    if audit_file.exists():
        with audit_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        break
    decisions = [r for r in records if r.get("kind") in ("service_decision", "service_startup")]
    return ok, detail, len(records), decisions[-tail:]


def _active_limits(policy: RiskPolicy) -> tuple[tuple[str, str], ...]:
    dumped = policy.model_dump()
    return tuple((label, str(dumped[key])) for key, label in _ACTIVE_LIMIT_FIELDS if key in dumped)


def _reconciliation_status(decisions: list[dict[str, object]]) -> str:
    for record in reversed(decisions):
        if record.get("kind") == "service_startup":
            payload = record.get("payload", {})
            if isinstance(payload, dict):
                return str(payload.get("outcome", "unknown"))
    return "unknown (no startup recorded)"


def build_snapshot(
    *,
    mode: TradingMode,
    halt_file: Path,
    audit_file: Path,
    risk_policy_path: Path | None = None,
    data_dir: Path | None = None,
    symbols: tuple[str, ...] = (),
    ledger_path: Path | None = None,
    repo_root: Path | None = None,
    tail: int = 10,
    now_utc: datetime | None = None,
) -> MonitoringSnapshot:
    """Assemble a monitoring snapshot from persisted state (read-only)."""

    now = now_utc or datetime.now(tz=UTC)
    warnings: list[str] = []

    lock = resolve_mode_lock(
        requested_mode=mode,
        paper_account_allowlist=(),
        broker_reported_account_id=None,
        broker_reported_environment_is_paper=None,
        order_transmission_enabled=False,
    )
    live_capable = lock.capability not in (
        ExecutionCapability.NO_ORDERS,
        ExecutionCapability.SIMULATED_ONLY,
        ExecutionCapability.DENIED_LIVE_DISABLED,
    ) and lock.mode in (TradingMode.CANARY_LIVE, TradingMode.LIVE)
    banner = (
        f"MODE={lock.mode.value.upper()}  CAPABILITY={lock.capability.value}  "
        f"LIVE-CAPABLE={live_capable}"
    )

    halt = HaltStore(halt_file).read()
    if halt.halted:
        warnings.append(f"platform is HALTED ({halt.reason.value if halt.reason else 'unknown'})")

    audit_ok, audit_detail, audit_records, decisions = _read_audit(audit_file, tail=tail)
    if not audit_ok:
        warnings.append(f"audit chain verification failed: {audit_detail}")

    freshness: list[DataFreshness] = []
    if data_dir is not None:
        for symbol in symbols:
            path = data_dir / f"{symbol.upper()}.csv"
            if not path.exists():
                freshness.append(DataFreshness(symbol=symbol.upper(), latest_session=None, bars=0))
                continue
            try:
                series = load_daily_csv(path, symbol=symbol.upper(), source="monitor").series
                latest = series[-1].session_date.isoformat() if len(series) else None
                freshness.append(
                    DataFreshness(symbol=symbol.upper(), latest_session=latest, bars=len(series))
                )
            except Exception:  # a bad data file is a warning, not a crash
                freshness.append(DataFreshness(symbol=symbol.upper(), latest_session=None, bars=0))
                warnings.append(f"{symbol.upper()} data file unreadable")

    policy_version: str | None = None
    policy_hash: str | None = None
    active_limits: tuple[tuple[str, str], ...] = ()
    if risk_policy_path is not None and risk_policy_path.exists():
        try:
            policy = load_risk_policy(risk_policy_path)
            policy_version = policy.policy_version
            policy_hash = policy.config_hash
            active_limits = _active_limits(policy)
        except Exception:  # invalid policy is surfaced as a warning
            warnings.append("risk policy file is invalid")

    ledger_tracked = False
    open_orders: tuple[OpenOrderView, ...] = ()
    net_positions: tuple[PositionView, ...] = ()
    recent_fills: tuple[FillView, ...] = ()
    pnl_note = (
        "positions, open orders, and fills are read from the execution ledger; "
        "no ledger is configured, so none are tracked"
    )
    if ledger_path is not None:
        view = read_ledger_view(ledger_path, fills_tail=tail)
        if view is None:
            if ledger_path.exists():
                warnings.append("execution ledger is unreadable")
            pnl_note = (
                "positions, open orders, and fills are read from the execution ledger; "
                "the ledger has not been created yet (nothing submitted)"
            )
        else:
            ledger_tracked = True
            open_orders = view.open_orders
            net_positions = view.net_positions
            recent_fills = view.recent_fills
            pnl_note = (
                "positions/orders/fills are direct ledger reads; realized and unrealized "
                "P&L are not reconstructed here (SHADOW is flat by construction)"
            )

    return MonitoringSnapshot(
        generated_at_utc=now.isoformat(),
        mode=lock.mode.value,
        capability=lock.capability.value,
        live_capable=live_capable,
        mode_banner=banner,
        halted=halt.halted,
        halt_reason=halt.reason.value if halt.reason else None,
        halt_detail=halt.detail,
        reconciliation_status=_reconciliation_status(decisions),
        audit_ok=audit_ok,
        audit_detail=audit_detail,
        audit_records=audit_records,
        recent_decisions=tuple(decisions),
        data_freshness=tuple(freshness),
        risk_policy_version=policy_version,
        risk_policy_hash=policy_hash,
        active_limits=active_limits,
        ledger_tracked=ledger_tracked,
        open_orders=open_orders,
        net_positions=net_positions,
        recent_fills=recent_fills,
        pnl_note=pnl_note,
        code_commit=_code_commit(repo_root or Path.cwd()),
        warnings=tuple(warnings),
    )


def render_text(snapshot: MonitoringSnapshot) -> str:
    """Plain-text render for the CLI."""

    if snapshot.halted:
        halt_line = "HALTED — " + (snapshot.halt_reason or "")
        if snapshot.halt_detail:
            halt_line += f"  ({snapshot.halt_detail})"
    else:
        halt_line = "armed"
    lines = [
        "=" * 72,
        "CHRONOS PLATFORM MONITOR",
        snapshot.mode_banner,
        "LIVE TRADING is hard-disabled in this build (no override exists).",
        "=" * 72,
        f"generated:      {snapshot.generated_at_utc}   commit {snapshot.code_commit}",
        f"halt:           {halt_line}",
        f"reconciliation: {snapshot.reconciliation_status}",
        f"audit chain:    {'OK' if snapshot.audit_ok else 'FAILED'} — {snapshot.audit_detail}",
        f"risk policy:    {snapshot.risk_policy_version or 'none'} "
        f"(hash {snapshot.risk_policy_hash or '-'})",
    ]
    if snapshot.data_freshness:
        lines.append("data freshness:")
        for f in snapshot.data_freshness:
            lines.append(f"  {f.symbol:6s} latest={f.latest_session or 'MISSING'} bars={f.bars}")
    if snapshot.active_limits:
        lines.append("active limits:")
        for label, value in snapshot.active_limits:
            lines.append(f"  {label}: {value}")
    lines.append(f"positions/orders/fills: {snapshot.pnl_note}")
    if snapshot.open_orders:
        lines.append(f"open orders ({len(snapshot.open_orders)}):")
        for order in snapshot.open_orders:
            lines.append(
                f"  {order.symbol:6s} {order.side:4s} qty={order.quantity} "
                f"filled={order.filled_quantity} status={order.status}"
            )
    if snapshot.net_positions:
        lines.append("net positions:")
        for position in snapshot.net_positions:
            lines.append(f"  {position.symbol:6s} net_shares={position.net_shares}")
    if snapshot.recent_fills:
        lines.append(f"recent fills ({len(snapshot.recent_fills)}):")
        for fill in snapshot.recent_fills:
            lines.append(
                f"  {fill.symbol:6s} cum_qty={fill.cumulative_quantity} "
                f"avg={fill.average_price or '-'} at={fill.at_utc}"
            )
    if snapshot.recent_decisions:
        lines.append(f"recent audit events ({len(snapshot.recent_decisions)}):")
        for record in snapshot.recent_decisions:
            payload = record.get("payload", {})
            lines.append(f"  [{record.get('kind')}] {payload}")
    if snapshot.warnings:
        lines.append("WARNINGS:")
        lines.extend(f"  ! {w}" for w in snapshot.warnings)
    lines.append("=" * 72)
    return "\n".join(lines)


def render_markdown(snapshot: MonitoringSnapshot) -> str:
    """Markdown render for a doc or an artifact."""

    status = "🛑 HALTED" if snapshot.halted else "🟢 armed"
    rows = [
        "# Chronos Platform Monitor",
        "",
        f"- **Mode:** `{snapshot.mode}` · **Capability:** `{snapshot.capability}` · "
        f"**Live-capable:** `{snapshot.live_capable}` (live is hard-disabled in this build)",
        f"- **Halt:** {status}"
        + (f" — `{snapshot.halt_reason}` ({snapshot.halt_detail})" if snapshot.halted else ""),
        f"- **Reconciliation:** {snapshot.reconciliation_status}",
        f"- **Audit chain:** {'OK' if snapshot.audit_ok else 'FAILED'} — {snapshot.audit_detail} "
        f"({snapshot.audit_records} records)",
        f"- **Risk policy:** {snapshot.risk_policy_version or 'none'} "
        f"(`{snapshot.risk_policy_hash or '-'}`)",
        f"- **Positions/orders/fills:** {snapshot.pnl_note}",
        f"- **Open orders:** {len(snapshot.open_orders)} · "
        f"**Net positions:** {len(snapshot.net_positions)} · "
        f"**Recent fills:** {len(snapshot.recent_fills)}",
        f"- **Commit:** `{snapshot.code_commit}` · generated {snapshot.generated_at_utc}",
    ]
    if snapshot.active_limits:
        rows.append("")
        rows.append("## Active limits")
        rows.extend(f"- **{label}:** {value}" for label, value in snapshot.active_limits)
    if snapshot.warnings:
        rows.append("")
        rows.append("## Warnings")
        rows.extend(f"- ⚠️ {w}" for w in snapshot.warnings)
    return "\n".join(rows)
