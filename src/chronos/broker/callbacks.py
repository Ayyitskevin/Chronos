"""Thread-safe bridge between EWrapper-style callbacks and Chronos requests.

The official TWS API delivers everything on a reader thread through callback
methods. This bridge is deliberately **import-safe without the IBKR
package**: it receives plain primitives (the adapter's thin ``_App`` subclass
forwards callback arguments), routes request-scoped streams into the
:class:`~chronos.broker.request_registry.RequestRegistry`, and holds the
small amount of connection-level state that has no request ID
(``nextValidId``, managed accounts, current time, notices).

Sentinel hygiene: TWS uses ``-1``/huge-double sentinels for "no value".
Helpers here convert those to ``None`` so no fabricated number ever reaches a
domain model — a missing value stays missing (owner spec: never invent
market data).
"""

from __future__ import annotations

import logging
import math
import threading
from collections import deque
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from chronos.broker.request_registry import RequestRegistry
from chronos.domain.enums import DataQuality

# Benign, id-less TWS status notices (farm connectivity etc.) that must never
# fail a request: 2103-2108 market-data farm messages, 2119 connecting, 2158
# sec-def farm OK, 1101/1102 connectivity restored.
_BENIGN_CODES = frozenset({1101, 1102, 2103, 2104, 2105, 2106, 2107, 2108, 2119, 2158})

# Loss and restoration both invalidate evidence from the prior TWS session.
_CONNECTION_UNCERTAIN_CODES = frozenset({1100, 1101, 1102, 1300, 2110})

_MARKET_DATA_TYPE_MAP = {
    1: DataQuality.LIVE,
    2: DataQuality.FROZEN,
    3: DataQuality.DELAYED,
    4: DataQuality.DELAYED_FROZEN,
}

# Tick ids per the TWS API tick-type table.
_TICK_BID = frozenset({1, 66})
_TICK_ASK = frozenset({2, 67})
_TICK_LAST = frozenset({4, 68})
_TICK_CLOSE = frozenset({9, 75})
_TICK_VOLUME = frozenset({8, 74})
_TICK_OPTION_CALL_OI = 27
_TICK_OPTION_PUT_OI = 28
_TICK_MODEL_OPTION = frozenset({13, 83})

_UNSET_THRESHOLD = 1e100  # ibapi UNSET_DOUBLE is ~1.798e308


def clean_price(value: float | None) -> Decimal | None:
    """Convert a TWS price to Decimal, mapping sentinels/invalid to None."""

    if value is None or not math.isfinite(value) or value <= 0 or value >= _UNSET_THRESHOLD:
        return None
    return Decimal(str(value))


def clean_size(value: float | None) -> int | None:
    if value is None or not math.isfinite(value) or value < 0 or value >= _UNSET_THRESHOLD:
        return None
    return int(value)


def clean_ratio(value: float | None, *, low: float, high: float) -> Decimal | None:
    if value is None or not math.isfinite(value) or not (low <= value <= high):
        return None
    return Decimal(str(value))


@dataclass(slots=True)
class QuoteState:
    """Per-request quote accumulation from tick callbacks."""

    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    close: Decimal | None = None
    volume: int | None = None
    open_interest: int | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    implied_volatility: Decimal | None = None
    data_quality: DataQuality = DataQuality.UNKNOWN


@dataclass(slots=True)
class _SingleFlight:
    """One id-less request in progress (positions, open orders, current time)."""

    items: list[Any] = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)


class CallbackBridge:
    """Connection-level state and callback routing for the official adapter."""

    def __init__(
        self,
        registry: RequestRegistry,
        *,
        on_connection_uncertain: Callable[[str], object] | None = None,
        on_managed_account_scope_change: (
            Callable[[str], AbstractContextManager[None]] | None
        ) = None,
    ) -> None:
        self.registry = registry
        self._on_connection_uncertain = on_connection_uncertain
        self._on_managed_account_scope_change = on_managed_account_scope_change
        self._logger = logging.getLogger("chronos.broker.callbacks")
        self._lock = threading.Lock()
        self.connected_event = threading.Event()
        self.next_valid_id: int | None = None
        self.managed_accounts: tuple[str, ...] = ()
        self.managed_accounts_event = threading.Event()
        self.connection_closed = threading.Event()
        self.notices: deque[str] = deque(maxlen=200)
        self._current_time: _SingleFlight | None = None
        self._positions: _SingleFlight | None = None
        self._open_orders: _SingleFlight | None = None
        self._order_acks: dict[int, _SingleFlight] = {}
        self.order_statuses: dict[int, tuple[str, float, float]] = {}
        self._quotes: dict[int, QuoteState] = {}
        self._commissions: dict[str, tuple[Decimal | None, str | None]] = {}
        self._market_rules: dict[int, list[tuple[Decimal, Decimal]]] = {}
        self._market_rule_events: dict[int, threading.Event] = {}

    # ------------------------------------------------------------------ #
    # Connection-level callbacks
    # ------------------------------------------------------------------ #

    def on_next_valid_id(self, order_id: int) -> None:
        with self._lock:
            self.next_valid_id = order_id
        self.connected_event.set()

    def on_managed_accounts(self, accounts_csv: str) -> None:
        accounts = tuple(part.strip() for part in accounts_csv.split(",") if part.strip())
        with self._lock:
            previous = self.managed_accounts
        scope_changed = bool(previous) and frozenset(accounts) != frozenset(previous)
        if scope_changed and self._on_managed_account_scope_change is not None:
            with (
                self._on_managed_account_scope_change(
                    "official IBKR managed-account scope changed"
                ),
                self._lock,
            ):
                self.managed_accounts = accounts
        else:
            if scope_changed:
                self._invalidate_connection("official IBKR managed-account scope changed")
            with self._lock:
                self.managed_accounts = accounts
        self.managed_accounts_event.set()

    def on_connection_closed(self) -> None:
        self._invalidate_connection("official IBKR connection closed")
        self.connection_closed.set()
        # Fail everything in flight rather than letting waiters time out slowly.
        self._fail_all_single_flights("connection closed")

    def on_error(self, req_id: int, code: int, message: str) -> None:
        if code in _CONNECTION_UNCERTAIN_CODES:
            self._invalidate_connection(f"official IBKR connectivity event {code}")
        if code in _BENIGN_CODES or req_id < 0:
            with self._lock:
                self.notices.append(f"[{code}] {message}")
            return
        # Order-path errors arrive keyed by ORDER id, not request id, and many
        # venue rejects come ONLY through this callback (M7 review finding H2):
        # complete the matching ack flight so submit/preview see the reject
        # immediately instead of stranding on a timeout.
        with self._lock:
            ack = self._order_acks.get(req_id)
        if ack is not None and not ack.done.is_set():
            ack.items.append(("error", req_id, code, message))
            ack.done.set()
            return
        self.registry.fail(req_id, code, message)

    def _invalidate_connection(self, reason: str) -> None:
        callback = self._on_connection_uncertain
        if callback is None:
            return
        try:
            callback(reason)
        except Exception:
            self._logger.exception(
                "Connection uncertainty observer failed",
                extra={"event": "connection_uncertainty_observer_failed"},
            )

    # ------------------------------------------------------------------ #
    # Single-flight, id-less request flows
    # ------------------------------------------------------------------ #

    def start_current_time(self) -> _SingleFlight:
        with self._lock:
            if self._current_time is not None and not self._current_time.done.is_set():
                raise RuntimeError("a currentTime request is already in flight")
            flight = _SingleFlight()
            self._current_time = flight
            return flight

    def on_current_time(self, unix_time: int) -> None:
        with self._lock:
            flight = self._current_time
        if flight is not None:
            flight.items.append(unix_time)
            flight.done.set()

    def start_positions(self) -> _SingleFlight:
        with self._lock:
            if self._positions is not None and not self._positions.done.is_set():
                raise RuntimeError("a positions request is already in flight")
            flight = _SingleFlight()
            self._positions = flight
            return flight

    def on_position(self, account: str, contract: Any, position: float, avg_cost: float) -> None:
        with self._lock:
            flight = self._positions
        if flight is not None and not flight.done.is_set():
            flight.items.append((account, contract, position, avg_cost))

    def on_position_end(self) -> None:
        with self._lock:
            flight = self._positions
        if flight is not None:
            flight.done.set()

    def start_open_orders(self) -> _SingleFlight:
        with self._lock:
            if self._open_orders is not None and not self._open_orders.done.is_set():
                raise RuntimeError("an open-orders request is already in flight")
            flight = _SingleFlight()
            self._open_orders = flight
            return flight

    def on_open_order(self, order_id: int, contract: Any, order: Any, order_state: Any) -> None:
        with self._lock:
            flight = self._open_orders
            ack = self._order_acks.get(order_id)
        if flight is not None and not flight.done.is_set():
            flight.items.append((order_id, contract, order, order_state))
        if ack is not None and not ack.done.is_set():
            ack.items.append(("openOrder", order_id, contract, order, order_state))
            ack.done.set()

    def on_open_order_end(self) -> None:
        with self._lock:
            flight = self._open_orders
        if flight is not None:
            flight.done.set()

    def reset_for_reconnect(self) -> None:
        """Clear connection-scoped state before a fresh handshake (finding H4).

        Without this, stale events let a reconnect skip handshake verification
        and a latched ``connection_closed`` permanently marks a healthy new
        session as dead. Order-status caches are also connection-scoped: id
        sequences can restart across gateway sessions.
        """

        with self._lock:
            self.connected_event.clear()
            self.managed_accounts_event.clear()
            self.connection_closed.clear()
            self.managed_accounts = ()
            self.next_valid_id = None
            self.order_statuses.clear()
            self._order_acks.clear()

    # -- per-order acknowledgement (M7 order path) ---------------------- #

    def start_order_ack(self, order_id: int) -> _SingleFlight:
        """Await the first openOrder/orderStatus callback for one order id."""

        with self._lock:
            flight = _SingleFlight()
            self._order_acks[order_id] = flight
            return flight

    def clear_order_ack(self, order_id: int) -> None:
        with self._lock:
            self._order_acks.pop(order_id, None)

    def on_order_status(
        self,
        order_id: int,
        status: str,
        filled: float,
        remaining: float,
        avg_fill_price: float,
        perm_id: int,
    ) -> None:
        with self._lock:
            ack = self._order_acks.get(order_id)
            self.order_statuses[order_id] = (status, filled, remaining)
        if ack is not None and not ack.done.is_set():
            ack.items.append(("orderStatus", order_id, status, filled, remaining, perm_id))
            ack.done.set()

    def _fail_all_single_flights(self, reason: str) -> None:
        del reason  # waiters observe connection_closed; items stay as-is
        with self._lock:
            flights = [self._current_time, self._positions, self._open_orders]
            flights.extend(self._order_acks.values())
            for flight in flights:
                if flight is not None:
                    flight.done.set()

    # ------------------------------------------------------------------ #
    # Request-scoped streams (routed into the registry)
    # ------------------------------------------------------------------ #

    def on_account_summary(
        self, req_id: int, account: str, tag: str, value: str, currency: str
    ) -> None:
        self.registry.add(req_id, (account, tag, value, currency))

    def on_account_summary_end(self, req_id: int) -> None:
        self.registry.finish(req_id)

    def on_contract_details(self, req_id: int, details: Any) -> None:
        self.registry.add(req_id, details)

    def on_contract_details_end(self, req_id: int) -> None:
        self.registry.finish(req_id)

    def on_sec_def_opt_params(
        self,
        req_id: int,
        exchange: str,
        underlying_con_id: int,
        trading_class: str,
        multiplier: str,
        expirations: Any,
        strikes: Any,
    ) -> None:
        self.registry.add(
            req_id,
            (exchange, underlying_con_id, trading_class, multiplier, expirations, strikes),
        )

    def on_sec_def_opt_params_end(self, req_id: int) -> None:
        self.registry.finish(req_id)

    def on_exec_details(self, req_id: int, contract: Any, execution: Any) -> None:
        self.registry.add(req_id, (contract, execution))

    def on_exec_details_end(self, req_id: int) -> None:
        self.registry.finish(req_id)

    def on_commission_report(self, report: Any) -> None:
        exec_id = str(getattr(report, "execId", "") or "")
        if not exec_id:
            return
        commission = getattr(report, "commission", None)
        currency = getattr(report, "currency", None)
        amount = (
            Decimal(str(commission))
            if commission is not None
            and math.isfinite(float(commission))
            and abs(float(commission)) < _UNSET_THRESHOLD
            else None
        )
        with self._lock:
            self._commissions[exec_id] = (amount, str(currency) if currency else None)

    def commission_for(self, exec_id: str) -> tuple[Decimal | None, str | None]:
        with self._lock:
            return self._commissions.get(exec_id, (None, None))

    # ------------------------------------------------------------------ #
    # Quotes
    # ------------------------------------------------------------------ #

    def open_quote(self, req_id: int) -> None:
        with self._lock:
            self._quotes[req_id] = QuoteState()

    def quote_state(self, req_id: int) -> QuoteState | None:
        with self._lock:
            return self._quotes.get(req_id)

    def close_quote(self, req_id: int) -> QuoteState | None:
        with self._lock:
            return self._quotes.pop(req_id, None)

    def on_tick_price(self, req_id: int, tick_type: int, price: float) -> None:
        with self._lock:
            state = self._quotes.get(req_id)
        if state is None:
            return
        value = clean_price(price)
        if tick_type in _TICK_BID:
            state.bid = value
        elif tick_type in _TICK_ASK:
            state.ask = value
        elif tick_type in _TICK_LAST:
            state.last = value
        elif tick_type in _TICK_CLOSE:
            state.close = value

    def on_tick_size(self, req_id: int, tick_type: int, size: float) -> None:
        with self._lock:
            state = self._quotes.get(req_id)
        if state is None:
            return
        value = clean_size(size)
        if tick_type in _TICK_VOLUME:
            state.volume = value
        elif tick_type in (_TICK_OPTION_CALL_OI, _TICK_OPTION_PUT_OI):
            state.open_interest = value

    def on_tick_option(
        self,
        req_id: int,
        tick_type: int,
        implied_volatility: float | None,
        delta: float | None,
        gamma: float | None,
        theta: float | None,
    ) -> None:
        if tick_type not in _TICK_MODEL_OPTION:
            return
        with self._lock:
            state = self._quotes.get(req_id)
        if state is None:
            return
        state.implied_volatility = clean_ratio(implied_volatility, low=0.0, high=10.0)
        state.delta = clean_ratio(delta, low=-1.0, high=1.0)
        state.gamma = clean_ratio(gamma, low=-10.0, high=10.0)
        state.theta = clean_ratio(theta, low=-100.0, high=100.0)

    def on_market_data_type(self, req_id: int, market_data_type: int) -> None:
        with self._lock:
            state = self._quotes.get(req_id)
        if state is not None:
            state.data_quality = _MARKET_DATA_TYPE_MAP.get(market_data_type, DataQuality.UNKNOWN)

    def on_tick_snapshot_end(self, req_id: int) -> None:
        self.registry.finish(req_id)

    # ------------------------------------------------------------------ #
    # Market rules (minimum price increments), keyed by rule id
    # ------------------------------------------------------------------ #

    def expect_market_rule(self, rule_id: int) -> threading.Event:
        with self._lock:
            event = self._market_rule_events.setdefault(rule_id, threading.Event())
            return event

    def on_market_rule(self, rule_id: int, increments: Any) -> None:
        parsed: list[tuple[Decimal, Decimal]] = []
        for entry in increments or ():
            low = getattr(entry, "lowEdge", None)
            increment = getattr(entry, "increment", None)
            if low is None or increment is None:
                continue
            parsed.append((Decimal(str(low)), Decimal(str(increment))))
        with self._lock:
            self._market_rules[rule_id] = parsed
            event = self._market_rule_events.setdefault(rule_id, threading.Event())
        event.set()

    def market_rule(self, rule_id: int) -> list[tuple[Decimal, Decimal]] | None:
        with self._lock:
            rule = self._market_rules.get(rule_id)
            return list(rule) if rule is not None else None
