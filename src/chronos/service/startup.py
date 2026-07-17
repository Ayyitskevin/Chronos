"""Service startup / reconnect sequence.

Order matters and is the safety-critical part of the service:

1. Read the persistent halt. If halted, stay halted — do not proceed.
2. Hydrate in-flight orders from the durable ledger (R-23) so their broker
   events reconcile instead of triggering evidence-losing UNKNOWN_ORDER halts.
3. Gather broker evidence. A disconnected broker cannot be reconciled against,
   so it blocks and halts.
4. Reconcile ledger and broker at the state level (R-22).
5. Set ``engine.reconciliation_passed`` True **only** on a clean report while
   not halted and connected. Any discrepancy stays False and raises a halt for
   operator review — there is no auto-flatten.

Every outcome is written to the audit log. ``reconciliation_passed`` is never
set True except through this clean path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from chronos.control.halt import HaltReason
from chronos.execution.reconciliation import (
    OrderStateView,
    ReconciliationReport,
    reconcile,
)
from chronos.service.runtime import ServiceComponents


@dataclass(frozen=True, slots=True)
class StartupReport:
    halted: bool
    reconciliation_passed: bool
    hydrated_orders: int
    report: ReconciliationReport | None
    detail: str


def run_startup(components: ServiceComponents, *, now_utc: datetime | None = None) -> StartupReport:
    """Run the ordered startup gate; return an auditable report."""

    now = now_utc or datetime.now(tz=UTC)
    engine = components.engine
    engine.reconciliation_passed = False  # never trust a stale flag on (re)start

    halt = components.halt_store.read()
    if halt.trading_blocked:
        detail = f"startup while halted ({halt.reason.value if halt.reason else 'unknown'})"
        _audit(components, "service_startup", {"outcome": "halted", "detail": detail}, now)
        return StartupReport(
            halted=True, reconciliation_passed=False, hydrated_orders=0, report=None, detail=detail
        )

    snapshots = components.ledger.working_order_snapshots()
    hydrated = engine.hydrate_from_ledger(snapshots)

    evidence = components.evidence_source.gather()
    if not evidence.connected:
        components.halt_store.halt(
            HaltReason.CONNECTION_UNSTABLE, "broker not connected at startup; cannot reconcile"
        )
        _audit(components, "service_startup", {"outcome": "disconnected"}, now)
        return StartupReport(
            halted=True,
            reconciliation_passed=False,
            hydrated_orders=hydrated,
            report=None,
            detail="broker disconnected; halted",
        )

    ledger_states = {
        intent_id: OrderStateView(status=status, filled_quantity=filled)
        for intent_id, (status, filled) in snapshots.items()
    }
    report = reconcile(
        broker_open_order_refs=evidence.open_order_refs,
        broker_positions=evidence.positions,
        ledger_working_intent_ids=components.ledger.working_intent_ids(),
        explained_position_symbols=components.explained_position_symbols,
        broker_order_states=evidence.order_states,
        ledger_order_states=ledger_states,
    )

    if report.passed:
        engine.reconciliation_passed = True
        _audit(
            components,
            "service_startup",
            {"outcome": "reconciled", "hydrated": hydrated},
            now,
        )
        return StartupReport(
            halted=False,
            reconciliation_passed=True,
            hydrated_orders=hydrated,
            report=report,
            detail="reconciled clean; order generation permitted",
        )

    components.halt_store.halt(
        HaltReason.RECONCILIATION_MISMATCH,
        f"{len(report.discrepancies)} reconciliation discrepancy(ies) at startup",
    )
    _audit(
        components,
        "service_startup",
        {
            "outcome": "discrepancy",
            "count": len(report.discrepancies),
            "kinds": sorted({d.kind.value for d in report.discrepancies}),
        },
        now,
    )
    return StartupReport(
        halted=True,
        reconciliation_passed=False,
        hydrated_orders=hydrated,
        report=report,
        detail=f"{len(report.discrepancies)} discrepancy(ies); halted for review",
    )


def _audit(
    components: ServiceComponents, kind: str, payload: dict[str, object], now: datetime
) -> None:
    del now  # AuditLog stamps its own UTC time
    components.audit_log.append(kind, payload)
