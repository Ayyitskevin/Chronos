"""SimBroker: a deterministic, in-memory seam-A ``chronos.broker.base.Broker`` (sim harness U2).

Test support only. Nothing in ``src/`` constructs this class, there is no runtime
broker mode for it, and it has no sockets, threads, randomness or wall clock. It
exists so the real order plane (``chronos.orders``) can be driven end to end
against a broker whose account, positions and executions change with fills, and
so one contract suite can hold it and the official adapter to the same rules.

Fidelity choices, each deliberate and each matching an existing model in the repo:

* **Pre-send refusals** mirror ``OfficialIBKRBroker`` (``broker/official_ibkr.py``):
  not connected, wrong account, an untransmitted request or a missing send guard
  raise ``BrokerRefusedBeforeSend`` and record nothing. The send itself happens
  inside ``send_guard.at_send()`` and then the optional kill switch's
  ``at_send()``, as the official adapter nests them.
* **Fill rule** is seam B's (``execution/brokers/simulated.py:143-146``), applied to
  a bar: BUY fills when ``low <= limit`` at ``min(open, limit)``; SELL fills when
  ``high >= limit`` at ``max(open, limit)``; prices are quantized to 0.01. A quote
  fills a BUY when ``ask <= limit`` at the ask, and a SELL when ``bid >= limit``
  at the bid. Fills happen only when the test advances a bar or a quote, never
  on submit, so every step is explicit.
* **Commission** for stocks is seam B's schedule, ``max(0.005 * shares, 1.00)`` USD;
  for listed options it is ``max(0.65 * contracts, 1.00)`` USD (IBKR's published rate).
* **Options** are tradable (an ``OptionContract`` request): cash and positions use the
  contract's ``multiplier``, holdings are keyed by ``con_id`` (a stock and its option
  share a symbol), and an option fills only through ``advance_option_quote`` or
  ``fill_order``, never through a stock bar or quote. There is no option reference
  data: chain, rule, deliverable and option-quote reads refuse with ``BrokerDataError``.
* **Venue rejections** (sent, then ``REJECTED``): a planned reject, a stock whose
  ``con_id`` is not the qualified one, and a BUY whose cost at the limit plus
  commission exceeds cash. No margin model: short sales are allowed.
* **Modify** is refused before send by default, because the B1 target refuses
  modify-in-place ("cancel and re-submit"). ``allow_modify=True`` enables a
  limit-price replace for tests that need it.
* **Cancel** is not kill-switch gated (risk-reducing, as in the official adapter)
  and removes the order from ``open_orders``; partial fills stay. A ``FillPlan`` can
  make it answer ``CANCEL_PENDING`` until ``confirm_cancel``. Unlike the official
  adapter today (SURVEY R-3), it refuses to cancel an order that is not Chronos's
  (no ``CHR-`` prefix), which is the contract's ownership rule.
* **Ambiguous sends** report ``SUBMISSION_UNKNOWN`` with no permanent id (no ack
  reached the caller) while the venue holds the order with one.
* **Positions and cash** derive only from this simulator's own executions; the
  average cost includes the buy commission and is per share, or per contract
  (price x multiplier) for options.
* **Data quality** defaults to ``DataQuality.DEMO``, the honest label for synthetic
  data; a test can choose another.

Crypto refuses (``BrokerDataError`` on reads, ``BrokerRefusedBeforeSend`` on orders).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from chronos.broker.base import (
    BrokerConnectionError,
    BrokerDataError,
    BrokerRefusedBeforeSend,
    BrokerSendGuard,
    OptionChainResponse,
)
from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    DisplayEnvironment,
    OrderIntent,
    OrderLifecycle,
    OrderSide,
)
from chronos.domain.models import (
    AccountSummary,
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    CancellationResult,
    ConnectionStatus,
    CryptoContract,
    Instrument,
    MarketQuote,
    OptionContract,
    OptionContractSpec,
    OptionDeliverableFacts,
    OptionMarketRule,
    OrderModification,
    OrderPreview,
    OrderRequest,
    OrderSubmission,
    UnderlyingContract,
)
from chronos.marketdata.bars import Bar, BarInterval, BarSeries, BarStatus

COMMISSION_PER_SHARE = Decimal("0.005")
#: IBKR's published per-contract rate for US listed options; the same $1 minimum applies.
OPTION_COMMISSION_PER_CONTRACT = Decimal("0.65")
MINIMUM_COMMISSION = Decimal("1.00")
_CENT = Decimal("0.01")
_STOCKS_ONLY = "SimBroker trades US stocks and listed options, not crypto"
_NO_OPTION_REFERENCE = (
    "SimBroker has no option reference data (chains, rules, deliverables, quotes)"
)
_WORKING = frozenset(
    {OrderLifecycle.SUBMITTED, OrderLifecycle.PARTIALLY_FILLED, OrderLifecycle.CANCEL_PENDING}
)


class SimClock:
    """A manual clock: time moves only when a test moves it."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("SimClock needs a timezone-aware start")
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("SimClock never moves backwards")
        self._now = self._now + delta

    def set(self, when: datetime) -> None:
        if when < self._now:
            raise ValueError("SimClock never moves backwards")
        self._now = when


@dataclass(frozen=True, slots=True)
class FillPlan:
    """What the simulated venue does with one order, keyed by ``order_ref``.

    * ``refuse_before_send``: raise ``BrokerRefusedBeforeSend``; nothing is recorded.
    * ``reject``: sent, then definitively rejected by the venue (``REJECTED``).
    * ``ambiguous``: sent and held by the venue, but no acknowledgement reached the
      caller: ``submit_order`` reports ``SUBMISSION_UNKNOWN`` while ``open_orders``
      shows the working order.
    * ``partial_fraction``: the first fill is ``max(1, floor(qty * n / d))`` shares;
      the next fill completes the order.
    * ``cancel_pending``: ``cancel_order`` answers ``CANCEL_PENDING`` (the order keeps
      working and can still fill) until the test calls ``confirm_cancel``, as the
      official adapter does before the broker confirms.
    """

    refuse_before_send: bool = False
    reject: bool = False
    ambiguous: bool = False
    partial_fraction: tuple[int, int] | None = None
    cancel_pending: bool = False

    def __post_init__(self) -> None:
        if sum((self.refuse_before_send, self.reject, self.ambiguous)) > 1:
            raise ValueError("a FillPlan takes at most one of refuse_before_send/reject/ambiguous")
        if self.partial_fraction is not None:
            numerator, denominator = self.partial_fraction
            if not 0 < numerator < denominator:
                raise ValueError("partial_fraction must satisfy 0 < n < d")


class _SendGate(Protocol):
    def at_send(self) -> AbstractContextManager[bool]: ...


@dataclass(slots=True)
class _SimOrder:
    broker_order_id: int
    permanent_id: int
    request: OrderRequest
    limit_price: Decimal
    lifecycle: OrderLifecycle
    submitted_at: datetime
    partial_fraction: tuple[int, int] | None
    cancel_pending: bool = False
    filled: Decimal = Decimal("0")

    @property
    def remaining(self) -> Decimal:
        return self.request.quantity - self.filled

    @property
    def working(self) -> bool:
        return self.lifecycle in _WORKING or self.lifecycle is OrderLifecycle.SUBMISSION_UNKNOWN


@dataclass(slots=True)
class _Holding:
    contract: Instrument
    quantity: Decimal = Decimal("0")
    #: Total cost of the open quantity in cash terms (price x quantity x multiplier),
    #: the buy commission included.
    cost: Decimal = Decimal("0")


class SimBroker:
    """Deterministic seam-A broker. See the module docstring for every fidelity rule."""

    def __init__(
        self,
        *,
        account_id: str,
        clock: Callable[[], datetime],
        symbols: Mapping[str, int],
        starting_cash: Decimal = Decimal("100000"),
        client_id: int = 17,
        plans: Mapping[str, FillPlan] | None = None,
        allow_modify: bool = False,
        kill_switch: _SendGate | None = None,
        environment: DisplayEnvironment = DisplayEnvironment.PAPER,
        quote_quality: DataQuality = DataQuality.DEMO,
        session_hours: Mapping[str, tuple[str, str]] | None = None,
        first_order_id: int = 1001,
    ) -> None:
        if not account_id.strip():
            raise ValueError("SimBroker needs an account id")
        self._account_id = account_id.strip()
        self._clock = clock
        self._con_ids = {symbol.strip().upper(): con_id for symbol, con_id in symbols.items()}
        self._session_hours = {k.strip().upper(): v for k, v in (session_hours or {}).items()}
        self._cash = Decimal(starting_cash)
        self._client_id = client_id
        self._plans = dict(plans or {})
        self._allow_modify = allow_modify
        self._kill_switch = kill_switch
        self._environment = environment
        self._quote_quality = quote_quality
        self._next_order_id = first_order_id
        self._connected = False
        self._last_sync: datetime | None = None
        self._orders: dict[int, _SimOrder] = {}
        self._executions: list[BrokerExecution] = []
        self._holdings: dict[int, _Holding] = {}  # by contract con_id
        self._marks: dict[int, Decimal] = {}  # per-unit price, by contract con_id
        self._quotes: dict[str, MarketQuote] = {}
        self._history: dict[str, list[Bar]] = {}
        self._cancelled_market_data: list[int] = []
        self._sent = 0

    # --- test controls ------------------------------------------------------------------------

    @property
    def sent_order_count(self) -> int:
        """Orders that crossed the simulated wire (refusals before send are not counted)."""

        return self._sent

    def order_lifecycle(self, broker_order_id: int) -> OrderLifecycle:
        return self._orders[broker_order_id].lifecycle

    def set_quote(self, symbol: str, *, bid: str, ask: str, last: str | None = None) -> None:
        """Script the current quote without matching any order."""

        contract = self._contract(symbol)
        self._quotes[contract.symbol] = MarketQuote(
            contract=contract,
            timestamp=self._clock(),
            data_quality=self._quote_quality,
            bid=Decimal(bid),
            ask=Decimal(ask),
            last=Decimal(last) if last is not None else None,
        )
        self._marks[contract.con_id] = (
            Decimal(last) if last is not None else (Decimal(bid) + Decimal(ask)) / 2
        )

    def advance_quote(self, symbol: str, *, bid: str, ask: str, last: str | None = None) -> None:
        """Set the quote, then fill working orders the quote crosses (at the quote)."""

        self.set_quote(symbol, bid=bid, ask=ask, last=last)
        quote = self._quotes[self._contract(symbol).symbol]
        assert quote.bid is not None and quote.ask is not None
        for order in self._working_for(quote.contract.symbol):
            if order.request.side is OrderSide.BUY and quote.ask <= order.limit_price:
                self._fill(order, price=quote.ask, at=quote.timestamp)
            elif order.request.side is OrderSide.SELL and quote.bid >= order.limit_price:
                self._fill(order, price=quote.bid, at=quote.timestamp)

    def advance_bar(self, bar: Bar) -> None:
        """Fill working orders this bar crosses (seam B's limit-cross rule)."""

        symbol = bar.symbol.upper()
        if symbol in self._con_ids:
            self._marks[self._con_ids[symbol]] = Decimal(str(bar.close))
        bar_open, high, low = Decimal(str(bar.open)), Decimal(str(bar.high)), Decimal(str(bar.low))
        for order in self._working_for(symbol):
            if bar.timestamp_utc < order.submitted_at:
                continue  # a bar from before the order existed cannot fill it
            if order.request.side is OrderSide.BUY and low <= order.limit_price:
                self._fill(order, price=min(bar_open, order.limit_price), at=bar.timestamp_utc)
            elif order.request.side is OrderSide.SELL and high >= order.limit_price:
                self._fill(order, price=max(bar_open, order.limit_price), at=bar.timestamp_utc)

    def advance_option_quote(self, contract: OptionContract, *, bid: str, ask: str) -> None:
        """Mark an option at its mid, then fill working orders on that exact contract.

        BUY fills when ``ask <= limit`` at the ask; SELL when ``bid >= limit`` at the bid.
        """

        bid_price, ask_price = Decimal(bid), Decimal(ask)
        self._marks[contract.con_id] = (bid_price + ask_price) / 2
        now = self._clock()
        for order in list(self._orders.values()):
            if not order.working or order.request.contract.con_id != contract.con_id:
                continue
            if order.request.side is OrderSide.BUY and ask_price <= order.limit_price:
                self._fill(order, price=ask_price, at=now)
            elif order.request.side is OrderSide.SELL and bid_price >= order.limit_price:
                self._fill(order, price=bid_price, at=now)

    def confirm_cancel(self, broker_order_id: int) -> None:
        """The venue confirms a ``CANCEL_PENDING`` cancel: the order becomes ``CANCELLED``."""

        order = self._orders[broker_order_id]
        if order.lifecycle is not OrderLifecycle.CANCEL_PENDING:
            raise ValueError(f"order {broker_order_id} has no pending cancel")
        order.lifecycle = OrderLifecycle.CANCELLED

    def fill_order(
        self, broker_order_id: int, quantity: Decimal, price: Decimal | None = None
    ) -> None:
        """Fill exactly ``quantity`` of a working order at ``price`` (default: its limit), now."""

        order = self._orders[broker_order_id]
        if not order.working:
            raise ValueError(f"order {broker_order_id} is not working")
        if not 0 < quantity <= order.remaining:
            raise ValueError("fill quantity must be positive and at most the remaining quantity")
        self._fill_quantity(
            order, quantity=quantity, price=price or order.limit_price, at=self._clock()
        )

    def add_history(self, bar: Bar) -> None:
        """Script a closed bar for ``historical_bars``."""

        self._history.setdefault(bar.symbol.upper(), []).append(bar)

    # --- seam A: session ----------------------------------------------------------------------

    async def connect(self) -> None:
        self._connected = True
        self._last_sync = self._clock()

    async def disconnect(self) -> None:
        self._connected = False

    async def connection_status(self) -> ConnectionStatus:
        return ConnectionStatus(
            state=ConnectionState.CONNECTED if self._connected else ConnectionState.DISCONNECTED,
            environment=self._environment,
            connected=self._connected,
            account_id=self._account_id,
            data_quality=self._quote_quality,
            last_successful_sync=self._last_sync,
            message="simulated broker",
            managed_accounts=(self._account_id,) if self._connected else (),
        )

    async def server_time(self) -> datetime:
        # The real adapters need a live session for this too (official_ibkr.py:1064-1065).
        self._require_connected()
        return self._clock()

    # --- seam A: account and order state ------------------------------------------------------

    async def account_summary(self) -> AccountSummary:
        self._require_connected()
        market_value = sum(
            (
                holding.quantity * self._mark(holding) * _multiplier(holding.contract)
                for holding in self._holdings.values()
            ),
            Decimal("0"),
        )
        net = self._cash + market_value
        return AccountSummary(
            account_id=self._account_id,
            net_liquidation=max(net, Decimal("0")),
            total_cash=self._cash,
            buying_power=max(self._cash, Decimal("0")),
            as_of=self._clock(),
        )

    async def positions(self) -> tuple[BrokerPosition, ...]:
        self._require_connected()
        result = []
        for holding in self._holdings.values():
            # Average cost per share, or per contract for an option (price x multiplier),
            # as IBKR reports it; market_price stays the per-unit quote price.
            average = holding.cost / abs(holding.quantity)
            mark = self._mark(holding)
            result.append(
                BrokerPosition(
                    account_id=self._account_id,
                    contract=holding.contract,
                    quantity=holding.quantity,
                    average_cost=average,
                    market_price=mark,
                    unrealized_pnl=(mark * _multiplier(holding.contract) - average)
                    * holding.quantity,
                )
            )
        return tuple(result)

    async def executions(self, since: datetime | None = None) -> tuple[BrokerExecution, ...]:
        self._require_connected()
        return tuple(e for e in self._executions if since is None or e.timestamp >= since)

    async def open_orders(self) -> tuple[BrokerOrder, ...]:
        self._require_connected()
        return tuple(self._broker_order(order) for order in self._orders.values() if order.working)

    # --- seam A: contracts and market data ----------------------------------------------------

    async def qualify_underlying(self, symbol: str) -> UnderlyingContract:
        self._require_connected()
        return self._contract(symbol)

    async def qualify_crypto(self, symbol: str) -> CryptoContract:
        del symbol
        raise BrokerDataError(_STOCKS_ONLY)

    async def option_chain_parameters(self, underlying: UnderlyingContract) -> OptionChainResponse:
        del underlying
        raise BrokerDataError(_NO_OPTION_REFERENCE)

    async def qualify_option_contracts(
        self, contracts: Sequence[OptionContractSpec]
    ) -> tuple[OptionContract, ...]:
        if contracts:
            raise BrokerDataError(_NO_OPTION_REFERENCE)
        return ()

    async def option_market_rules(
        self, contracts: Sequence[OptionContract]
    ) -> tuple[OptionMarketRule, ...]:
        if contracts:
            raise BrokerDataError(_NO_OPTION_REFERENCE)
        return ()

    async def option_deliverable_facts(
        self, contracts: Sequence[OptionContract]
    ) -> tuple[OptionDeliverableFacts, ...]:
        if contracts:
            raise BrokerDataError(_NO_OPTION_REFERENCE)
        return ()

    async def request_underlying_quote(self, contract: UnderlyingContract) -> MarketQuote:
        self._require_connected()
        try:
            return self._quotes[contract.symbol]
        except KeyError as error:
            raise BrokerDataError(f"no scripted quote for {contract.symbol}") from error

    async def request_crypto_quote(self, contract: CryptoContract) -> MarketQuote:
        del contract
        raise BrokerDataError(_STOCKS_ONLY)

    async def request_option_quotes(
        self, contracts: Sequence[OptionContract]
    ) -> tuple[MarketQuote, ...]:
        if contracts:
            raise BrokerDataError(_NO_OPTION_REFERENCE)
        return ()

    async def cancel_market_data(self, contract_ids: Sequence[int]) -> None:
        self._cancelled_market_data.extend(contract_ids)

    async def historical_bars(
        self,
        contract: UnderlyingContract,
        *,
        interval: BarInterval = BarInterval.DAY_1,
        lookback_days: int = 180,
    ) -> BarSeries:
        self._require_connected()
        now = self._clock()
        earliest = now - timedelta(days=lookback_days)
        bars = sorted(
            (
                bar
                for bar in self._history.get(contract.symbol, [])
                if bar.interval is interval
                and bar.status is BarStatus.CLOSED
                and earliest <= bar.timestamp_utc <= now
            ),
            key=lambda bar: bar.timestamp_utc,
        )
        return BarSeries(symbol=contract.symbol, interval=interval, bars=tuple(bars))

    # --- seam A: the order plane --------------------------------------------------------------

    async def preview_order(self, request: OrderRequest) -> OrderPreview:
        self._reverify(request.account_id)
        if request.transmit:
            raise BrokerRefusedBeforeSend("previews must never carry transmit=True; refusing")
        self._require_tradable(request)
        return OrderPreview(
            request=request,
            accepted=True,
            estimated_commission=_commission(request.contract, request.quantity),
            broker_message="simulated what-if",
            previewed_at=self._clock(),
        )

    async def submit_order(
        self,
        request: OrderRequest,
        *,
        send_guard: BrokerSendGuard | None = None,
    ) -> OrderSubmission:
        self._reverify(request.account_id)
        if not request.transmit:
            raise BrokerRefusedBeforeSend(
                "submit_order requires an explicitly transmit-authorized request; "
                "refusing before send"
            )
        if send_guard is None:
            raise BrokerRefusedBeforeSend(
                "submission is missing atomic reconciliation authorization; refusing before send"
            )
        self._require_tradable(request)
        plan = self._plans.get(request.order_ref, FillPlan())
        if plan.refuse_before_send:
            raise BrokerRefusedBeforeSend("simulated refusal before send (FillPlan)")
        with send_guard.at_send() as authorized:
            if not authorized:
                raise BrokerRefusedBeforeSend(
                    "reconciliation authorization changed; refusing before send"
                )
            kill_guard = (
                self._kill_switch.at_send() if self._kill_switch is not None else nullcontext(True)
            )
            with kill_guard as kill_authorized:
                if not kill_authorized:
                    raise BrokerRefusedBeforeSend("live kill switch engaged; refusing before send")
                order = self._record(request, plan)
        venue_refusal = self._venue_refusal(request)
        if plan.reject or venue_refusal:
            order.lifecycle = OrderLifecycle.REJECTED
            message = venue_refusal or "simulated venue rejection"
        elif plan.ambiguous:
            order.lifecycle = OrderLifecycle.SUBMISSION_UNKNOWN
            message = "no broker acknowledgement; state unknown"
        else:
            message = ""
        return OrderSubmission(
            correlation_id=request.correlation_id,
            broker_order_id=order.broker_order_id,
            # No acknowledgement means the caller never learnt the permanent id.
            permanent_id=None if plan.ambiguous and not venue_refusal else order.permanent_id,
            client_id=self._client_id,
            lifecycle=order.lifecycle,
            submitted_at=order.submitted_at,
            message=message,
        )

    async def modify_order(self, request: OrderModification) -> OrderSubmission:
        self._reverify(self._account_id)
        if not self._allow_modify:
            raise BrokerRefusedBeforeSend(
                "modify-in-place is not supported (as in the official adapter); "
                "cancel and re-submit instead (nothing was sent)"
            )
        order = self._orders.get(request.broker_order_id)
        if order is None or not order.working:
            raise BrokerRefusedBeforeSend("no working order with that id; nothing was sent")
        order.limit_price = request.new_limit_price
        return OrderSubmission(
            correlation_id=request.correlation_id,
            broker_order_id=order.broker_order_id,
            permanent_id=order.permanent_id,
            client_id=self._client_id,
            lifecycle=self._venue_lifecycle(order),
            submitted_at=self._clock(),
            message="simulated modify",
        )

    async def cancel_order(self, broker_order_id: int) -> CancellationResult:
        # Not kill-switch gated: cancellation is risk-reducing (as in the official adapter).
        if not self._connected:
            raise BrokerRefusedBeforeSend("not connected; no cancel was sent")
        now = self._clock()
        order = self._orders.get(broker_order_id)
        if order is None:
            return CancellationResult(
                broker_order_id=broker_order_id,
                requested=False,
                lifecycle=OrderLifecycle.REJECTED,
                message="no such order",
                timestamp=now,
            )
        if not order.request.order_ref.startswith("CHR-"):
            # The contract's ownership rule: Chronos never cancels an order it does not
            # own. (The official adapter lacks this check today: SURVEY R-3, RED.)
            return CancellationResult(
                broker_order_id=broker_order_id,
                requested=False,
                lifecycle=self._venue_lifecycle(order),
                message="not a Chronos order; refusing to cancel it",
                timestamp=now,
            )
        if not order.working or order.lifecycle is OrderLifecycle.CANCEL_PENDING:
            return CancellationResult(
                broker_order_id=broker_order_id,
                requested=False,
                lifecycle=order.lifecycle,
                message="order is not working, or its cancel is already pending",
                timestamp=now,
            )
        if order.cancel_pending:
            order.lifecycle = OrderLifecycle.CANCEL_PENDING
            return CancellationResult(
                broker_order_id=broker_order_id,
                requested=True,
                lifecycle=OrderLifecycle.CANCEL_PENDING,
                message="cancel requested; awaiting broker confirmation",
                timestamp=now,
            )
        order.lifecycle = OrderLifecycle.CANCELLED
        return CancellationResult(
            broker_order_id=broker_order_id,
            requested=True,
            lifecycle=OrderLifecycle.CANCELLED,
            message="simulated cancel",
            timestamp=now,
        )

    # --- internals ----------------------------------------------------------------------------

    def _require_connected(self) -> None:
        if not self._connected:
            raise BrokerConnectionError("simulated broker is not connected")

    def _reverify(self, account_id: str) -> None:
        if not self._connected:
            raise BrokerRefusedBeforeSend("not connected; no order call was sent")
        if account_id != self._account_id:
            raise BrokerRefusedBeforeSend(
                "order account does not match the configured account; refusing before send"
            )

    @staticmethod
    def _require_tradable(request: OrderRequest) -> None:
        if not isinstance(request.contract, UnderlyingContract | OptionContract):
            raise BrokerRefusedBeforeSend(f"{_STOCKS_ONLY}; refusing before send")

    def _venue_refusal(self, request: OrderRequest) -> str:
        """Why the simulated venue rejects a sent order ("" when it accepts it)."""

        contract = request.contract
        if isinstance(contract, UnderlyingContract) and contract.con_id != self._con_ids.get(
            contract.symbol
        ):
            return "simulated venue: no security definition for that contract id"
        if request.side is OrderSide.BUY:
            cost = request.limit_price * request.quantity * _multiplier(contract)
            if cost + _commission(contract, request.quantity) > self._cash:
                return "simulated venue: insufficient funds for the order"
        return ""

    def _contract(self, symbol: str) -> UnderlyingContract:
        key = symbol.strip().upper()
        try:
            con_id = self._con_ids[key]
        except KeyError as error:
            raise BrokerDataError(f"unknown symbol {key!r} in the simulator") from error
        hours, zone = self._session_hours.get(key, ("", ""))
        return UnderlyingContract(con_id=con_id, symbol=key, liquid_hours=hours, time_zone_id=zone)

    def _record(self, request: OrderRequest, plan: FillPlan) -> _SimOrder:
        order_id = self._next_order_id
        self._next_order_id += 1
        self._sent += 1
        order = _SimOrder(
            broker_order_id=order_id,
            permanent_id=900_000_000 + order_id,
            request=request,
            limit_price=request.limit_price,
            lifecycle=OrderLifecycle.SUBMITTED,
            submitted_at=self._clock(),
            partial_fraction=plan.partial_fraction,
            cancel_pending=plan.cancel_pending,
        )
        self._orders[order_id] = order
        return order

    def _working_for(self, symbol: str) -> list[_SimOrder]:
        """Working STOCK orders on ``symbol`` (bars and stock quotes never fill options)."""

        return [
            o
            for o in self._orders.values()
            if o.working
            and isinstance(o.request.contract, UnderlyingContract)
            and o.request.contract.symbol == symbol
        ]

    def _fill(self, order: _SimOrder, *, price: Decimal, at: datetime) -> None:
        quantity = order.remaining
        if order.partial_fraction is not None and order.filled == 0:
            numerator, denominator = order.partial_fraction
            quantity = min(
                max(Decimal("1"), (order.request.quantity * numerator) // denominator), quantity
            )
        self._fill_quantity(order, quantity=quantity, price=price, at=at)

    def _fill_quantity(
        self, order: _SimOrder, *, quantity: Decimal, price: Decimal, at: datetime
    ) -> None:
        price = price.quantize(_CENT)
        commission = _commission(order.request.contract, quantity)
        self._executions.append(
            BrokerExecution(
                execution_id=f"SIM-{len(self._executions) + 1:06d}",
                account_id=self._account_id,
                broker_order_id=order.broker_order_id,
                permanent_id=order.permanent_id,
                client_id=self._client_id,
                order_ref=order.request.order_ref,
                contract=order.request.contract,
                side=order.request.side,
                quantity=quantity,
                price=price,
                timestamp=at,
                commission=commission,
                commission_currency="USD",
            )
        )
        order.filled += quantity
        if order.lifecycle is not OrderLifecycle.CANCEL_PENDING or order.remaining == 0:
            order.lifecycle = (
                OrderLifecycle.FILLED if order.remaining == 0 else OrderLifecycle.PARTIALLY_FILLED
            )
        self._book(order.request.contract, order.request.side, quantity, price, commission)

    def _book(
        self,
        contract: Instrument,
        side: OrderSide,
        quantity: Decimal,
        price: Decimal,
        commission: Decimal,
    ) -> None:
        signed = quantity if side is OrderSide.BUY else -quantity
        notional = price * quantity * _multiplier(contract)
        if side is OrderSide.BUY:
            self._cash -= notional + commission
        else:
            self._cash += notional - commission
        holding = self._holdings.setdefault(contract.con_id, _Holding(contract=contract))
        if holding.quantity == 0 or (holding.quantity > 0) == (signed > 0):
            # Opening or adding: the commission joins the basis on the side that pays it.
            holding.cost += notional + (commission if side is OrderSide.BUY else Decimal("0"))
            holding.quantity += signed
        else:
            closing = min(abs(signed), abs(holding.quantity))
            average = holding.cost / abs(holding.quantity)
            holding.cost -= average * closing
            holding.quantity += signed
            opened = abs(signed) - closing
            if opened > 0:  # crossed through zero: the remainder opens at this price
                holding.cost = price * opened * _multiplier(contract)
        if holding.quantity == 0:
            del self._holdings[contract.con_id]

    def _mark(self, holding: _Holding) -> Decimal:
        """Per-unit mark: the last bar close or quote, else the average cost per unit."""

        fallback = holding.cost / abs(holding.quantity) / _multiplier(holding.contract)
        return self._marks.get(holding.contract.con_id, fallback)

    @staticmethod
    def _venue_lifecycle(order: _SimOrder) -> OrderLifecycle:
        # The venue holds an order the caller only knows as SUBMISSION_UNKNOWN.
        if order.lifecycle is OrderLifecycle.SUBMISSION_UNKNOWN:
            return (
                OrderLifecycle.SUBMITTED if order.filled == 0 else OrderLifecycle.PARTIALLY_FILLED
            )
        return order.lifecycle

    def _broker_order(self, order: _SimOrder) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id=order.broker_order_id,
            permanent_id=order.permanent_id,
            client_id=self._client_id,
            account_id=self._account_id,
            order_ref=order.request.order_ref,
            contract=order.request.contract,
            side=order.request.side,
            quantity=order.request.quantity,
            filled_quantity=order.filled,
            remaining_quantity=order.remaining,
            limit_price=order.limit_price,
            lifecycle=self._venue_lifecycle(order),
            transmit=True,
            outside_rth=order.request.outside_rth,
        )


def _multiplier(contract: Instrument) -> Decimal:
    return contract.multiplier if isinstance(contract, OptionContract) else Decimal("1")


def _commission(contract: Instrument, quantity: Decimal) -> Decimal:
    rate = (
        OPTION_COMMISSION_PER_CONTRACT
        if isinstance(contract, OptionContract)
        else COMMISSION_PER_SHARE
    )
    return max(quantity * rate, MINIMUM_COMMISSION).quantize(_CENT)


# --- U1 contract-suite hook (tests/contract/conftest.py) --------------------------------------

_CONTRACT_ACCOUNT = "DU5550001"
_CONTRACT_T0 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)


class _SeedGuard:
    @contextmanager
    def at_send(self) -> Iterator[bool]:
        yield True


def _seeded_contract_broker() -> SimBroker:
    """A fresh, seeded, UNCONNECTED SimBroker, built through its own submit path.

    Seed: one Chronos-owned (``CHR-``) stock order partially filled (5 of 10, so a
    working order and one execution exist), and one working order without the
    ``CHR-`` prefix (the contract's "foreign" order, which ``cancel_order`` refuses).
    """

    clock = SimClock(_CONTRACT_T0)
    broker = SimBroker(
        account_id=_CONTRACT_ACCOUNT,
        clock=clock,
        symbols={"AAPL": 999, "MSFT": 272093},
        starting_cash=Decimal("100000"),
        plans={"CHR-CONTRACT-SEED": FillPlan(partial_fraction=(1, 2))},
    )

    async def seed() -> None:
        await broker.connect()
        for ref, symbol, quantity, limit in (
            ("CHR-CONTRACT-SEED", "AAPL", "10", "100.00"),
            ("MANUAL-1", "MSFT", "1", "50.00"),
        ):
            contract = await broker.qualify_underlying(symbol)
            await broker.submit_order(
                OrderRequest(
                    correlation_id=f"seed-{ref}",
                    account_id=_CONTRACT_ACCOUNT,
                    contract=contract,
                    intent=OrderIntent.OPEN_LONG_STOCK,
                    side=OrderSide.BUY,
                    quantity=Decimal(quantity),
                    limit_price=Decimal(limit),
                    order_ref=ref,
                    transmit=True,
                ),
                send_guard=_SeedGuard(),
            )
        clock.advance(timedelta(minutes=1))
        broker.advance_quote("AAPL", bid="99.80", ask="99.90")  # crosses the owned BUY only
        await broker.disconnect()

    _run_without_a_loop(seed())
    return broker


def _run_without_a_loop(coroutine: Coroutine[Any, Any, None]) -> None:
    """Drive a coroutine that never suspends (every SimBroker call), with no event loop.

    ``build()`` is called from inside running async tests, where ``asyncio.run`` is
    not allowed. SimBroker's coroutines contain no real awaits, so one ``send``
    completes them; anything that does suspend is a bug and fails loudly.
    """

    try:
        coroutine.send(None)
    except StopIteration:
        return
    coroutine.close()
    raise RuntimeError("SimBroker seeding suspended; it must never await real I/O")


async def _contract_fill(broker: object, broker_order_id: int, quantity: Decimal) -> None:
    assert isinstance(broker, SimBroker)
    broker.fill_order(broker_order_id, quantity)


def contract_harness() -> Any:
    """The ``sim`` harness for the U1 contract suite (imported lazily, as the hook requires)."""

    from tests.contract.conftest import BrokerHarness

    return BrokerHarness(
        name="sim",
        build=_seeded_contract_broker,
        account_id=_CONTRACT_ACCOUNT,
        owned_order_id=1001,
        accepts_orders=True,
        implements_full_protocol=True,
        foreign_order_id=1002,
        cause_fill=_contract_fill,
    )
