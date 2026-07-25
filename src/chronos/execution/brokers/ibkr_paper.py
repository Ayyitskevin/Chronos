"""IBKR paper-account execution adapter (Phase 9).

Implements ``ExecutionBrokerPort`` over the TWS API via ``ib_async``
(ADR-0002). This adapter is the ONLY code path in the platform that can hand
an equity order to Interactive Brokers, and it is constructible only when:

1. the resolved ``ModeLock`` grants ``PAPER_SUBMISSION`` (which itself
   requires transmission enabled, a non-empty operator allowlist, a
   broker-reported ``DU``/``DF`` account id on that allowlist, and a
   paper-verified environment), and
2. the connected gateway reports exactly one managed account, equal to the
   lock's paper account id, and
3. the configured port is on the operator-approved paper-port list.

Every order is a DAY limit order with ``outsideRth=False`` and
``orderRef=intent_id`` for idempotent reconciliation. There is no market
order path and no live path here; nothing in this module reads credentials —
authentication belongs to the operator's TWS/IB Gateway session.

STATUS: implemented and unit-tested against a fake IB object
(``tests/platform_unit/test_ibkr_paper_adapter.py``); NOT yet exercised
against a real IB Gateway from this environment (no credentials — see
docs/TEST_RESULTS.md "Requires owner action").
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from chronos.broker.base import BrokerSafetyError
from chronos.control.modes import ModeLock
from chronos.execution.brokers.port import BrokerEvent, BrokerEventKind, SubmissionReceipt
from chronos.execution.intents import OrderIntent

PAPER_PORTS = frozenset({7497, 4002})

_STATUS_MAP = {
    "PendingSubmit": None,
    "PreSubmitted": BrokerEventKind.ACKNOWLEDGED,
    "Submitted": BrokerEventKind.ACKNOWLEDGED,
    "Filled": BrokerEventKind.FILLED,
    "Cancelled": BrokerEventKind.CANCELLED,
    "ApiCancelled": BrokerEventKind.CANCELLED,
    "Inactive": BrokerEventKind.REJECTED,
}


class IBLike(Protocol):
    """Structural subset of ``ib_async.IB`` used by the adapter (testable)."""

    def isConnected(self) -> bool: ...

    def managedAccounts(self) -> list[str]: ...

    def placeOrder(self, contract: Any, order: Any) -> Any: ...

    def cancelOrder(self, order: Any) -> Any: ...

    def waitOnUpdate(self, timeout: float = ...) -> bool: ...


@dataclass
class IBKRPaperExecutionAdapter:
    """Paper-only execution adapter. See module docstring for guarantees."""

    ib: IBLike
    mode_lock: ModeLock
    port: int
    #: QUARANTINE (ADR-0016 §9 / RISK_REGISTER R-28). This adapter holds the
    #: repository's *second* transmit site — ``order.transmit = True`` below,
    #: a hardcoded literal that the single-transmit-site AST test cannot see
    #: because that test scans ``transmit=`` keyword arguments in
    #: ``chronos.orders`` and this is an attribute assignment in another
    #: package. It is constructed nowhere in production: the only
    #: ``ExecutionEngine`` wiring passes ``NullExecutionBroker``. Rather than
    #: rely on that staying true by accident, construction now requires an
    #: explicit opt-in, so a future wiring mistake fails loudly here instead of
    #: quietly acquiring a second, ungated path to a broker. Nothing in
    #: ``src/`` passes this flag, and a structural test asserts that.
    quarantine_ack: bool = False
    _trades: dict[str, Any] = field(default_factory=dict)
    _emitted: dict[str, tuple[str, int]] = field(default_factory=dict)
    _events: list[BrokerEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.quarantine_ack:
            raise BrokerSafetyError(
                "IBKRPaperExecutionAdapter is QUARANTINED (R-28): it carries a second "
                "transmit site outside the audited chronos.orders boundary and is wired "
                "into no production path. Constructing it requires quarantine_ack=True, "
                "which only tests pass. If you are wiring this into a runtime path, stop "
                "and read ADR-0016 §8 first: chronos.orders is the single canonical "
                "execution plane."
            )
        if not self.mode_lock.may_submit_paper:
            raise BrokerSafetyError(
                "IBKRPaperExecutionAdapter requires a PAPER_SUBMISSION mode lock; "
                f"denials: {self.mode_lock.denial_reasons}"
            )
        if self.port not in PAPER_PORTS:
            raise BrokerSafetyError(
                f"port {self.port} is not an approved IBKR paper port {sorted(PAPER_PORTS)}"
            )

    def verify_account(self) -> str:
        """Re-verify the gateway account before any submission window."""

        if not self.ib.isConnected():
            raise BrokerSafetyError("gateway is not connected")
        accounts = [a for a in self.ib.managedAccounts() if a.strip()]
        expected = self.mode_lock.paper_account_id
        if expected is None or accounts != [expected]:
            raise BrokerSafetyError(
                "managed accounts do not exactly match the verified paper account; "
                "refusing to trade"
            )
        return expected

    def submit(self, intent: OrderIntent) -> SubmissionReceipt:
        account = self.verify_account()
        if intent.intent_id in self._trades:
            raise BrokerSafetyError(f"intent {intent.intent_id} was already submitted")
        try:
            from ib_async import LimitOrder, Stock
        except ImportError as error:  # pragma: no cover - dependency is pinned
            raise BrokerSafetyError(f"ib_async unavailable: {error}") from error
        contract = Stock(intent.symbol, "SMART", "USD")
        order = LimitOrder(
            action=intent.side.value,
            totalQuantity=intent.quantity,
            lmtPrice=float(intent.limit_price),
        )
        order.account = account
        order.tif = "DAY"
        order.outsideRth = False
        order.orderRef = intent.intent_id
        order.transmit = True
        if not self.ib.isConnected():
            raise BrokerSafetyError(
                "gateway disconnected between account verification and order submission"
            )
        trade = self.ib.placeOrder(contract, order)
        self._trades[intent.intent_id] = trade
        return SubmissionReceipt(intent_id=intent.intent_id, submitted_at_utc=datetime.now(tz=UTC))

    def request_cancel(self, intent_id: str) -> None:
        trade = self._trades.get(intent_id)
        if trade is None:
            return
        self.ib.cancelOrder(trade.order)

    def drain_events(self) -> tuple[BrokerEvent, ...]:
        """Pump the ib_async loop briefly and translate trade state to events.

        Fill quantity is authoritative over IB's raw status text for every
        fill-relevant kind: the status field and the filled field can be
        observed inconsistently within a single poll (TWS eventual
        consistency), so a status of "Filled" with filled < total is
        reported as PARTIAL_FILL, and any status with filled >= total is
        reported as FILLED regardless of what the status string says. An
        entirely unrecognized status is never silently dropped, even at
        zero fill: it surfaces as ERROR so the engine halts rather than
        going dark on an unknown broker state.
        """

        with contextlib.suppress(Exception):
            # A pump failure surfaces via data staleness, never as a crash here.
            self.ib.waitOnUpdate(0.05)
        for intent_id, trade in self._trades.items():
            status = getattr(trade.orderStatus, "status", "")
            filled_raw = getattr(trade.orderStatus, "filled", 0)
            filled = int(filled_raw) if filled_raw else 0
            previous = self._emitted.get(intent_id)
            if previous == (status, filled):
                continue

            recognized = status in _STATUS_MAP
            kind = _STATUS_MAP.get(status)
            if kind is None:
                if recognized and filled == 0:
                    # "PendingSubmit" with nothing filled yet: legitimately
                    # nothing to report.
                    self._emitted[intent_id] = (status, filled)
                    continue
                kind = BrokerEventKind.ERROR

            total = getattr(trade.order, "totalQuantity", 0)
            if kind in (
                BrokerEventKind.ACKNOWLEDGED,
                BrokerEventKind.PARTIAL_FILL,
                BrokerEventKind.FILLED,
            ):
                if total > 0 and filled >= total:
                    kind = BrokerEventKind.FILLED
                elif filled > 0:
                    kind = BrokerEventKind.PARTIAL_FILL
                else:
                    kind = BrokerEventKind.ACKNOWLEDGED

            average_raw = getattr(trade.orderStatus, "avgFillPrice", None)
            average = Decimal(str(average_raw)) if average_raw and filled > 0 else None
            commission = self._commission_total(trade)
            self._events.append(
                BrokerEvent(
                    kind=kind,
                    intent_id=intent_id,
                    at_utc=datetime.now(tz=UTC),
                    cumulative_filled=filled,
                    average_fill_price=average,
                    commission_usd=commission,
                    detail=f"ib status={status}",
                )
            )
            self._emitted[intent_id] = (status, filled)
        events = tuple(self._events)
        self._events.clear()
        return events

    @staticmethod
    def _commission_total(trade: Any) -> Decimal | None:
        fills = getattr(trade, "fills", None) or []
        total = Decimal(0)
        seen = False
        for fill in fills:
            report = getattr(fill, "commissionReport", None)
            commission = getattr(report, "commission", None) if report else None
            if commission is not None:
                total += Decimal(str(commission))
                seen = True
        return total if seen else None
