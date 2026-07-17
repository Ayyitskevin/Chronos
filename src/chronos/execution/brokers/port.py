"""Execution broker port: the narrow protocol execution adapters implement.

The port is event-oriented. ``submit`` returns only a receipt that the request
left the process; every state claim (acceptance, fill, cancel, rejection)
arrives as a ``BrokerEvent`` and is applied to the order state machine. The
execution engine never infers success from a call returning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from chronos.execution.intents import OrderIntent


class BrokerEventKind(StrEnum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class BrokerEvent:
    kind: BrokerEventKind
    intent_id: str
    at_utc: datetime
    cumulative_filled: int
    average_fill_price: Decimal | None
    commission_usd: Decimal | None
    detail: str


@dataclass(frozen=True, slots=True)
class SubmissionReceipt:
    """Evidence only that a submission request was handed to the adapter."""

    intent_id: str
    submitted_at_utc: datetime


@runtime_checkable
class ExecutionBrokerPort(Protocol):
    """Minimal, event-driven execution adapter surface."""

    def submit(self, intent: OrderIntent) -> SubmissionReceipt: ...

    def request_cancel(self, intent_id: str) -> None: ...

    def drain_events(self) -> tuple[BrokerEvent, ...]:
        """Return and clear pending events in arrival order."""
        ...
