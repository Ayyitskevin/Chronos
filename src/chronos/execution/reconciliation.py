"""Execution-plane startup/reconnect reconciliation gate (Phase 9).

Before the execution engine may submit anything, broker evidence and the
durable ledger must agree. The gate:

1. collects broker open orders (by ``orderRef`` = intent id) and positions;
2. collects the ledger's working intents;
3. classifies discrepancies — an open broker order the ledger does not know,
   a working ledger intent the broker does not report, or any position in a
   symbol the ledger cannot explain;
4. returns a report. ANY material discrepancy leaves ``passed=False`` and the
   caller must keep ``ExecutionEngine.reconciliation_passed`` False and raise
   a halt for operator review. There is no auto-flatten: an unknown position
   blocks trading rather than triggering an emergency order (Phase 9 rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DiscrepancyKind(StrEnum):
    UNKNOWN_BROKER_ORDER = "UNKNOWN_BROKER_ORDER"
    MISSING_BROKER_ORDER = "MISSING_BROKER_ORDER"
    UNEXPLAINED_POSITION = "UNEXPLAINED_POSITION"


@dataclass(frozen=True, slots=True)
class Discrepancy:
    kind: DiscrepancyKind
    reference: str
    detail: str


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    passed: bool
    discrepancies: tuple[Discrepancy, ...]
    broker_open_order_refs: tuple[str, ...]
    ledger_working_intent_ids: tuple[str, ...]
    broker_position_symbols: tuple[str, ...]


def reconcile(
    *,
    broker_open_order_refs: tuple[str, ...],
    broker_positions: dict[str, int],
    ledger_working_intent_ids: tuple[str, ...],
    explained_position_symbols: frozenset[str],
) -> ReconciliationReport:
    """Pure comparison; callers gather evidence and persist the outcome."""

    discrepancies: list[Discrepancy] = []
    broker_set = set(broker_open_order_refs)
    ledger_set = set(ledger_working_intent_ids)

    for reference in sorted(broker_set - ledger_set):
        discrepancies.append(
            Discrepancy(
                kind=DiscrepancyKind.UNKNOWN_BROKER_ORDER,
                reference=reference,
                detail="broker reports an open order this ledger never submitted",
            )
        )
    for reference in sorted(ledger_set - broker_set):
        discrepancies.append(
            Discrepancy(
                kind=DiscrepancyKind.MISSING_BROKER_ORDER,
                reference=reference,
                detail=(
                    "ledger believes this intent is working but the broker does not "
                    "report it; requires manual status resolution"
                ),
            )
        )
    for symbol, shares in sorted(broker_positions.items()):
        if shares != 0 and symbol not in explained_position_symbols:
            discrepancies.append(
                Discrepancy(
                    kind=DiscrepancyKind.UNEXPLAINED_POSITION,
                    reference=symbol,
                    detail=(
                        f"broker reports {shares} shares that the ledger cannot map "
                        "to a Chronos fill; blocking trading (no auto-flatten)"
                    ),
                )
            )

    return ReconciliationReport(
        passed=not discrepancies,
        discrepancies=tuple(discrepancies),
        broker_open_order_refs=tuple(sorted(broker_set)),
        ledger_working_intent_ids=tuple(sorted(ledger_set)),
        broker_position_symbols=tuple(sorted(broker_positions)),
    )
