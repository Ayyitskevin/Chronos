"""Read-only Streamlit page for the platform monitor.

This page renders a :class:`~chronos.monitoring.snapshot.MonitoringSnapshot` and
nothing else. It imports no broker adapter, opens no market-data connection,
and exposes no control that can arm, halt, submit, modify, or cancel an order —
it is a window onto persisted state, not a cockpit. Configuration is read from
environment variables so the page stays a pure function of files on disk:

    CHRONOS_MONITOR_MODE   trading mode to resolve the lock banner (default: shadow)
    CHRONOS_HALT_FILE      halt-state file            (default: data/platform_halt.json)
    CHRONOS_AUDIT_FILE     audit log                  (default: data/platform_audit.jsonl)
    CHRONOS_RISK_POLICY    risk policy yaml           (default: config/risk.example.yaml)
    CHRONOS_DATA_DIR       daily-bar CSV directory    (default: research/data/raw)
    CHRONOS_MONITOR_SYMBOLS  comma-separated symbols  (default: SPY,QQQ)
    CHRONOS_LEDGER_FILE    SQLite execution ledger, read-only (optional)

Launch with ``streamlit run src/chronos/monitoring/streamlit_app.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from chronos.control.modes import TradingMode
from chronos.monitoring.snapshot import MonitoringSnapshot, build_snapshot

_DEFAULT_HALT = "data/platform_halt.json"
_DEFAULT_AUDIT = "data/platform_audit.jsonl"
_DEFAULT_POLICY = "config/risk.example.yaml"
_DEFAULT_DATA_DIR = "research/data/raw"
_DEFAULT_SYMBOLS = "SPY,QQQ"


def _config_snapshot() -> MonitoringSnapshot:
    mode_name = os.environ.get("CHRONOS_MONITOR_MODE", "shadow").strip().lower()
    try:
        mode = TradingMode(mode_name)
    except ValueError:
        mode = TradingMode.SHADOW
    symbols = tuple(
        s.strip().upper()
        for s in os.environ.get("CHRONOS_MONITOR_SYMBOLS", _DEFAULT_SYMBOLS).split(",")
        if s.strip()
    )
    ledger_env = os.environ.get("CHRONOS_LEDGER_FILE", "").strip()
    return build_snapshot(
        mode=mode,
        halt_file=Path(os.environ.get("CHRONOS_HALT_FILE", _DEFAULT_HALT)),
        audit_file=Path(os.environ.get("CHRONOS_AUDIT_FILE", _DEFAULT_AUDIT)),
        risk_policy_path=Path(os.environ.get("CHRONOS_RISK_POLICY", _DEFAULT_POLICY)),
        data_dir=Path(os.environ.get("CHRONOS_DATA_DIR", _DEFAULT_DATA_DIR)),
        symbols=symbols,
        ledger_path=Path(ledger_env) if ledger_env else None,
    )


def render_monitor(snapshot: MonitoringSnapshot) -> None:
    """Render one snapshot with Streamlit primitives (no side effects on state)."""

    st.title("Chronos Platform Monitor")
    st.caption("Read-only view of persisted platform state — no order controls exist on this page.")

    # Mode / capability must be legible from text alone, never colour alone.
    st.subheader(snapshot.mode_banner)
    if snapshot.live_capable:
        st.error("LIVE-CAPABLE — this build should never report this. Investigate immediately.")
    else:
        st.success("Live trading is hard-disabled in this build (no override exists).")

    if snapshot.halted:
        st.error(
            f"🛑 HALTED — {snapshot.halt_reason or 'unknown'}"
            + (f" ({snapshot.halt_detail})" if snapshot.halt_detail else "")
        )
    else:
        st.info(
            "🟢 Armed (not halted). Order generation still requires capability and reconciliation."
        )

    columns = st.columns(3)
    columns[0].metric("Mode", snapshot.mode.upper())
    columns[1].metric("Capability", snapshot.capability)
    columns[2].metric("Reconciliation", snapshot.reconciliation_status)

    audit_columns = st.columns(3)
    audit_columns[0].metric("Audit chain", "OK" if snapshot.audit_ok else "FAILED")
    audit_columns[1].metric("Audit records", str(snapshot.audit_records))
    audit_columns[2].metric("Code commit", snapshot.code_commit)
    st.caption(f"audit detail: {snapshot.audit_detail}")

    st.subheader("Risk policy")
    st.write(
        {
            "version": snapshot.risk_policy_version or "none",
            "hash": snapshot.risk_policy_hash or "-",
        }
    )
    if snapshot.active_limits:
        st.dataframe(
            [{"Limit": label, "Value": value} for label, value in snapshot.active_limits],
            hide_index=True,
        )

    st.subheader("Positions, orders & fills")
    st.caption(snapshot.pnl_note)
    if snapshot.net_positions:
        st.dataframe(
            [
                {"Symbol": position.symbol, "Net shares": position.net_shares}
                for position in snapshot.net_positions
            ],
            hide_index=True,
        )
    else:
        st.caption("No net positions.")
    if snapshot.open_orders:
        st.dataframe(
            [
                {
                    "Symbol": order.symbol,
                    "Side": order.side,
                    "Quantity": order.quantity,
                    "Filled": order.filled_quantity,
                    "Status": order.status,
                }
                for order in snapshot.open_orders
            ],
            hide_index=True,
        )
    else:
        st.caption("No open orders.")
    if snapshot.recent_fills:
        st.dataframe(
            [
                {
                    "Symbol": fill.symbol,
                    "Cumulative qty": fill.cumulative_quantity,
                    "Average price": fill.average_price or "-",
                    "At (UTC)": fill.at_utc,
                }
                for fill in snapshot.recent_fills
            ],
            hide_index=True,
        )
    else:
        st.caption("No recent fills.")

    if snapshot.data_freshness:
        st.subheader("Data freshness")
        st.dataframe(
            [
                {
                    "Symbol": freshness.symbol,
                    "Latest session": freshness.latest_session or "MISSING",
                    "Bars": freshness.bars,
                }
                for freshness in snapshot.data_freshness
            ],
            hide_index=True,
        )

    if snapshot.recent_decisions:
        st.subheader("Recent audit events")
        st.dataframe(
            [
                {
                    "Kind": str(record.get("kind")),
                    "Payload": str(record.get("payload", {})),
                }
                for record in snapshot.recent_decisions
            ],
            hide_index=True,
        )

    if snapshot.warnings:
        st.subheader("Warnings")
        for warning in snapshot.warnings:
            st.warning(warning)

    st.caption(f"generated {snapshot.generated_at_utc}")


def main() -> None:
    st.set_page_config(
        page_title="Chronos Platform Monitor",
        page_icon="🛰️",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    render_monitor(_config_snapshot())


if __name__ == "__main__":
    main()
