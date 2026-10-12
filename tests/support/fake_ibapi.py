"""A stand-in for the ``ibapi`` package and an in-process TWS, for TESTS ONLY (S1b).

It lets the REAL, unmodified ``chronos.broker.official_ibkr.OfficialIBKRBroker`` (its ``_App``,
its ``CallbackBridge``, its normalizers) run a whole session with no gateway, no socket and no
real ``ibapi``. Nothing in ``src/`` imports this module; it is reached only through
:func:`install`, which patches the adapter's two loader functions for one test with
``monkeypatch`` (never ``sys.modules``, so ``ibapi`` stays absent) and arms a socket tripwire.

Model (S1a DESIGN §2, with the reader's review folded in):

* :class:`FakeGateway` is one TWS. Its venue state is a ``SimBroker`` (the reviewed r2 fill,
  cash, position, commission and cancel models), seeded with the same facts as the ``sim``
  contract harness, so the two harnesses are judged on one book.
* :class:`FakeEClient` is the client half of ibapi. Requests run on the caller's thread and
  enqueue callbacks; ``run()`` (the adapter's reader thread) dispatches them, FIFO, one at a
  time — the same thread shape as real ibapi, and a deterministic order.
* :meth:`FakeEClient.wait_dispatched` is ``queue.join()`` with ``task_done()`` called only
  AFTER each callback returns: "the last callback was dispatched", not "the queue is empty"
  (reader P2-1a).
* Ownership is IB's rule, by CLIENT ID: a client may cancel only its own client's orders; any
  other id gets ``10147``. Manual TWS orders carry client id 0.
* Adapter order ids and SimBroker ids are mapped both ways (previews consume adapter ids and
  create nothing at the venue).

ASSUMED IB SEMANTICS — every one of these is the author's reading of the TWS API, to verify
against real captures at K4: callback order (``openOrder`` before ``orderStatus``; a live fill
as ``orderStatus``, ``execDetails(reqId=-1)``, ``commissionReport``); the error codes 201
(reject), 10147 (cancel of an unknown or foreign id), 10148 (cannot be cancelled), 200 (no
security definition); ``PreSubmitted`` for a what-if; the unset-double sentinel for what-if
margin fields; execution ``time`` as a tz-less ``YYYYMMDD HH:MM:SS`` in UTC; ``ExecutionFilter``
``time`` as an inclusive lower bound at second resolution.

TIME CONVENTION (explicit; fable r1 P2-1). The fake renders ``Execution.time`` tz-less in
:data:`EXECUTION_TIME_ZONE` (UTC) with :data:`EXECUTION_TIME_FORMAT`, and reads the adapter's
``ExecutionFilter.time`` with :data:`FILTER_TIME_FORMAT` in the same zone, because those are
what the adapter parses and writes (``official_ibkr.py:808-832`` reads a tz-less time as UTC;
``:1120-1121`` writes the bound in UTC). TWS is believed to render execution times in the
LOGIN's time zone (newer builds append the zone name). So any pass that depends on execution
instants (the ``since`` cases) proves the adapter against this convention only: candidate
finding S1-F3, a K4 check. ``FakeGateway.execution_time_zone`` lets a test render another zone.

RECORDED RISK R-ARITY (fable r1 P2-2; Daybreak P3-2). The fake's three ``error`` shapes are
the ADAPTER's reading of ibapi (``official_ibkr.py:497-504``: an optional NON-int first value
is dropped as a timestamp, then ``(reqId, errorCode, errorString[, advancedOrderRejectJson])``).
No ibapi source, wheel or version pin exists on this machine or in the repo (``pyproject.toml``
names ``ibapi.*`` only in a mypy override), so the shape could not be cited. Current ibapi is
believed (unverified) to call ``error(reqId, errorTime: int, errorCode, errorString,
advancedOrderRejectJson)``; under that shape the adapter would read the TIME as the code, and
connection-loss and reject codes would never match (fail-open). Every error-flight pass here
holds under the fake's shapes only. First K4 check, and a B1 prerequisite: read the installed
ibapi's ``EWrapper.error`` signature and capture one error from the paper gateway.
"""

from __future__ import annotations

import queue
import socket
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from chronos.config.settings import Settings
from chronos.domain.enums import (
    BrokerAdapter,
    BrokerMode,
    IBEnvironment,
    OptionRight,
    OrderIntent,
    OrderLifecycle,
    OrderSide,
)
from chronos.domain.models import (
    BrokerExecution,
    BrokerOrder,
    Instrument,
    OptionContract,
    OrderRequest,
)
from tests.support.order_fakes import option_contract
from tests.support.sim_broker import (
    _CONTRACT_ACCOUNT,
    SimBroker,
    _run_without_a_loop,
    _seeded_contract_broker,
    _SeedGuard,
)

ACCOUNT = _CONTRACT_ACCOUNT
CLIENT_ID = 17
MANUAL_CLIENT_ID = 0
OWNED_ID = 1001  # the seed's Chronos order (CHR-CONTRACT-SEED), client 17, 5 of 10 filled
FOREIGN_ID = 1002  # the seed's manual order (MANUAL-1), client 0, working
READER_THREAD = "chronos-ibapi-reader"  # the name the adapter gives its reader thread

UNSET_DOUBLE = 1.7976931348623157e308
CODE_NO_SECURITY = 200
CODE_ORDER_REJECTED = 201
CODE_CANCEL_NOT_FOUND = 10147
CODE_CANNOT_CANCEL = 10148
#: ASSUMED-IB (verify at K4): sent before the handshake when the client id is still held.
CODE_CLIENT_ID_IN_USE = 326
#: ASSUMED-IB (verify at K4): the answer to placeOrder (whatIf included) under Read-Only API.
CODE_READ_ONLY = 321

#: The fake's time convention (see the module docstring): what the adapter parses and writes.
EXECUTION_TIME_ZONE = UTC
EXECUTION_TIME_FORMAT = "%Y%m%d %H:%M:%S"
FILTER_TIME_FORMAT = "%Y%m%d-%H:%M:%S"

_STOP = object()


# --- the stub ibapi objects (only the attributes the adapter reads or writes) ------------------


class FakeContract:
    def __init__(self) -> None:
        self.conId = 0
        self.symbol = ""
        self.secType = ""
        self.exchange = ""
        self.primaryExchange = ""
        self.currency = "USD"
        self.lastTradeDateOrContractMonth = ""
        self.right = ""
        self.strike = 0.0
        self.multiplier = ""
        self.localSymbol = ""
        self.tradingClass = ""


class FakeOrder:
    def __init__(self) -> None:
        self.action = ""
        self.orderType = ""
        self.totalQuantity: Any = Decimal("0")
        self.lmtPrice = 0.0
        self.tif = ""
        self.outsideRth = False
        self.account = ""
        self.orderRef = ""
        self.whatIf = False
        self.transmit = False
        self.permId = 0
        self.clientId = 0
        # completed orders only (ASSUMED IB: ``Order.filledQuantity``; ``orderId`` may be absent)
        self.orderId = 0
        self.filledQuantity: Any = Decimal("0")


class FakeOrderState:
    def __init__(self, status: str, *, commission: float = UNSET_DOUBLE) -> None:
        self.status = status
        self.commission = commission
        self.initMarginChange = UNSET_DOUBLE
        self.maintMarginChange = UNSET_DOUBLE
        self.equityWithLoanChange = UNSET_DOUBLE
        self.warningText = ""


class FakeExecution:
    def __init__(self) -> None:
        self.execId = ""
        self.acctNumber = ""
        self.orderId = 0
        self.permId = 0
        self.clientId = 0
        self.orderRef = ""
        self.side = ""
        self.shares = 0.0
        self.price = 0.0
        self.time = ""


class FakeExecutionFilter:
    def __init__(self) -> None:
        self.acctCode = ""
        self.time = ""


class FakeCommissionReport:
    def __init__(self, exec_id: str, commission: float, currency: str) -> None:
        self.execId = exec_id
        self.commission = commission
        self.currency = currency


class FakeEWrapper:
    """The ``EWrapper`` base: the adapter's ``_App`` overrides every callback it consumes."""


class RecordingWrapper(FakeEWrapper):
    """A wrapper that records every dispatched callback (for client-level tests)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Callable[..., None]:
        def record(*args: Any) -> None:
            self.calls.append((name, args))

        return record


# --- the client: requests on the caller's thread, callbacks on the reader thread --------------


class FakeEClient:
    gateway: FakeGateway  # set by bound_to()

    @classmethod
    def bound_to(cls, gateway: FakeGateway) -> type[FakeEClient]:
        return type("BoundFakeEClient", (cls,), {"gateway": gateway})

    def __init__(self, wrapper: Any) -> None:
        self.wrapper = wrapper
        self.client_id: int | None = None
        self._queue: queue.Queue[Any] = queue.Queue()
        self._connected = False

    # session -------------------------------------------------------------------------------
    def connect(self, host: str, port: int, clientId: int) -> None:
        del host, port
        self.client_id = int(clientId)
        self.gateway.record(self, "connect")
        self._connected = True
        self.gateway.attach(self)

    def run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    if self.gateway.stuck_reader:
                        self.gateway.readers_released.wait()  # U-a: a reader that won't stop
                    return
                name, args = item
                getattr(self.wrapper, name)(*args)
            finally:
                self._queue.task_done()  # only AFTER the callback returned (reader P2-1a)

    def isConnected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
        self.gateway.record(self, "disconnect")
        if not self._connected:
            return
        self._connected = False
        self.gateway.detach(self)
        self.emit("connectionClosed")
        self._queue.put(_STOP)

    def emit(self, name: str, *args: Any) -> None:
        self._queue.put((name, args))

    def wait_dispatched(self) -> None:
        """Block until every callback queued so far has been DISPATCHED (not merely dequeued)."""

        self._queue.join()

    # requests -------------------------------------------------------------------------------
    def reqMarketDataType(self, marketDataType: int) -> None:
        self.gateway.record(self, "reqMarketDataType")
        del marketDataType

    def reqCurrentTime(self) -> None:
        self.gateway.record(self, "reqCurrentTime")
        self.gateway.current_time(self)

    def reqAccountSummary(self, reqId: int, group: str, tags: str) -> None:
        self.gateway.record(self, "reqAccountSummary")
        del group, tags
        self.gateway.account_summary(self, reqId)

    def cancelAccountSummary(self, reqId: int) -> None:
        self.gateway.record(self, "cancelAccountSummary")
        del reqId

    def reqPositions(self) -> None:
        self.gateway.record(self, "reqPositions")
        self.gateway.positions(self)

    def cancelPositions(self) -> None:
        self.gateway.record(self, "cancelPositions")
        pass

    def reqExecutions(self, reqId: int, execFilter: FakeExecutionFilter) -> None:
        self.gateway.record(self, "reqExecutions")
        self.gateway.executions(self, reqId, execFilter)

    def reqAllOpenOrders(self) -> None:
        self.gateway.record(self, "reqAllOpenOrders")
        self.gateway.open_orders(self)

    def reqCompletedOrders(self, apiOnly: bool) -> None:
        self.gateway.completed_orders(self, bool(apiOnly))

    def placeOrder(self, orderId: int, contract: FakeContract, order: FakeOrder) -> None:
        self.gateway.record(self, "placeOrder")
        self.gateway.place_order(self, int(orderId), contract, order)

    def cancelOrder(self, orderId: int, manualCancelOrderTime: str = "") -> None:
        self.gateway.record(self, "cancelOrder")
        del manualCancelOrderTime
        self.gateway.cancel_order(self, int(orderId))


# --- the gateway: one TWS over one SimBroker ---------------------------------------------------


class FakeGateway:
    def __init__(self, sim: SimBroker, *, client_of: dict[int, int]) -> None:
        self.sim = sim
        self._lock = threading.RLock()
        self._client_of = dict(client_of)  # sim order id -> placing client id
        self._adapter_of = {sim_id: sim_id for sim_id in client_of}  # sim id -> adapter id
        self._sim_of = {sim_id: sim_id for sim_id in client_of}  # adapter id -> sim id
        self._clients: list[FakeEClient] = []
        self._next_valid_id = max(client_of, default=0) + 1
        self._registry: dict[int, Instrument] = {}
        self.error_style = "plain"  # "plain" (3 args) | "json" (4) | "timestamped" (ts first)
        self.errors_sent: list[tuple[int, int, str]] = []
        #: Every cancel that reached the wire, as (client id, order id), recorded BEFORE the
        #: gateway's own ownership check (Daybreak r1 P2-1: pins S1-F1's send mechanism).
        self.cancel_requests: list[tuple[int, int]] = []
        #: The zone execution times are rendered in (tz-less); UTC is the fake's convention.
        self.execution_time_zone: Any = EXECUTION_TIME_ZONE
        #: Sim order ids that left the venue WITHOUT a completed-order record (the "vanished"
        #: order of the CANCEL_PENDING settle design: absence alone must never settle).
        self.unrecorded_completed: set[int] = set()
        #: ASSUMED IB: a completedOrder row may carry no ``orderId`` (0). True fills it in.
        self.completed_order_ids = False
        self.completed_requests = 0
        # --- B1 U-a connection-lifecycle knobs (all default-off: the S1b behaviour) ---------
        #: "full" | "no_next_valid_id" | "no_managed_accounts" (which handshake callbacks to send)
        self.handshake = "full"
        #: what managedAccounts reports at the handshake
        self.managed_accounts: tuple[str, ...] = (ACCOUNT,)
        #: client ids a previous session still holds: a connect gets 326 and no handshake
        self.held_client_ids: set[int] = set()
        #: Read-Only API: placeOrder (whatIf included) is answered with 321 and nothing else
        self.read_only = False
        #: a reader thread that ignores the stop sentinel until release_readers()
        self.stuck_reader = False
        self.readers_released = threading.Event()
        #: every EClient call that reached this gateway, in order, as (client id, method)
        self.transcript: list[tuple[int | None, str]] = []
        _run_without_a_loop(self._seed_registry())

    @classmethod
    def seeded(cls) -> FakeGateway:
        sim = _seeded_contract_broker()
        client_of = {OWNED_ID: CLIENT_ID, FOREIGN_ID: MANUAL_CLIENT_ID}
        return cls(sim, client_of=client_of)

    async def _seed_registry(self) -> None:
        await self.sim.connect()
        for symbol in ("AAPL", "MSFT"):
            contract = await self.sim.qualify_underlying(symbol)
            self._registry[contract.con_id] = contract
        option = option_contract()
        self._registry[option.con_id] = option

    def sim_now(self) -> datetime:
        out: list[datetime] = []

        async def read() -> None:
            out.append(await self.sim.server_time())

        _run_without_a_loop(read())
        return out[0]

    def _run(self, coroutine: Any) -> Any:
        box: list[Any] = []

        async def capture() -> None:
            box.append(await coroutine)

        _run_without_a_loop(capture())
        return box[0]

    # session ---------------------------------------------------------------------------------
    def attach(self, client: FakeEClient) -> None:
        with self._lock:
            if client.client_id in self.held_client_ids:
                # ASSUMED-IB: the refusal arrives id-less (-1), before any handshake callback.
                self._error(
                    client,
                    -1,
                    CODE_CLIENT_ID_IN_USE,
                    "Unable to connect as the client id is already in use. "
                    "Retry with a unique client id.",
                )
                return
            self._clients.append(client)
            if self.handshake != "no_next_valid_id":
                client.emit("nextValidId", self._next_valid_id)
            if self.handshake != "no_managed_accounts":
                client.emit("managedAccounts", ",".join(self.managed_accounts))

    def detach(self, client: FakeEClient) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)

    # B1 U-a controls -------------------------------------------------------------------------
    @property
    def next_valid_id(self) -> int:
        return self._next_valid_id

    def record(self, client: FakeEClient, method: str) -> None:
        with self._lock:
            self.transcript.append((client.client_id, method))

    def release_readers(self) -> None:
        """Let a stuck reader thread end (test teardown)."""

        self.readers_released.set()

    def _broadcast(self, emit: Callable[[FakeEClient], None]) -> None:
        with self._lock:
            clients = list(self._clients)
            for client in clients:
                emit(client)
        for client in clients:
            client.wait_dispatched()

    def change_managed_accounts(self, accounts: tuple[str, ...]) -> None:
        """Mid-session managedAccounts with a new set, dispatched before this returns."""

        self.managed_accounts = accounts
        self._broadcast(lambda client: client.emit("managedAccounts", ",".join(accounts)))

    def emit_error(self, req_id: int, code: int, message: str) -> None:
        """An error callback to every attached client, in the current ``error_style``."""

        self._broadcast(lambda client: self._error(client, req_id, code, message))

    def drop(self) -> None:
        """The gateway closes every session: connectionClosed, then each reader thread ends."""

        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            client._connected = False
            client.emit("connectionClosed")
            client.wait_dispatched()
            client._queue.put(_STOP)

    # helpers ---------------------------------------------------------------------------------
    def _error(self, client: FakeEClient, req_id: int, code: int, message: str) -> None:
        self.errors_sent.append((req_id, code, message))
        if self.error_style == "json":
            client.emit("error", req_id, code, message, "")
        elif self.error_style == "timestamped":
            # The adapter strips a NON-int first value as a timestamp (official_ibkr.py:497-504),
            # so the fake emits that documented form. Real ibapi >= 10.3x is believed to add an
            # int ``errorTime`` as the SECOND argument instead: unverified offline, a K4 check.
            client.emit("error", 1_760_000_000.0, req_id, code, message)
        elif self.error_style == "errortime_second":
            # The shape current ibapi is BELIEVED to use (R-ARITY, unverified offline): an int
            # errorTime second, then the code, the message and the reject json.
            client.emit("error", req_id, 1_760_000_000_000, code, message, "")
        else:
            client.emit("error", req_id, code, message)

    @staticmethod
    def contract_of(instrument: Instrument) -> FakeContract:
        contract = FakeContract()
        contract.conId = instrument.con_id
        contract.symbol = instrument.symbol
        contract.currency = getattr(instrument, "currency", "USD") or "USD"
        if isinstance(instrument, OptionContract):
            contract.secType = "OPT"
            contract.exchange = instrument.exchange
            contract.lastTradeDateOrContractMonth = instrument.expiration.strftime("%Y%m%d")
            contract.right = "P" if instrument.right is OptionRight.PUT else "C"
            contract.strike = float(instrument.strike)
            contract.multiplier = str(int(instrument.multiplier))
            contract.localSymbol = instrument.local_symbol
            contract.tradingClass = instrument.trading_class
        else:
            contract.secType = "STK"
            contract.exchange = getattr(instrument, "exchange", "SMART") or "SMART"
            contract.primaryExchange = getattr(instrument, "primary_exchange", "") or ""
        return contract

    def _order_of(self, order: BrokerOrder, adapter_id: int) -> FakeOrder:
        ib = FakeOrder()
        ib.action = order.side.value
        ib.orderType = "LMT"
        ib.totalQuantity = order.quantity
        ib.lmtPrice = float(order.limit_price)
        ib.account = ACCOUNT
        ib.orderRef = order.order_ref or ""
        ib.transmit = True
        ib.outsideRth = order.outside_rth
        ib.permId = order.permanent_id or 0
        ib.clientId = self._client_of.get(order.broker_order_id, MANUAL_CLIENT_ID)
        del adapter_id
        return ib

    @staticmethod
    def _status_of(order: BrokerOrder) -> str:
        return {
            OrderLifecycle.SUBMITTED: "Submitted",
            OrderLifecycle.PARTIALLY_FILLED: "Submitted",
            OrderLifecycle.CANCEL_PENDING: "PendingCancel",
            OrderLifecycle.FILLED: "Filled",
            OrderLifecycle.CANCELLED: "Cancelled",
            OrderLifecycle.REJECTED: "Inactive",
        }.get(order.lifecycle, "Submitted")

    def _emit_status(self, client: FakeEClient, order: BrokerOrder, adapter_id: int) -> None:
        client.emit(
            "orderStatus",
            adapter_id,
            self._status_of(order),
            float(order.filled_quantity),
            float(order.remaining_quantity),
            0.0,
            order.permanent_id or 0,
            0,
            0.0,
            self._client_of.get(order.broker_order_id, MANUAL_CLIENT_ID),
            "",
            0.0,
        )

    def _execution_of(self, execution: BrokerExecution) -> FakeExecution:
        ib = FakeExecution()
        ib.execId = execution.execution_id
        ib.acctNumber = ACCOUNT
        ib.orderId = self._adapter_of.get(execution.broker_order_id, execution.broker_order_id)
        ib.permId = execution.permanent_id or 0
        ib.clientId = self._client_of.get(execution.broker_order_id, MANUAL_CLIENT_ID)
        ib.orderRef = execution.order_ref or ""
        ib.side = "BOT" if execution.side is OrderSide.BUY else "SLD"
        ib.shares = float(execution.quantity)
        ib.price = float(execution.price)
        ib.time = execution.timestamp.astimezone(self.execution_time_zone).strftime(
            EXECUTION_TIME_FORMAT
        )
        return ib

    def _commission_of(self, execution: BrokerExecution) -> FakeCommissionReport:
        amount = float(execution.commission) if execution.commission is not None else UNSET_DOUBLE
        return FakeCommissionReport(execution.execution_id, amount, "USD")

    def _working(self) -> dict[int, BrokerOrder]:
        return {o.broker_order_id: o for o in self._run(self.sim.open_orders())}

    # reads -------------------------------------------------------------------------------------
    def current_time(self, client: FakeEClient) -> None:
        with self._lock:
            client.emit("currentTime", int(self.sim_now().timestamp()))

    def account_summary(self, client: FakeEClient, req_id: int) -> None:
        with self._lock:
            summary = self._run(self.sim.account_summary())
            for tag, value in (
                ("NetLiquidation", summary.net_liquidation),
                ("TotalCashValue", summary.total_cash),
                ("BuyingPower", summary.buying_power),
            ):
                client.emit("accountSummary", req_id, ACCOUNT, tag, str(value), "USD")
            client.emit("accountSummaryEnd", req_id)

    def positions(self, client: FakeEClient) -> None:
        with self._lock:
            for position in self._run(self.sim.positions()):
                client.emit(
                    "position",
                    ACCOUNT,
                    self.contract_of(position.contract),
                    float(position.quantity),
                    float(position.average_cost),
                )
            client.emit("positionEnd")

    def executions(self, client: FakeEClient, req_id: int, execution_filter: Any) -> None:
        since: datetime | None = None
        raw = str(getattr(execution_filter, "time", "") or "")
        if raw:
            since = datetime.strptime(raw, FILTER_TIME_FORMAT).replace(
                tzinfo=self.execution_time_zone
            )
        with self._lock:
            for execution in self._run(self.sim.executions()):
                stamp = execution.timestamp.astimezone(UTC).replace(microsecond=0)
                if since is not None and stamp < since:
                    continue
                client.emit(
                    "execDetails",
                    req_id,
                    self.contract_of(execution.contract),
                    self._execution_of(execution),
                )
                client.emit("commissionReport", self._commission_of(execution))
            client.emit("execDetailsEnd", req_id)

    def open_orders(self, client: FakeEClient) -> None:
        with self._lock:
            for sim_id, order in self._working().items():
                adapter_id = self._adapter_of.get(sim_id, sim_id)
                client.emit(
                    "openOrder",
                    adapter_id,
                    self.contract_of(order.contract),
                    self._order_of(order, adapter_id),
                    FakeOrderState(self._status_of(order)),
                )
                self._emit_status(client, order, adapter_id)
            client.emit("openOrderEnd")

    def completed_orders(self, client: FakeEClient, api_only: bool) -> None:
        """``reqCompletedOrders``: one ``completedOrder`` per terminal order, then the end marker.

        ASSUMED IB (K4): rows carry account, ``orderRef``, permId, clientId, the order's contract,
        side, ``totalQuantity``, ``lmtPrice``, ``filledQuantity`` and the final status; ``apiOnly``
        drops manual (client 0) orders.
        """

        with self._lock:
            self.completed_requests += 1
            for order in self.sim.terminal_orders():
                sim_id = order.broker_order_id
                if sim_id in self.unrecorded_completed:
                    continue
                if api_only and self._client_of.get(sim_id, MANUAL_CLIENT_ID) == MANUAL_CLIENT_ID:
                    continue
                adapter_id = self._adapter_of.get(sim_id, sim_id)
                ib = self._order_of(order, adapter_id)
                ib.filledQuantity = order.filled_quantity
                ib.orderId = adapter_id if self.completed_order_ids else 0
                client.emit(
                    "completedOrder",
                    self.contract_of(order.contract),
                    ib,
                    FakeOrderState(self._status_of(order)),
                )
            client.emit("completedOrdersEnd")

    def vanish_without_record(self, sim_id: int) -> None:
        """Drop a working order from the venue with NO status and NO completed-order record."""

        with self._lock:
            self._run(self.sim.cancel_order(sim_id))
            self.unrecorded_completed.add(sim_id)

    # orders ------------------------------------------------------------------------------------
    def _request_of(
        self, contract: FakeContract, order: FakeOrder, *, transmit: bool
    ) -> OrderRequest:
        instrument = self._registry[int(contract.conId)]
        side = OrderSide(order.action)
        if isinstance(instrument, OptionContract):
            intent = (
                OrderIntent.OPEN_SHORT_PUT
                if side is OrderSide.SELL
                else OrderIntent.CLOSE_SHORT_OPTION
            )
        else:
            intent = (
                OrderIntent.OPEN_LONG_STOCK
                if side is OrderSide.BUY
                else OrderIntent.CLOSE_LONG_STOCK
            )
        return OrderRequest(
            correlation_id=f"fake-gateway-{order.orderRef or 'order'}",
            account_id=str(order.account),
            contract=instrument,
            intent=intent,
            side=side,
            quantity=Decimal(str(order.totalQuantity)),
            limit_price=Decimal(str(order.lmtPrice)),
            order_ref=str(order.orderRef),
            transmit=transmit,
        )

    def place_order(
        self, client: FakeEClient, adapter_id: int, contract: FakeContract, order: FakeOrder
    ) -> None:
        with self._lock:
            self._next_valid_id = max(self._next_valid_id, adapter_id + 1)
            if self.read_only:
                self._error(
                    client,
                    adapter_id,
                    CODE_READ_ONLY,
                    "Error validating request: The API interface is currently in Read-Only mode.",
                )
                return
            if int(contract.conId) not in self._registry:
                self._error(client, adapter_id, CODE_NO_SECURITY, "No security definition found")
                return
            instrument = self._registry[int(contract.conId)]
            if order.whatIf:
                preview = self._run(
                    self.sim.preview_order(self._request_of(contract, order, transmit=False))
                )
                client.emit(
                    "openOrder",
                    adapter_id,
                    self.contract_of(instrument),
                    order,
                    FakeOrderState(
                        "PreSubmitted", commission=float(preview.estimated_commission or 0)
                    ),
                )
                return
            submission = self._run(
                self.sim.submit_order(
                    self._request_of(contract, order, transmit=True), send_guard=_SeedGuard()
                )
            )
            sim_id = submission.broker_order_id
            self._client_of[sim_id] = int(client.client_id or 0)
            self._adapter_of[sim_id] = adapter_id
            self._sim_of[adapter_id] = sim_id
            if submission.lifecycle is OrderLifecycle.REJECTED:
                self._error(
                    client, adapter_id, CODE_ORDER_REJECTED, submission.message or "rejected"
                )
                return
            working = self._working().get(sim_id)
            if working is None:
                return
            client.emit(
                "openOrder",
                adapter_id,
                self.contract_of(instrument),
                self._order_of(working, adapter_id),
                FakeOrderState(self._status_of(working)),
            )
            self._emit_status(client, working, adapter_id)

    def cancel_order(self, client: FakeEClient, adapter_id: int) -> None:
        with self._lock:
            self.cancel_requests.append((int(client.client_id or 0), adapter_id))
            sim_id = self._sim_of.get(adapter_id)
            if sim_id is None or self._client_of.get(sim_id) != client.client_id:
                # IB: unknown, or another client's order -> "not found" (assumed, verify at K4).
                self._error(
                    client, adapter_id, CODE_CANCEL_NOT_FOUND, "Order to be cancelled is not found"
                )
                return
            result = self._run(self.sim.cancel_order(sim_id))
            if not result.requested:
                self._error(client, adapter_id, CODE_CANNOT_CANCEL, "Order cannot be cancelled")
                return
            # IB reports PendingCancel before Cancelled (assumed, verify at K4; fable r1 P3-2).
            statuses = ["PendingCancel"]
            if result.lifecycle is OrderLifecycle.CANCELLED:
                statuses.append("Cancelled")
            for status in statuses:
                client.emit(
                    "orderStatus",
                    adapter_id,
                    status,
                    0.0,
                    0.0,
                    0.0,
                    0,
                    0,
                    0.0,
                    int(client.client_id or 0),
                    "",
                    0.0,
                )

    # test controls -------------------------------------------------------------------------------
    def add_working_order(
        self, *, ref: str, symbol: str, quantity: str, limit: str, client_id: int
    ) -> int:
        """Seed one more working stock BUY at the venue under ``client_id`` (no execution)."""

        with self._lock:
            contract = self._run(self.sim.qualify_underlying(symbol))
            submission = self._run(
                self.sim.submit_order(
                    OrderRequest(
                        correlation_id=f"seed-{ref}",
                        account_id=ACCOUNT,
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
            )
            sim_id: int = submission.broker_order_id
            self._client_of[sim_id] = client_id
            self._adapter_of[sim_id] = sim_id
            self._sim_of[sim_id] = sim_id
            self._next_valid_id = max(self._next_valid_id, sim_id + 1)
            return sim_id

    def fill_sim_order(self, sim_id: int, quantity: Decimal) -> list[str]:
        """Fill at the venue and stream it live to every attached client, IB-ordered.

        Returns the new execution ids, only AFTER each client's reader has dispatched the last
        callback (``wait_dispatched``), so the next adapter read sees the whole fill.
        """

        with self._lock:
            before = {e.execution_id for e in self._run(self.sim.executions())}
            self.sim.fill_order(sim_id, quantity)
            new = [e for e in self._run(self.sim.executions()) if e.execution_id not in before]
            order = next(
                (o for o in self._run(self.sim.open_orders()) if o.broker_order_id == sim_id), None
            )
            clients = list(self._clients)
            adapter_id = self._adapter_of.get(sim_id, sim_id)
            for client in clients:
                if order is not None:
                    self._emit_status(client, order, adapter_id)
                else:
                    filled = sum((e.quantity for e in new), Decimal(0))
                    client.emit(
                        "orderStatus",
                        adapter_id,
                        "Filled",
                        float(filled),
                        0.0,
                        0.0,
                        0,
                        0,
                        0.0,
                        self._client_of.get(sim_id, MANUAL_CLIENT_ID),
                        "",
                        0.0,
                    )
                for execution in new:
                    client.emit(
                        "execDetails",
                        -1,
                        self.contract_of(execution.contract),
                        self._execution_of(execution),
                    )
                    client.emit("commissionReport", self._commission_of(execution))
        for client in clients:
            client.wait_dispatched()
        return [e.execution_id for e in new]


# --- the tests-only seam -----------------------------------------------------------------------


def install(monkeypatch: pytest.MonkeyPatch, gateway: FakeGateway) -> None:
    """Point the adapter's two loaders at this gateway's stand-in classes, and forbid sockets.

    Scoped to one test by ``monkeypatch``; ``sys.modules`` is never touched.
    """

    from chronos.broker import official_ibkr

    client_class = FakeEClient.bound_to(gateway)
    monkeypatch.setattr(
        official_ibkr,
        "_load_ibapi",
        lambda: (client_class, FakeEWrapper, FakeContract, FakeExecutionFilter),
    )
    monkeypatch.setattr(official_ibkr, "_load_order_class", lambda: FakeOrder)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the FakeGateway must never open a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def official_settings() -> Settings:
    """Paper settings for the adapter over the fake: every ALLOW_* flag stays FALSE."""

    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        broker_mode=BrokerMode.IBKR,
        broker_adapter=BrokerAdapter.OFFICIAL_IBKR,
        ib_environment=IBEnvironment.PAPER,
        ib_host="127.0.0.1",
        ib_port=7497,
        ib_client_id=CLIENT_ID,
        ib_account_id=ACCOUNT,
        allow_order_transmit=False,
        allow_live_trading=False,
    )


class InstalledGatewayLedger:
    """An ``OrderOwnershipLedger`` double standing in for the persisted order book (S1-F1).

    It is the fake persisted book for tests that cancel through the adapter over the gateway:
    it proves owned only an order the INSTALLED gateway holds with a ``CHR-`` ref, this
    adapter's client id, the configured account and the same permId. It is deliberately NOT the
    adapter's own pre-filter restated: it compares the identity the adapter read from the venue
    with the gateway's own record of the order. The real ledger is tested separately
    (``tests/integration/test_order_ownership_ledger.py``).
    """

    def proves_owned(self, identity: Any) -> bool:
        from chronos.broker import official_ibkr

        gateway = official_ibkr._load_ibapi()[0].gateway  # the one install() bound
        order = next(
            (
                order
                for order in gateway._run(gateway.sim.open_orders())
                if order.broker_order_id == identity.broker_order_id
            ),
            None,
        )
        return (
            order is not None
            and str(order.order_ref or "").startswith("CHR-")
            and order.order_ref == identity.order_ref
            and order.permanent_id == identity.permanent_id
            and identity.account_id == ACCOUNT
            and identity.client_id == CLIENT_ID
            and gateway._client_of.get(order.broker_order_id) == CLIENT_ID
        )


# --- the U1 contract hook (tests/contract/conftest.py) -----------------------------------------


def _build_official() -> Any:
    from chronos.broker.official_ibkr import OfficialIBKRBroker

    return OfficialIBKRBroker(official_settings(), order_ownership=InstalledGatewayLedger())


async def _contract_fill(broker: Any, broker_order_id: int, quantity: Decimal) -> None:
    gateway = broker._app.gateway
    gateway.fill_sim_order(gateway._sim_of.get(broker_order_id, broker_order_id), quantity)


def contract_harness() -> Any:
    """The ``official_fake`` harness: the REAL adapter over the FakeGateway.

    The gateway itself is created and installed per case by the contract suite's autouse
    fixture (``_official_fake_gateway``), before ``build()`` runs (the constructor already calls
    the loader). The submit cases are deferred to S2 by the suite (``S2_DEFERRED``).
    """

    from tests.contract.conftest import BrokerHarness

    return BrokerHarness(
        name="official_fake",
        build=_build_official,
        account_id=ACCOUNT,
        owned_order_id=OWNED_ID,
        accepts_orders=True,
        implements_full_protocol=True,
        foreign_order_id=FOREIGN_ID,
        cause_fill=_contract_fill,
    )
