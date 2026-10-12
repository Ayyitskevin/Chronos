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
        self._connected = True
        self.gateway.attach(self)

    def run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                name, args = item
                getattr(self.wrapper, name)(*args)
            finally:
                self._queue.task_done()  # only AFTER the callback returned (reader P2-1a)

    def isConnected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
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
        del marketDataType

    def reqCurrentTime(self) -> None:
        self.gateway.current_time(self)

    def reqAccountSummary(self, reqId: int, group: str, tags: str) -> None:
        del group, tags
        self.gateway.account_summary(self, reqId)

    def cancelAccountSummary(self, reqId: int) -> None:
        del reqId

    def reqPositions(self) -> None:
        self.gateway.positions(self)

    def cancelPositions(self) -> None:
        pass

    def reqExecutions(self, reqId: int, execFilter: FakeExecutionFilter) -> None:
        self.gateway.executions(self, reqId, execFilter)

    def reqAllOpenOrders(self) -> None:
        self.gateway.open_orders(self)

    def placeOrder(self, orderId: int, contract: FakeContract, order: FakeOrder) -> None:
        self.gateway.place_order(self, int(orderId), contract, order)

    def cancelOrder(self, orderId: int, manualCancelOrderTime: str = "") -> None:
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
            self._clients.append(client)
            client.emit("nextValidId", self._next_valid_id)
            client.emit("managedAccounts", ACCOUNT)

    def detach(self, client: FakeEClient) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)

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
        ib.time = execution.timestamp.astimezone(UTC).strftime("%Y%m%d %H:%M:%S")
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
            since = datetime.strptime(raw, "%Y%m%d-%H:%M:%S").replace(tzinfo=UTC)
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
            status = (
                "Cancelled" if result.lifecycle is OrderLifecycle.CANCELLED else "PendingCancel"
            )
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


# --- the U1 contract hook (tests/contract/conftest.py) -----------------------------------------


def _build_official() -> Any:
    from chronos.broker.official_ibkr import OfficialIBKRBroker

    return OfficialIBKRBroker(official_settings())


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
