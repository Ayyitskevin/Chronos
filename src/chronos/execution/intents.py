"""Order intents: the only bridge from portfolio decisions to broker orders.

An intent is created by the portfolio layer from a strategy proposal, priced
by an execution policy, validated by the risk engine, and only then handed to
the execution engine. Its identity (``intent_id``) is a deterministic UUIDv5
of its economic content, which is the duplicate-suppression key: replaying the
same proposal on the same bar yields the same intent id, and the order ledger
refuses to submit an intent id twice.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from chronos.domain.enums import OrderSide

# Fixed namespace for deterministic intent ids. Never change this value:
# historical audit records depend on it.
_INTENT_NAMESPACE = uuid.UUID("b1c5a7e4-83d2-45f9-9a61-2f60c3a8d9e7")


class TimeInForce(StrEnum):
    DAY = "DAY"


class IntentStatus(StrEnum):
    """Lifecycle of an order intent, superset of broker order states (Phase 9)."""

    CREATED = "CREATED"
    RISK_REJECTED = "RISK_REJECTED"
    RISK_APPROVED = "RISK_APPROVED"
    PENDING_SUBMISSION = "PENDING_SUBMISSION"
    SUBMITTED = "SUBMITTED"
    PRE_SUBMITTED = "PRE_SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    INACTIVE = "INACTIVE"
    UNKNOWN = "UNKNOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """One fully specified, not-yet-validated equity order.

    Only limit orders exist in this build; there is deliberately no market
    order type. Prices are ``Decimal`` because they cross the money boundary.
    """

    strategy_id: str
    strategy_version: str
    symbol: str
    side: OrderSide
    quantity: int
    limit_price: Decimal
    stop_price: Decimal | None
    time_in_force: TimeInForce
    decision_timestamp_utc: datetime
    source_bar_sequence_id: str
    proposal_reason: str

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("intent quantity must be a positive whole number of shares")
        if self.limit_price <= 0:
            raise ValueError("intent limit price must be positive")
        if self.stop_price is not None and self.stop_price <= 0:
            raise ValueError("intent stop price must be positive when present")
        if self.decision_timestamp_utc.tzinfo is None:
            raise ValueError("intent decision timestamp must be timezone-aware")

    @property
    def intent_id(self) -> str:
        """Deterministic identity used for idempotent submission.

        Fields are length-prefixed (``len:value``) before joining so that no
        combination of embedded delimiters in the free-text fields
        (``strategy_id``, ``strategy_version``, ``symbol``,
        ``source_bar_sequence_id``) can make two economically-different intents
        hash to the same id. This is the duplicate-suppression key, so a
        collision would be a safety defect.
        """

        def field(value: str) -> str:
            return f"{len(value)}:{value}"

        content = "|".join(
            (
                field(self.strategy_id),
                field(self.strategy_version),
                field(self.symbol),
                field(self.side.value),
                field(str(self.quantity)),
                field(str(self.limit_price)),
                field(str(self.stop_price) if self.stop_price is not None else "-"),
                field(self.time_in_force.value),
                field(self.source_bar_sequence_id),
            )
        )
        return str(uuid.uuid5(_INTENT_NAMESPACE, content))

    @property
    def notional(self) -> Decimal:
        return self.limit_price * self.quantity
