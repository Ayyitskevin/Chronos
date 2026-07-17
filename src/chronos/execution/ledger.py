"""Order-intent ledgers.

``MemoryLedger`` backs backtests and unit tests. ``SqliteLedger`` (persistence
package) backs shadow/paper runs; both are write-through and duplicate-safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from chronos.execution.intents import IntentStatus, OrderIntent

_WORKING_STATUSES = frozenset(
    {
        IntentStatus.SUBMITTED,
        IntentStatus.PRE_SUBMITTED,
        IntentStatus.ACKNOWLEDGED,
        IntentStatus.PARTIALLY_FILLED,
        IntentStatus.PENDING_CANCEL,
    }
)


@dataclass(frozen=True, slots=True)
class LedgerFill:
    intent_id: str
    cumulative_quantity: int
    average_price: Decimal | None
    commission_usd: Decimal | None
    at_utc: datetime


@dataclass(frozen=True, slots=True)
class LedgerTransition:
    intent_id: str
    status: IntentStatus
    at_utc: datetime
    evidence: str


@dataclass
class MemoryLedger:
    """In-process LedgerPort implementation with full history retention."""

    intents: dict[str, OrderIntent] = field(default_factory=dict)
    statuses: dict[str, IntentStatus] = field(default_factory=dict)
    transitions: list[LedgerTransition] = field(default_factory=list)
    fills: list[LedgerFill] = field(default_factory=list)
    fail_writes: bool = False

    def record_intent(self, intent: OrderIntent, status: IntentStatus) -> None:
        if self.fail_writes:
            raise OSError("injected ledger failure")
        if intent.intent_id in self.intents:
            raise ValueError(f"duplicate intent id {intent.intent_id}")
        self.intents[intent.intent_id] = intent
        self.statuses[intent.intent_id] = status

    def has_intent(self, intent_id: str) -> bool:
        if self.fail_writes:
            raise OSError("injected ledger failure")
        return intent_id in self.intents

    def record_transition(
        self, intent_id: str, status: IntentStatus, at_utc: datetime, evidence: str
    ) -> None:
        if self.fail_writes:
            raise OSError("injected ledger failure")
        self.statuses[intent_id] = status
        self.transitions.append(
            LedgerTransition(intent_id=intent_id, status=status, at_utc=at_utc, evidence=evidence)
        )

    def record_fill(
        self,
        intent_id: str,
        cumulative_quantity: int,
        average_price: Decimal | None,
        commission_usd: Decimal | None,
        at_utc: datetime,
    ) -> None:
        if self.fail_writes:
            raise OSError("injected ledger failure")
        self.fills.append(
            LedgerFill(
                intent_id=intent_id,
                cumulative_quantity=cumulative_quantity,
                average_price=average_price,
                commission_usd=commission_usd,
                at_utc=at_utc,
            )
        )

    def working_intent_ids(self) -> tuple[str, ...]:
        return tuple(
            intent_id for intent_id, status in self.statuses.items() if status in _WORKING_STATUSES
        )

    def working_order_snapshots(self) -> dict[str, tuple[IntentStatus, int]]:
        """Latest (status, cumulative filled) for each working intent."""

        filled: dict[str, int] = {}
        for fill in self.fills:
            filled[fill.intent_id] = max(filled.get(fill.intent_id, 0), fill.cumulative_quantity)
        return {
            intent_id: (status, filled.get(intent_id, 0))
            for intent_id, status in self.statuses.items()
            if status in _WORKING_STATUSES
        }
