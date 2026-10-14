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

B1 U-f1 ADDITIONS (the capture workload; every one ASSUMED-IB, the K4 / B1-day verification list):
the option market-rule id and its increment bands (:data:`OPTION_RULE_ID` 9001 is an arbitrary
stand-in, not a real IB rule id); that ``validExchanges`` and ``marketRuleIds`` are comma lists
aligned by position; that option ``ContractDetails`` carry ``liquidHours``, ``timeZoneId``,
``minTick``, ``underConId``, ``underSymbol`` and ``underSecType`` ("STK"), with the same hours
string the underlying fake uses; the option tick set (bid 1, ask 2, last 4, put/call volume 30/29
and open interest 28/27, ``tickOptionComputation`` types 10/11/12 before the model type 13, in
that order, LIVE tick ids only: a delayed type-3/4 stream would use 66/67/68 and 83, which this
fake does not send); a streaming option request has no ``tickSnapshotEnd``; ``position`` avgCost
for an option is per contract with the multiplier included; the daily-bar date text (``YYYYMMDD``
under formatDate 1), that the sim date's bar exists only from the 09:30 New York open, is partial
(a quarter of the day's volume) until the 16:00 close and complete after it, and that weekends
carry none, no exchange holidays, no ``barCount``/``wap`` semantics, and the error-162 text; and
everything about INTRADAY bars (refused loudly: not modelled). What a real gateway does for an
unknown market rule id is not known: the fake refuses it loudly rather than invent an answer.

B1 U-f1 r1 ADDITIONS (reader review of 2c1c764; also ASSUMED-IB, the K4 / B1-day list): the fake
records every served historical request in :attr:`FakeGateway.historical_requests` and refuses
``keepUpToDate=True`` loudly, so the ADAPTER's request (TRADES, regular hours, YYYYMMDD, not
streaming) is pinned; it does NOT model what a real gateway does with ``useRTH``,
``whatToShow``, ``endDateTime`` or ``keepUpToDate`` (it serves the same series whatever they
are), so only the request is rehearsed, not the venue's answer to it. Also NOT modelled, each a
K4 / B1-day item: QUALIFICATION CARDINALITY (exactly one detail per option identity here; a real
gateway can answer several for one identity, for example several trading classes or exchanges,
which the adapter must treat as ambiguous); CHAIN REALISM (the secDefOptParams reply is one SMART
row; real replies carry several exchanges, trading classes and multipliers and hundreds of
strikes, so candidate-count and size limits are not exercised); the SENTINEL FAMILY (the
unset-double maximum float and -1.0 for implied volatility are pinned; any other value a real
gateway uses for "not computed" is not). SCOPE: ``capture_readonly.py`` never requests option
quotes (it stops at qualification and ``option_market_rules``), so the option tick model above
rehearses ``request_option_quotes``, a path the B1 capture does not run: U-f2 should not expect
option ticks in a capture.
"""

from __future__ import annotations

import queue
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

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
    UnderlyingContract,
)
from tests.support.order_fakes import option_contract
from tests.support.sim_broker import (
    _CONTRACT_ACCOUNT,
    SimBroker,
    SimClock,
    _run_without_a_loop,
    _seeded_contract_broker,
    _SeedGuard,
)

ACCOUNT = _CONTRACT_ACCOUNT
CLIENT_ID = 17
#: the exchange the fake's bars are dated in, and its regular session (ASSUMED-IB: no holidays)
EXCHANGE_TIME_ZONE = ZoneInfo("America/New_York")
SESSION_OPEN = (9, 30)
SESSION_CLOSE = (16, 0)
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

#: ASSUMED-IB (K4): an arbitrary stand-in for the option market-rule id and its bands (low edge,
#: increment); a real capture supplies the real ones.
OPTION_RULE_ID = 9001
OPTION_RULE_INCREMENTS: tuple[tuple[float, float], ...] = ((0.0, 0.01), (3.0, 0.05))
#: The capture day's option con ids (AAPL, underlying 999, around the fake's spot of 100).
CAPTURE_OPTION_CON_IDS = {
    "PUT_100_NOV": 211,
    "PUT_95_NOV": 212,
    "PUT_90_NOV": 213,
    "CALL_100_NOV": 214,
    "PUT_95_DEC": 215,
}

_STOP = object()

#: How long ``wait_dispatched`` waits for the reader thread before failing the test (it used to
#: block forever when a callback raised and killed the reader: reader build-ua P3-2).
DISPATCH_TIMEOUT_S = 10.0


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


class FakeContractDetails:
    """The ContractDetails fields the adapter reads on the smoke's qualification path."""

    def __init__(self, contract: FakeContract) -> None:
        self.contract = contract
        self.liquidHours = "20261012:0930-20261012:1600;20261013:0930-20261013:1600"
        self.timeZoneId = "US/Eastern"
        self.minTick = 0.01
        self.marketRuleIds = ""
        # B1 U-f1: the option evidence fields the adapter reads off the details (ASSUMED-IB).
        self.validExchanges = "SMART"
        self.underConId = 0
        self.underSymbol = ""
        self.underSecType = ""


class FakePriceIncrement:
    """One market-rule band (``ibapi.common.PriceIncrement``)."""

    def __init__(self, low_edge: float, increment: float) -> None:
        self.lowEdge = low_edge
        self.increment = increment


class FakeBarData:
    """One historical bar (``ibapi.common.BarData``); ``date`` is the ``YYYYMMDD`` daily text."""

    def __init__(
        self,
        date_text: str,
        *,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float = 1_000_000.0,
    ) -> None:
        self.date = date_text
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.wap = close
        self.barCount = 0


@dataclass(frozen=True)
class OptionTicks:
    """What one option's market-data stream carries (deterministic; ASSUMED-IB shapes)."""

    bid: float
    ask: float
    last: float
    volume: float
    open_interest: float
    implied_vol: float
    delta: float
    gamma: float
    theta: float
    vega: float
    opt_price: float
    und_price: float

    @classmethod
    def default(cls, right: OptionRight) -> OptionTicks:
        if right is OptionRight.PUT:
            return cls(3.10, 3.20, 3.15, 120.0, 3400.0, 0.30, -0.47, 0.05, -0.06, 0.11, 3.15, 100.0)
        return cls(3.40, 3.50, 3.45, 240.0, 5100.0, 0.30, 0.53, 0.05, -0.06, 0.11, 3.45, 100.0)

    @classmethod
    def unset_greeks(cls) -> OptionTicks:
        """The model computation arrives with every figure unset (the sentinel)."""

        return replace(
            cls.default(OptionRight.PUT),
            implied_vol=UNSET_DOUBLE,
            delta=UNSET_DOUBLE,
            gamma=UNSET_DOUBLE,
            theta=UNSET_DOUBLE,
        )

    def computation(self, tick_type: int) -> tuple[float, ...]:
        """The eight ``tickOptionComputation`` figures; 10/11/12 differ from the model (13)."""

        shift = 13 - tick_type  # 3, 2, 1 for bid/ask/last computations, 0 for the model
        return (
            round(self.implied_vol + 0.01 * shift, 4),
            self.delta - 0.01 * shift if abs(self.delta) < 1e100 else self.delta,
            round(self.opt_price + 0.01 * shift, 4),
            0.0,
            self.gamma,
            self.vega,
            self.theta,
            self.und_price,
        )


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
                try:
                    getattr(self.wrapper, name)(*args)
                except Exception as error:  # the real ibapi reader survives a raising callback
                    self.gateway.callback_errors.append(f"{name}: {type(error).__name__}: {error}")
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

    def wait_dispatched(self, timeout: float = DISPATCH_TIMEOUT_S) -> None:
        """Block until every callback queued so far has been DISPATCHED (not merely dequeued).

        Fails the test, instead of hanging it, when the callbacks are not all dispatched within
        ``timeout`` or when any callback raised (the reader records and survives it).
        """

        deadline = time.monotonic() + timeout
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"FakeGateway callbacks were not dispatched within {timeout:.0f}s "
                        "(is the reader thread alive?)"
                    )
                self._queue.all_tasks_done.wait(remaining)
        if self.gateway.callback_errors:
            raise AssertionError(f"a FakeGateway callback raised: {self.gateway.callback_errors}")

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
        self.gateway.record(self, "reqCompletedOrders")
        self.gateway.completed_orders(self, bool(apiOnly))

    def placeOrder(self, orderId: int, contract: FakeContract, order: FakeOrder) -> None:
        self.gateway.record(self, "placeOrder")
        self.gateway.place_order(self, int(orderId), contract, order)

    def reqContractDetails(self, reqId: int, contract: FakeContract) -> None:
        self.gateway.record(self, "reqContractDetails")
        self.gateway.contract_details(self, int(reqId), contract)

    def reqSecDefOptParams(
        self,
        reqId: int,
        underlyingSymbol: str,
        futFopExchange: str,
        underlyingSecType: str,
        underlyingConId: int,
    ) -> None:
        del futFopExchange, underlyingSecType
        self.gateway.record(self, "reqSecDefOptParams")
        self.gateway.sec_def_opt_params(self, int(reqId), underlyingSymbol, int(underlyingConId))

    def reqMktData(
        self,
        reqId: int,
        contract: FakeContract,
        genericTickList: str,
        snapshot: bool,
        regulatorySnapshot: bool,
        mktDataOptions: Any,
    ) -> None:
        del genericTickList, regulatorySnapshot, mktDataOptions
        self.gateway.record(self, "reqMktData")
        self.gateway.market_data(self, int(reqId), contract, snapshot=bool(snapshot))

    def cancelMktData(self, reqId: int) -> None:
        del reqId
        self.gateway.record(self, "cancelMktData")

    def reqMarketRule(self, marketRuleId: int) -> None:
        self.gateway.record(self, "reqMarketRule")
        self.gateway.market_rule(self, int(marketRuleId))

    def reqHistoricalData(
        self,
        reqId: int,
        contract: FakeContract,
        endDateTime: str,
        durationStr: str,
        barSizeSetting: str,
        whatToShow: str,
        useRTH: int,
        formatDate: int,
        keepUpToDate: bool,
        chartOptions: Any,
    ) -> None:
        self.gateway.record(self, "reqHistoricalData")
        if keepUpToDate:
            raise AssertionError(
                "historical request keepUpToDate=True is not modelled: the fake serves one-shot "
                "(non-streaming) daily bars only"
            )
        self.gateway.historical_data(
            self, int(reqId), contract, str(durationStr), str(barSizeSetting), int(formatDate)
        )
        self.gateway.historical_requests.append(
            (
                int(contract.conId),
                str(endDateTime),
                str(durationStr),
                str(barSizeSetting),
                str(whatToShow),
                int(useRTH),
                int(formatDate),
                bool(keepUpToDate),
                list(chartOptions or []),
            )
        )

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
        #: every exception an adapter callback raised on a reader thread (recorded, not lost)
        self.callback_errors: list[str] = []
        # --- B1 U-b market-data knobs (all default-off) -----------------------------------
        #: con_id -> how a snapshot reqMktData is answered (default: type 1 with prices):
        #:   int < 100: that marketDataType, then prices, then tickSnapshotEnd;
        #:   int >= 100: that error code for the request, nothing else;
        #:   "hang": nothing; "no_type": prices + end, no marketDataType;
        #:   "empty": marketDataType 1 + end, no prices;
        #:   ("notice_then", code, type): the error, then that type + prices + end;
        #:   a list: one element per successive request (the last one repeats).
        self.entitlement: dict[int, Any] = {}
        self._entitlement_calls: dict[int, int] = {}
        #: extra accountSummary rows (tag, value, currency), sent after the standard three
        self.extra_summary_rows: list[tuple[str, str, str]] = []
        #: extra position rows (instrument, quantity, avgCost), sent after the venue's
        self.extra_positions: list[tuple[Instrument, Decimal, float]] = []
        # --- B1 U-f1 capture-workload knobs (all default-off: the S1b/U-b behaviour) --------
        #: market rule id -> (low edge, increment) bands
        self.market_rules: dict[int, tuple[tuple[float, float], ...]] = {
            OPTION_RULE_ID: OPTION_RULE_INCREMENTS
        }
        #: option con id -> attributes to overwrite on its ContractDetails (blank a field, etc.)
        self.details_override: dict[int, dict[str, Any]] = {}
        #: option con id -> its tick set (default: :meth:`OptionTicks.default` for its right)
        self.option_ticks: dict[int, OptionTicks] = {}
        #: con id -> the bars sent instead of the generated ones (malformed rows included)
        self.bars_override: dict[int, list[FakeBarData]] = {}
        #: con id -> how reqHistoricalData is answered: an int >= 100 is that error code for
        #: the request and nothing else; "hang" sends nothing
        self.historical: dict[int, Any] = {}
        #: every SERVED reqHistoricalData as (conId, endDateTime, duration, barSize, whatToShow,
        #: useRTH, formatDate, keepUpToDate, chartOptions); a refused request is not recorded
        self.historical_requests: list[tuple[Any, ...]] = []
        _run_without_a_loop(self._seed_registry())

    @classmethod
    def seeded(cls) -> FakeGateway:
        sim = _seeded_contract_broker()
        client_of = {OWNED_ID: CLIENT_ID, FOREIGN_ID: MANUAL_CLIENT_ID}
        return cls(sim, client_of=client_of)

    @classmethod
    def capture_day(cls) -> FakeGateway:
        """The seeded gateway plus what a capture day looks like (deterministic, ASSUMED-IB).

        A five-contract AAPL option chain around the fake's spot of 100 (puts 90/95/100 and a call
        100 for 2026-11-20, a put 95 for 2026-12-18, beside the seed's expired 150 put); a short
        put position; a second Chronos stock order (MSFT, 20 at 410) filled 8, which is a second
        execution with its commission, a stock position and a partly filled working order; and
        the seed's owned and manual orders. Everything is read back through the real adapter.
        """

        gateway = cls.seeded()
        for option in _capture_day_options():
            gateway.add_option(option)
        sim_id = gateway.add_working_order(
            ref="CHR-CAPTURE-1", symbol="MSFT", quantity="20", limit="410.00", client_id=CLIENT_ID
        )
        gateway.fill_sim_order(sim_id, Decimal("8"))
        short_put = gateway._registry[CAPTURE_OPTION_CON_IDS["PUT_100_NOV"]]
        gateway.extra_positions.append(
            (short_put, Decimal("-1"), 310.0)
        )  # 3.10 x 100, per contract
        return gateway

    def add_option(self, option: OptionContract) -> None:
        """Make one more option known to the venue (contract details, chain, market data)."""

        with self._lock:
            self._registry[option.con_id] = option

    async def _seed_registry(self) -> None:
        await self.sim.connect()
        for symbol in ("AAPL", "MSFT"):
            contract = await self.sim.qualify_underlying(symbol)
            self._registry[contract.con_id] = contract
        option = option_contract()
        self._registry[option.con_id] = option

    def set_sim_time(self, when: datetime) -> None:
        """Move the manual sim clock forward (it never moves back); the fake has no other clock."""

        clock = self.sim._clock
        if not isinstance(clock, SimClock):
            raise AssertionError("the fake's sim clock is not a SimClock; cannot move it")
        clock.set(when)

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
            for tag, raw, currency in self.extra_summary_rows:
                client.emit("accountSummary", req_id, ACCOUNT, tag, raw, currency)
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
            for instrument, quantity, avg_cost in self.extra_positions:
                client.emit(
                    "position", ACCOUNT, self.contract_of(instrument), float(quantity), avg_cost
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

    # market data (B1 U-b: the smoke's read path) ------------------------------------------------
    _MARKET_DATA_MESSAGES: ClassVar[dict[int, str]] = {
        100: "Max rate of messages per second has been exceeded",
        101: "Max number of tickers has been reached",
        354: "Requested market data is not subscribed",
        420: "Invalid real-time query: pacing violation",
        10089: "Requested market data requires additional subscription for API",
        10090: "Part of requested market data is not subscribed",
        10091: "Part of requested market data requires additional subscription for API",
        10167: "Requested market data is not subscribed. Displaying delayed market data",
        10186: "Requested market data is not subscribed. Delayed market data is not enabled",
        10189: "Failed to request tick-by-tick data",
        10197: "No market data during competing live session",
    }
    _UNDERLYING_PRICES: ClassVar[dict[int, float]] = {
        1: 99.90,
        2: 100.10,
        4: 100.00,
        9: 99.50,
    }  # bid, ask, last, close

    def _options_matching(self, contract: FakeContract) -> list[OptionContract]:
        """The registry's options a details query names: by con id, else by the full identity."""

        options = [i for i in self._registry.values() if isinstance(i, OptionContract)]
        con_id = int(contract.conId or 0)
        if con_id:
            return [option for option in options if option.con_id == con_id]
        return [
            option
            for option in options
            if option.symbol == str(contract.symbol or "").upper()
            and option.expiration.strftime("%Y%m%d") == str(contract.lastTradeDateOrContractMonth)
            and abs(float(option.strike) - float(contract.strike)) < 1e-9
            and option.right.value == str(contract.right or "")[:1].upper()
            and (not contract.tradingClass or option.trading_class == contract.tradingClass)
        ]

    def _option_details(self, option: OptionContract) -> FakeContractDetails:
        details = FakeContractDetails(self.contract_of(option))
        details.validExchanges = "SMART,CBOE"
        details.marketRuleIds = f"{OPTION_RULE_ID},{OPTION_RULE_ID}"  # aligned by position
        details.underConId = int(option.underlying_con_id or 0)
        details.underSymbol = option.symbol
        details.underSecType = "STK"
        for name, value in self.details_override.get(option.con_id, {}).items():
            setattr(details, name, value)
        return details

    def contract_details(self, client: FakeEClient, req_id: int, contract: FakeContract) -> None:
        with self._lock:
            by_id = self._registry.get(int(contract.conId or 0))
            if str(contract.secType) == "OPT" or isinstance(by_id, OptionContract):
                options = self._options_matching(contract)
                if not options:
                    self._error(
                        client,
                        req_id,
                        CODE_NO_SECURITY,
                        "No security definition has been found for the request",
                    )
                    return
                for option in options:
                    client.emit("contractDetails", req_id, self._option_details(option))
                client.emit("contractDetailsEnd", req_id)
                return
            symbol = str(contract.symbol or "").upper()
            matches = [
                instrument
                for instrument in self._registry.values()
                if isinstance(instrument, UnderlyingContract) and instrument.symbol == symbol
            ]
            if not matches:
                self._error(
                    client,
                    req_id,
                    CODE_NO_SECURITY,
                    "No security definition has been found for the request",
                )
                return
            for instrument in matches:
                client.emit(
                    "contractDetails", req_id, FakeContractDetails(self.contract_of(instrument))
                )
            client.emit("contractDetailsEnd", req_id)

    def market_rule(self, client: FakeEClient, rule_id: int) -> None:
        with self._lock:
            bands = self.market_rules.get(rule_id)
            if bands is None:
                raise AssertionError(
                    f"market rule {rule_id} is not modelled (ASSUMED-IB, K4: what a real gateway "
                    "answers for an unknown rule id is not known)"
                )
            client.emit("marketRule", rule_id, [FakePriceIncrement(low, inc) for low, inc in bands])

    def sec_def_opt_params(
        self, client: FakeEClient, req_id: int, symbol: str, underlying_con_id: int
    ) -> None:
        with self._lock:
            options = [
                instrument
                for instrument in self._registry.values()
                if isinstance(instrument, OptionContract)
                and instrument.symbol == symbol
                and instrument.underlying_con_id == underlying_con_id
            ]
            if options:
                client.emit(
                    "securityDefinitionOptionParameter",
                    req_id,
                    "SMART",
                    underlying_con_id,
                    options[0].trading_class,
                    str(int(options[0].multiplier)),
                    {option.expiration.strftime("%Y%m%d") for option in options},
                    {float(option.strike) for option in options},
                )
            client.emit("securityDefinitionOptionParameterEnd", req_id)

    def _entitlement_for(self, con_id: int) -> Any:
        answer = self.entitlement.get(con_id, 1)
        if isinstance(answer, list):
            index = self._entitlement_calls.get(con_id, 0)
            self._entitlement_calls[con_id] = index + 1
            return answer[min(index, len(answer) - 1)]
        return answer

    def _emit_prices(self, client: FakeEClient, req_id: int, con_id: int) -> None:
        instrument = self._registry.get(con_id)
        if isinstance(instrument, OptionContract):
            self._emit_option_ticks(client, req_id, instrument)
            return
        for tick_type, price in self._UNDERLYING_PRICES.items():
            client.emit("tickPrice", req_id, tick_type, price, None)
        client.emit("tickSize", req_id, 8, 1000.0)

    def _emit_option_ticks(self, client: FakeEClient, req_id: int, option: OptionContract) -> None:
        ticks = self.option_ticks.get(option.con_id) or OptionTicks.default(option.right)
        put = option.right is OptionRight.PUT
        client.emit("tickPrice", req_id, 1, ticks.bid, None)
        client.emit("tickPrice", req_id, 2, ticks.ask, None)
        client.emit("tickPrice", req_id, 4, ticks.last, None)
        client.emit("tickSize", req_id, 30 if put else 29, ticks.volume)
        client.emit("tickSize", req_id, 28 if put else 27, ticks.open_interest)
        for tick_type in (10, 11, 12, 13):  # bid/ask/last computations, then the model's
            client.emit(
                "tickOptionComputation", req_id, tick_type, 0, *ticks.computation(tick_type)
            )

    def market_data(
        self, client: FakeEClient, req_id: int, contract: FakeContract, *, snapshot: bool
    ) -> None:
        with self._lock:
            con_id = int(contract.conId)
            # the smoke asks for snapshots (ended by tickSnapshotEnd); an option STREAM has no end
            ends = snapshot or not isinstance(self._registry.get(con_id), OptionContract)
            answer = self._entitlement_for(con_id)
            if answer == "hang":
                return
            if isinstance(answer, tuple) and answer[0] == "notice_then":
                _, code, market_data_type = answer
                self._error(client, req_id, code, self._MARKET_DATA_MESSAGES.get(code, "notice"))
                client.emit("marketDataType", req_id, market_data_type)
                self._emit_prices(client, req_id, con_id)
                if ends:
                    client.emit("tickSnapshotEnd", req_id)
                return
            if isinstance(answer, int) and answer >= 100:
                self._error(client, req_id, answer, self._MARKET_DATA_MESSAGES.get(answer, "error"))
                return
            if answer == "no_type":
                self._emit_prices(client, req_id, con_id)
            elif answer == "empty":
                client.emit("marketDataType", req_id, 1)
            else:
                client.emit("marketDataType", req_id, int(answer))
                self._emit_prices(client, req_id, con_id)
            if ends:
                client.emit("tickSnapshotEnd", req_id)

    # historical bars (B1 U-f1: daily only, what the capture asks for) ----------------------------
    def _generated_bars(self, lookback_days: int) -> list[FakeBarData]:
        """Weekday sessions in the ``lookback_days`` calendar days ending at the sim date.

        A pure function of the sim clock: no wall clock, no randomness. The sim date is read in
        :data:`EXCHANGE_TIME_ZONE`. Its bar exists only on a weekday from the open, carries a
        quarter of the day's volume while the session is in progress (before the close) and the
        full volume after it: ASSUMED-IB.
        """

        now = self.sim_now().astimezone(EXCHANGE_TIME_ZONE)
        today = now.date()
        clock = (now.hour, now.minute)
        has_todays_bar = clock >= SESSION_OPEN  # weekends are excluded for every day below
        in_progress = SESSION_OPEN <= clock < SESSION_CLOSE
        start = today - timedelta(days=lookback_days - 1)
        sessions = [
            start + timedelta(days=offset)
            for offset in range(lookback_days)
            if (start + timedelta(days=offset)).weekday() < 5
            and ((start + timedelta(days=offset)) != today or has_todays_bar)
        ]
        bars: list[FakeBarData] = []
        for index, session in enumerate(sessions):
            base = round(100.0 + 0.5 * (index % 7) + 0.05 * index, 2)
            volume = 1_000_000.0 + 1000.0 * index
            bars.append(
                FakeBarData(
                    session.strftime("%Y%m%d"),
                    open_=base,
                    high=round(base + 1.0, 2),
                    low=round(base - 1.0, 2),
                    close=round(base + 0.25, 2),
                    volume=volume / 4 if (session == today and in_progress) else volume,
                )
            )
        return bars

    def historical_data(
        self,
        client: FakeEClient,
        req_id: int,
        contract: FakeContract,
        duration: str,
        bar_size: str,
        format_date: int,
    ) -> None:
        count, _, unit = duration.partition(" ")
        if unit != "D" or not count.isdigit() or bar_size != "1 day" or format_date != 1:
            raise AssertionError(
                f"historical request ({duration!r}, {bar_size!r}, formatDate {format_date}) is "
                "not modelled: the fake serves daily bars over 'N D' with formatDate 1 only"
            )
        with self._lock:
            con_id = int(contract.conId)
            answer = self.historical.get(con_id)
            if answer == "hang":
                return
            if isinstance(answer, int) and answer >= 100:
                self._error(client, req_id, answer, "Historical Market Data Service error message")
                return
            bars = (
                self.bars_override[con_id]
                if con_id in self.bars_override  # an empty list is "the venue returned no bars"
                else self._generated_bars(int(count))
            )
            for bar in bars:
                client.emit("historicalData", req_id, bar)
            first = bars[0].date if bars else ""
            last = bars[-1].date if bars else ""
            client.emit("historicalDataEnd", req_id, f"{first}  00:00:00", f"{last}  23:59:59")

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


def _capture_day_options() -> list[OptionContract]:
    """The capture day's AAPL chain: (key, expiration, strike, right), OSI local symbols."""

    nov, dec = date(2026, 11, 20), date(2026, 12, 18)
    layout = (
        ("PUT_100_NOV", nov, "100", OptionRight.PUT),
        ("PUT_95_NOV", nov, "95", OptionRight.PUT),
        ("PUT_90_NOV", nov, "90", OptionRight.PUT),
        ("CALL_100_NOV", nov, "100", OptionRight.CALL),
        ("PUT_95_DEC", dec, "95", OptionRight.PUT),
    )
    options: list[OptionContract] = []
    for key, expiration, strike, right in layout:
        osi = f"{'AAPL':<6}{expiration:%y%m%d}{right.value}{int(Decimal(strike) * 1000):08d}"
        options.append(
            OptionContract(
                symbol="AAPL",
                underlying_con_id=999,
                expiration=expiration,
                strike=Decimal(strike),
                right=right,
                multiplier=Decimal("100"),
                trading_class="AAPL",
                con_id=CAPTURE_OPTION_CON_IDS[key],
                local_symbol=osi,
                deliverable_shares=Decimal("100"),
                deliverable_verified=True,
            )
        )
    return options


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
