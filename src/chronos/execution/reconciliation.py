"""Execution-plane startup/reconnect reconciliation gate (Phase 9).

Before the execution engine may submit anything, broker evidence and the
durable ledger must agree. The gate:

1. collects broker open orders (by ``orderRef`` = intent id) and positions;
2. collects the ledger's working intents;
3. classifies discrepancies —
   - an open broker order the ledger does not know (UNKNOWN_BROKER_ORDER),
   - a working ledger intent the broker does not report (MISSING_BROKER_ORDER),
   - a position in a symbol the ledger cannot explain (UNEXPLAINED_POSITION),
   - an order present in *both* whose broker-reported state contradicts the
     ledger's latest known state (ORDER_STATE_MISMATCH) — this is the
     state-level check that pure id-set membership misses;
4. returns a report. ANY material discrepancy leaves ``passed=False`` and the
   caller must keep ``ExecutionEngine.reconciliation_passed`` False and raise
   a halt for operator review. There is no auto-flatten: an unknown position
   blocks trading rather than triggering an emergency order (Phase 9 rule).

State-level comparison (ORDER_STATE_MISMATCH) is deliberately coarsened: it
compares a lifecycle *bucket* (working / filled / closed) plus the cumulative
filled quantity, not the exact ``IntentStatus`` enum. This flags the material
disagreements — a broker fill the ledger missed, a fill-count drift, a
broker cancellation the ledger thinks is still working — without false-firing
on benign lags like PreSubmitted-vs-Submitted that are both "working, unfilled".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from chronos.execution.intents import IntentStatus


class DiscrepancyKind(StrEnum):
    UNKNOWN_BROKER_ORDER = "UNKNOWN_BROKER_ORDER"
    MISSING_BROKER_ORDER = "MISSING_BROKER_ORDER"
    UNEXPLAINED_POSITION = "UNEXPLAINED_POSITION"
    ORDER_STATE_MISMATCH = "ORDER_STATE_MISMATCH"


class LifecycleBucket(StrEnum):
    WORKING = "WORKING"
    FILLED = "FILLED"
    CLOSED = "CLOSED"
    OTHER = "OTHER"


_WORKING_STATUSES = frozenset(
    {
        IntentStatus.SUBMITTED,
        IntentStatus.PRE_SUBMITTED,
        IntentStatus.ACKNOWLEDGED,
        IntentStatus.PARTIALLY_FILLED,
        IntentStatus.PENDING_CANCEL,
    }
)
_CLOSED_STATUSES = frozenset({IntentStatus.CANCELLED, IntentStatus.REJECTED, IntentStatus.INACTIVE})


def lifecycle_bucket(status: IntentStatus) -> LifecycleBucket:
    """Coarsen an IntentStatus to a comparison bucket."""

    if status is IntentStatus.FILLED:
        return LifecycleBucket.FILLED
    if status in _WORKING_STATUSES:
        return LifecycleBucket.WORKING
    if status in _CLOSED_STATUSES:
        return LifecycleBucket.CLOSED
    return LifecycleBucket.OTHER


@dataclass(frozen=True, slots=True)
class OrderStateView:
    """One side's view of an order's state, for the state-level comparison."""

    status: IntentStatus
    filled_quantity: int

    @property
    def bucket(self) -> LifecycleBucket:
        return lifecycle_bucket(self.status)


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
    broker_order_states: dict[str, OrderStateView] | None = None,
    ledger_order_states: dict[str, OrderStateView] | None = None,
) -> ReconciliationReport:
    """Pure comparison; callers gather evidence and persist the outcome.

    ``broker_order_states`` / ``ledger_order_states`` are optional. When both
    are supplied, orders present in *both* sides' state maps are compared at
    the (bucket, filled_quantity) level and any disagreement is reported as
    ORDER_STATE_MISMATCH. When omitted, behaviour is exactly the id-set /
    position comparison (backward compatible).
    """

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

    if broker_order_states is not None and ledger_order_states is not None:
        for reference in sorted(set(broker_order_states) & set(ledger_order_states)):
            broker_view = broker_order_states[reference]
            ledger_view = ledger_order_states[reference]
            if (broker_view.bucket, broker_view.filled_quantity) != (
                ledger_view.bucket,
                ledger_view.filled_quantity,
            ):
                discrepancies.append(
                    Discrepancy(
                        kind=DiscrepancyKind.ORDER_STATE_MISMATCH,
                        reference=reference,
                        detail=(
                            "broker state "
                            f"({broker_view.bucket.value}, filled {broker_view.filled_quantity}) "
                            "contradicts ledger state "
                            f"({ledger_view.bucket.value}, filled {ledger_view.filled_quantity}); "
                            "blocking trading until resolved"
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
