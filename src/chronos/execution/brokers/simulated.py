"""Deterministic simulated execution broker.

Used by backtest, replay, and the E2E/chaos suites. Fills are computed from
the next bar handed to ``advance_bar``: a buy limit fills when the bar's low
trades through the limit (fill at min(open, limit)), a sell limit fills when
the bar's high reaches it (fill at max(open, limit)). Day orders expire at the
end of the bar that followed submission. Fault injection supports rejection,
partial fills, duplicated events, dropped events, and silent acceptance
(submission without acknowledgement) for chaos testing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from chronos.domain.enums import OrderSide
from chronos.execution.brokers.port import BrokerEvent, BrokerEventKind, SubmissionReceipt
from chronos.execution.intents import OrderIntent
from chronos.marketdata.bars import Bar


@dataclass(frozen=True, slots=True)
class FaultPlan:
    """Deterministic fault injection keyed by intent id."""

    reject_intent_ids: frozenset[str] = frozenset()
    partial_fill_intent_ids: frozenset[str] = frozenset()
    duplicate_events_intent_ids: frozenset[str] = frozenset()
    drop_ack_intent_ids: frozenset[str] = frozenset()
    partial_fraction_numerator: int = 1
    partial_fraction_denominator: int = 2


@dataclass
class _WorkingOrder:
    intent: OrderIntent
    submitted_at_utc: datetime
    acknowledged: bool = False
    cumulative_filled: int = 0


@dataclass
class SimulatedExecutionBroker:
    """In-process deterministic broker implementing ExecutionBrokerPort."""

    commission_per_share: Decimal = Decimal("0.005")
    commission_minimum: Decimal = Decimal("1.00")
    faults: FaultPlan = field(default_factory=FaultPlan)
    _working: dict[str, _WorkingOrder] = field(default_factory=dict)
    _events: list[BrokerEvent] = field(default_factory=list)
    _seen_intent_ids: set[str] = field(default_factory=set)

    def submit(self, intent: OrderIntent) -> SubmissionReceipt:
        if intent.intent_id in self._seen_intent_ids:
            # A duplicate submission is broker-rejected, mirroring the ledger
            # guarantee; the sim must not quietly create a second order.
            self._events.append(
                BrokerEvent(
                    kind=BrokerEventKind.REJECTED,
                    intent_id=intent.intent_id,
                    at_utc=intent.decision_timestamp_utc,
                    cumulative_filled=0,
                    average_fill_price=None,
                    commission_usd=None,
                    detail="duplicate intent id",
                )
            )
            return SubmissionReceipt(
                intent_id=intent.intent_id, submitted_at_utc=intent.decision_timestamp_utc
            )
        self._seen_intent_ids.add(intent.intent_id)

        if intent.intent_id in self.faults.reject_intent_ids:
            self._events.append(
                BrokerEvent(
                    kind=BrokerEventKind.REJECTED,
                    intent_id=intent.intent_id,
                    at_utc=intent.decision_timestamp_utc,
                    cumulative_filled=0,
                    average_fill_price=None,
                    commission_usd=None,
                    detail="injected rejection",
                )
            )
            return SubmissionReceipt(
                intent_id=intent.intent_id, submitted_at_utc=intent.decision_timestamp_utc
            )

        order = _WorkingOrder(intent=intent, submitted_at_utc=intent.decision_timestamp_utc)
        self._working[intent.intent_id] = order
        if intent.intent_id not in self.faults.drop_ack_intent_ids:
            order.acknowledged = True
            self._emit(
                BrokerEvent(
                    kind=BrokerEventKind.ACKNOWLEDGED,
                    intent_id=intent.intent_id,
                    at_utc=intent.decision_timestamp_utc,
                    cumulative_filled=0,
                    average_fill_price=None,
                    commission_usd=None,
                    detail="accepted",
                ),
                intent.intent_id,
            )
        return SubmissionReceipt(
            intent_id=intent.intent_id, submitted_at_utc=intent.decision_timestamp_utc
        )

    def request_cancel(self, intent_id: str) -> None:
        order = self._working.pop(intent_id, None)
        if order is None:
            return
        self._emit(
            BrokerEvent(
                kind=BrokerEventKind.CANCELLED,
                intent_id=intent_id,
                at_utc=order.submitted_at_utc,
                cumulative_filled=order.cumulative_filled,
                average_fill_price=None,
                commission_usd=None,
                detail="cancel honoured",
            ),
            intent_id,
        )

    def drain_events(self) -> tuple[BrokerEvent, ...]:
        events = tuple(self._events)
        self._events.clear()
        return events

    def advance_bar(self, bar: Bar) -> None:
        """Evaluate all working orders against one newly closed bar."""

        for intent_id in list(self._working):
            order = self._working[intent_id]
            intent = order.intent
            if intent.symbol != bar.symbol:
                continue
            limit = float(intent.limit_price)
            fill_price: float | None = None
            if intent.side is OrderSide.BUY and bar.low <= limit:
                fill_price = min(bar.open, limit)
            elif intent.side is OrderSide.SELL and bar.high >= limit:
                fill_price = max(bar.open, limit)

            if fill_price is None:
                # Day order: expires after the bar following submission.
                del self._working[intent_id]
                self._emit(
                    BrokerEvent(
                        kind=BrokerEventKind.CANCELLED,
                        intent_id=intent_id,
                        at_utc=bar.timestamp_utc,
                        cumulative_filled=order.cumulative_filled,
                        average_fill_price=None,
                        commission_usd=None,
                        detail="day order expired unfilled",
                    ),
                    intent_id,
                )
                continue

            price = Decimal(str(fill_price)).quantize(Decimal("0.01"))
            if intent_id in self.faults.partial_fill_intent_ids and order.cumulative_filled == 0:
                partial = max(
                    1,
                    intent.quantity
                    * self.faults.partial_fraction_numerator
                    // self.faults.partial_fraction_denominator,
                )
                if partial < intent.quantity:
                    order.cumulative_filled = partial
                    self._emit(
                        BrokerEvent(
                            kind=BrokerEventKind.PARTIAL_FILL,
                            intent_id=intent_id,
                            at_utc=bar.timestamp_utc,
                            cumulative_filled=partial,
                            average_fill_price=price,
                            commission_usd=self._commission(partial),
                            detail="injected partial fill",
                        ),
                        intent_id,
                    )
                    continue

            filled = intent.quantity
            del self._working[intent_id]
            self._emit(
                BrokerEvent(
                    kind=BrokerEventKind.FILLED,
                    intent_id=intent_id,
                    at_utc=bar.timestamp_utc,
                    cumulative_filled=filled,
                    average_fill_price=price,
                    commission_usd=self._commission(filled - order.cumulative_filled),
                    detail="filled",
                ),
                intent_id,
            )

    def open_order_intent_ids(self) -> tuple[str, ...]:
        return tuple(self._working)

    def _commission(self, shares: int) -> Decimal:
        return max(self.commission_per_share * shares, self.commission_minimum)

    def _emit(self, event: BrokerEvent, intent_id: str) -> None:
        self._events.append(event)
        if intent_id in self.faults.duplicate_events_intent_ids:
            self._events.append(event)
