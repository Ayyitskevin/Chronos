"""Read-only platform monitoring plane (Phase 15).

Surfaces the platform's persisted state — operating mode and live-lock status,
halt reason, reconciliation outcome, audit-chain integrity, data freshness,
recent shadow decisions, and code commit — for a local operator. It reads
state files only: it makes no broker call, opens no market-data connection,
and contains no trading logic or submission path. The package deliberately
imports no broker adapter, which a test enforces.

Paper and live are made visually unmistakable by an explicit textual label
(``mode_banner``) and a boolean ``live_capable`` flag, never by colour alone.
"""

from chronos.monitoring.snapshot import (
    MonitoringSnapshot,
    build_snapshot,
    render_markdown,
    render_text,
)

__all__ = [
    "MonitoringSnapshot",
    "build_snapshot",
    "render_markdown",
    "render_text",
]
