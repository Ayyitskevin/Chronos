"""Production IBKR adapter on the official TWS API (read-only at M2).

Design (docs/LIVE_WHEEL_GAME_PLAN.md, Milestone 2):

- The official ``ibapi`` package ships with the TWS API distribution and is
  **never** a pip dependency; it is imported lazily so this module, demo
  mode, tests, and CI all work without it (docs/ibkr_setup.md covers the
  owner install).
- The callback thread never touches domain logic: a thin ``_App`` subclass
  forwards primitive callback payloads into the import-safe
  :class:`~chronos.broker.callbacks.CallbackBridge`; async methods await the
  correlated results through the request registry.
- Read paths only. Order methods raise :class:`BrokerSafetyError` until the
  Milestone 5-7 order service exists — the live path arrives there, by
  design, not here.
- Fail-closed normalization: unsupported security types, unparseable
  payloads, or missing account identity raise instead of guessing.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from chronos.broker.base import (
    BrokerConnectionError,
    BrokerDataError,
    BrokerError,
    BrokerSafetyError,
)
from chronos.broker.callbacks import CallbackBridge, QuoteState
from chronos.broker.order_ids import OrderIdAllocator
from chronos.broker.request_registry import RequestRegistry
from chronos.config.settings import Settings
from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    DisplayEnvironment,
    IBEnvironment,
    OptionRight,
    OrderSide,
)
from chronos.domain.models import (
    AccountSummary,
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    CancellationResult,
    ConnectionStatus,
    Instrument,
    MarketQuote,
    ModelGreeks,
    OptionChainParameters,
    OptionContract,
    OptionContractSpec,
    OrderModification,
    OrderPreview,
    OrderRequest,
    OrderSubmission,
    UnderlyingContract,
)

_CONNECT_TIMEOUT_S = 15.0
_REQUEST_TIMEOUT_S = 20.0
_QUOTE_TIMEOUT_S = 15.0

_PAPER_PORTS = frozenset({7497, 4002})
_LIVE_PORTS = frozenset({7496, 4001})

_INSTALL_GUIDANCE = (
    "The official IBKR TWS API package (ibapi) is not installed. It is not on "
    "PyPI: download the TWS API from interactivebrokers.github.io, then install "
    "the bundled Python client (see docs/ibkr_setup.md). Demo mode runs without it."
)

_ORDER_PATH_GUIDANCE = (
    "Order transmission through the official adapter arrives with the Milestone 5-7 "
    "order service and live gate stack (docs/LIVE_WHEEL_GAME_PLAN.md); the adapter "
    "is read-only until then."
)


def _load_ibapi() -> tuple[Any, Any, Any, Any]:
    try:
        from ibapi.client import EClient
        from ibapi.contract import Contract
        from ibapi.execution import ExecutionFilter
        from ibapi.wrapper import EWrapper
    except ImportError as error:
        raise BrokerError(_INSTALL_GUIDANCE) from error
    return EClient, EWrapper, Contract, ExecutionFilter


def _make_app(bridge: CallbackBridge) -> Any:
    """Build the EWrapper/EClient subclass that forwards into the bridge.

    Signatures use ``*args`` tolerantly: TWS API minor versions append
    parameters (e.g. ``error`` gained ``advancedOrderRejectJson``), and the
    bridge only needs the stable leading arguments.
    """

    EClient, EWrapper, _, _ = _load_ibapi()

    class _App(EWrapper, EClient):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            EClient.__init__(self, self)

        def error(self, *args: Any) -> None:
            # (reqId, errorCode, errorString[, advancedOrderRejectJson])
            # Older/newer builds may prepend a timestamp; find the int pair.
            values = list(args)
            if values and not isinstance(values[0], int):
                values = values[1:]
            if len(values) >= 3:
                bridge.on_error(int(values[0]), int(values[1]), str(values[2]))

        def connectionClosed(self) -> None:
            bridge.on_connection_closed()

        def nextValidId(self, orderId: int) -> None:
            bridge.on_next_valid_id(int(orderId))

        def managedAccounts(self, accountsList: str) -> None:
            bridge.on_managed_accounts(str(accountsList))

        def currentTime(self, time: int) -> None:
            bridge.on_current_time(int(time))

        def accountSummary(
            self, reqId: int, account: str, tag: str, value: str, currency: str
        ) -> None:
            bridge.on_account_summary(int(reqId), account, tag, value, currency)

        def accountSummaryEnd(self, reqId: int) -> None:
            bridge.on_account_summary_end(int(reqId))

        def position(self, account: str, contract: Any, position: Any, avgCost: float) -> None:
            bridge.on_position(account, contract, float(position), float(avgCost))

        def positionEnd(self) -> None:
            bridge.on_position_end()

        def contractDetails(self, reqId: int, contractDetails: Any) -> None:
            bridge.on_contract_details(int(reqId), contractDetails)

        def contractDetailsEnd(self, reqId: int) -> None:
            bridge.on_contract_details_end(int(reqId))

        def securityDefinitionOptionParameter(
            self,
            reqId: int,
            exchange: str,
            underlyingConId: int,
            tradingClass: str,
            multiplier: str,
            expirations: Any,
            strikes: Any,
        ) -> None:
            bridge.on_sec_def_opt_params(
                int(reqId),
                exchange,
                int(underlyingConId),
                tradingClass,
                multiplier,
                expirations,
                strikes,
            )

        def securityDefinitionOptionParameterEnd(self, reqId: int) -> None:
            bridge.on_sec_def_opt_params_end(int(reqId))

        def execDetails(self, reqId: int, contract: Any, execution: Any) -> None:
            bridge.on_exec_details(int(reqId), contract, execution)

        def execDetailsEnd(self, reqId: int) -> None:
            bridge.on_exec_details_end(int(reqId))

        def commissionReport(self, commissionReport: Any) -> None:
            bridge.on_commission_report(commissionReport)

        def openOrder(self, orderId: int, contract: Any, order: Any, orderState: Any) -> None:
            bridge.on_open_order(int(orderId), contract, order, orderState)

        def openOrderEnd(self) -> None:
            bridge.on_open_order_end()

        def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any) -> None:
            bridge.on_tick_price(int(reqId), int(tickType), float(price))

        def tickSize(self, reqId: int, tickType: int, size: Any) -> None:
            bridge.on_tick_size(int(reqId), int(tickType), float(size))

        def tickOptionComputation(self, reqId: int, tickType: int, *args: Any) -> None:
            # (tickAttrib,) impliedVol, delta, optPrice, pvDividend, gamma,
            # vega, theta, undPrice — tickAttrib present on newer builds.
            values = list(args)
            if values and isinstance(values[0], int):
                values = values[1:]
            implied_vol = float(values[0]) if len(values) > 0 and values[0] is not None else None
            delta = float(values[1]) if len(values) > 1 and values[1] is not None else None
            gamma = float(values[4]) if len(values) > 4 and values[4] is not None else None
            theta = float(values[6]) if len(values) > 6 and values[6] is not None else None
            bridge.on_tick_option(int(reqId), int(tickType), implied_vol, delta, gamma, theta)

        def tickSnapshotEnd(self, reqId: int) -> None:
            bridge.on_tick_snapshot_end(int(reqId))

        def marketDataType(self, reqId: int, marketDataType: int) -> None:
            bridge.on_market_data_type(int(reqId), int(marketDataType))

        def marketRule(self, marketRuleId: int, priceIncrements: Any) -> None:
            bridge.on_market_rule(int(marketRuleId), priceIncrements)

    return _App()


# --------------------------------------------------------------------------- #
# Pure normalizers (unit-tested without ibapi)
# --------------------------------------------------------------------------- #


def account_summary_from_rows(
    rows: Sequence[tuple[str, str, str, str]],
    *,
    expected_account: str,
    as_of: datetime,
) -> AccountSummary:
    values: dict[str, Decimal] = {}
    currency = "USD"
    for account, tag, value, row_currency in rows:
        if account != expected_account:
            raise BrokerDataError(
                f"account summary row for unexpected account (expected {expected_account!r})"
            )
        try:
            values[tag] = Decimal(value)
        except ArithmeticError as error:  # pragma: no cover - Decimal() raises InvalidOperation
            raise BrokerDataError(f"unparseable account summary value for {tag}") from error
        except Exception as error:
            raise BrokerDataError(f"unparseable account summary value for {tag}") from error
        if row_currency:
            currency = row_currency
    required = {"NetLiquidation", "TotalCashValue", "BuyingPower"}
    missing = required - set(values)
    if missing:
        raise BrokerDataError(f"account summary missing tags: {sorted(missing)}")
    return AccountSummary(
        account_id=expected_account,
        net_liquidation=values["NetLiquidation"],
        total_cash=values["TotalCashValue"],
        buying_power=values["BuyingPower"],
        currency=currency,
        as_of=as_of,
    )


def instrument_from_contract(contract: Any) -> Instrument:
    sec_type = str(getattr(contract, "secType", "") or "")
    symbol = str(getattr(contract, "symbol", "") or "")
    con_id = int(getattr(contract, "conId", 0) or 0)
    currency = str(getattr(contract, "currency", "") or "USD")
    if con_id <= 0 or not symbol:
        raise BrokerDataError(f"contract missing identity (conId={con_id}, symbol={symbol!r})")
    if sec_type == "STK":
        exchange = str(getattr(contract, "exchange", "") or "SMART")
        primary = str(getattr(contract, "primaryExchange", "") or "") or None
        return UnderlyingContract(
            con_id=con_id,
            symbol=symbol,
            exchange=exchange,
            primary_exchange=primary,
            currency=currency,
        )
    if sec_type == "OPT":
        raw_expiry = str(getattr(contract, "lastTradeDateOrContractMonth", "") or "")
        expiration = _parse_yyyymmdd(raw_expiry)
        right_raw = str(getattr(contract, "right", "") or "").upper()
        if right_raw.startswith("C"):
            right = OptionRight.CALL
        elif right_raw.startswith("P"):
            right = OptionRight.PUT
        else:
            raise BrokerDataError(f"unparseable option right {right_raw!r}")
        strike = Decimal(str(getattr(contract, "strike", 0) or 0))
        multiplier_raw = str(getattr(contract, "multiplier", "") or "100")
        local_symbol = str(getattr(contract, "localSymbol", "") or "")
        trading_class = str(getattr(contract, "tradingClass", "") or symbol)
        return OptionContract(
            con_id=con_id,
            symbol=symbol,
            expiration=expiration,
            strike=strike,
            right=right,
            multiplier=Decimal(multiplier_raw),
            trading_class=trading_class,
            local_symbol=local_symbol or f"{symbol}-{raw_expiry}",
            currency=currency,
        )
    raise BrokerDataError(
        f"unsupported security type {sec_type!r} for {symbol!r}; Chronos M2 normalizes "
        "STK and OPT only (crypto arrives with Milestone 7C's domain model)"
    )


def position_from_row(
    row: tuple[str, Any, float, float], *, expected_account: str
) -> BrokerPosition:
    account, contract, quantity, avg_cost = row
    if account != expected_account:
        raise BrokerDataError(
            f"position row for unexpected account (expected {expected_account!r})"
        )
    return BrokerPosition(
        account_id=account,
        contract=instrument_from_contract(contract),
        quantity=Decimal(str(quantity)),
        average_cost=Decimal(str(avg_cost)),
    )


def execution_from_pair(
    contract: Any,
    execution: Any,
    *,
    commission: Decimal | None,
    commission_currency: str | None,
) -> BrokerExecution:
    side_raw = str(getattr(execution, "side", "") or "").upper()
    if side_raw in ("BOT", "BUY"):
        side = OrderSide.BUY
    elif side_raw in ("SLD", "SELL"):
        side = OrderSide.SELL
    else:
        raise BrokerDataError(f"unparseable execution side {side_raw!r}")
    raw_time = str(getattr(execution, "time", "") or "")
    timestamp = _parse_execution_time(raw_time)
    return BrokerExecution(
        execution_id=str(getattr(execution, "execId", "") or ""),
        account_id=str(getattr(execution, "acctNumber", "") or ""),
        broker_order_id=int(getattr(execution, "orderId", 0) or 0),
        permanent_id=int(getattr(execution, "permId", 0) or 0) or None,
        client_id=int(getattr(execution, "clientId", 0) or 0),
        order_ref=str(getattr(execution, "orderRef", "") or "") or None,
        contract=instrument_from_contract(contract),
        side=side,
        quantity=Decimal(str(getattr(execution, "shares", 0) or 0)),
        price=Decimal(str(getattr(execution, "price", 0) or 0)),
        timestamp=timestamp,
        commission=commission,
        commission_currency=commission_currency if commission is not None else None,
    )


def _parse_yyyymmdd(raw: str) -> date:
    text = raw.strip()[:8]
    if len(text) != 8 or not text.isdigit():
        raise BrokerDataError(f"unparseable TWS date {raw!r}")
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError as error:
        raise BrokerDataError(f"unparseable TWS date {raw!r}") from error


def _parse_execution_time(raw: str) -> datetime:
    # TWS format: "20260717 14:30:00" optionally followed by a timezone name.
    parts = raw.strip().split()
    if len(parts) >= 2:
        day = _parse_yyyymmdd(parts[0])
        clock = parts[1].split(":")
        if len(clock) != 3 or not all(piece.isdigit() for piece in clock):
            raise BrokerDataError(f"unparseable execution time {raw!r}")
        tz: Any = UTC
        if len(parts) >= 3:
            try:
                from zoneinfo import ZoneInfo

                tz = ZoneInfo(parts[2])
            except Exception as error:
                raise BrokerDataError(f"unknown execution timezone in {raw!r}") from error
        try:
            return datetime(
                day.year,
                day.month,
                day.day,
                int(clock[0]),
                int(clock[1]),
                int(clock[2]),
                tzinfo=tz,
            )
        except ValueError as error:
            raise BrokerDataError(f"unparseable execution time {raw!r}") from error
    raise BrokerDataError(f"unparseable execution time {raw!r}")


def quote_from_state(
    state: QuoteState, *, contract: Instrument, timestamp: datetime
) -> MarketQuote:
    greeks = None
    if any(
        value is not None
        for value in (state.delta, state.gamma, state.theta, state.implied_volatility)
    ):
        greeks = ModelGreeks(
            delta=state.delta,
            gamma=state.gamma,
            theta=state.theta,
            implied_volatility=state.implied_volatility,
        )
    return MarketQuote(
        contract=contract,
        timestamp=timestamp,
        data_quality=state.data_quality,
        bid=state.bid,
        ask=state.ask,
        last=state.last,
        close=state.close,
        volume=state.volume,
        open_interest=state.open_interest,
        greeks=greeks,
    )


def chain_parameters_from_row(
    row: tuple[str, int, str, str, Any, Any],
) -> OptionChainParameters:
    exchange, underlying_con_id, trading_class, multiplier, expirations, strikes = row
    parsed_expirations = [_parse_yyyymmdd(str(raw)) for raw in sorted(set(expirations or ()))]
    parsed_strikes = tuple(
        Decimal(str(strike)) for strike in sorted(set(strikes or ())) if float(strike) > 0
    )
    return OptionChainParameters(
        exchange=exchange,
        underlying_con_id=underlying_con_id,
        trading_class=trading_class,
        multiplier=Decimal(str(multiplier)),
        expirations=tuple(parsed_expirations),
        strikes=parsed_strikes,
    )


def verify_environment_port(environment: IBEnvironment, port: int) -> None:
    """Refuse an environment/port mismatch (paper config on a live port etc.)."""

    if environment is IBEnvironment.PAPER and port not in _PAPER_PORTS:
        raise BrokerSafetyError(
            f"IB_ENVIRONMENT=paper requires a paper port {sorted(_PAPER_PORTS)}, got {port}"
        )
    if environment is IBEnvironment.LIVE and port not in _LIVE_PORTS:
        raise BrokerSafetyError(
            f"IB_ENVIRONMENT=live requires a live port {sorted(_LIVE_PORTS)}, got {port}"
        )


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #


class OfficialIBKRBroker:
    """Read-only production adapter over the official TWS API."""

    def __init__(self, settings: Settings) -> None:
        _load_ibapi()  # fail fast with install guidance if the package is absent
        if not settings.ib_account_id.strip():
            raise BrokerSafetyError(
                "IB_ACCOUNT_ID is required for the official IBKR adapter; refusing to "
                "connect without an expected account to verify against"
            )
        if (
            settings.ib_account_allowlist
            and settings.ib_account_id not in settings.ib_account_allowlist
        ):
            raise BrokerSafetyError(
                "IB_ACCOUNT_ID is not in IB_ACCOUNT_ALLOWLIST; refusing to connect"
            )
        verify_environment_port(settings.ib_environment, settings.ib_port)
        self._settings = settings
        self.registry = RequestRegistry()
        self.bridge = CallbackBridge(self.registry)
        self.order_ids = OrderIdAllocator()
        self._app: Any = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        if self._app is not None:
            return
        app = _make_app(self.bridge)
        app.connect(self._settings.ib_host, self._settings.ib_port, self._settings.ib_client_id)
        thread = threading.Thread(target=app.run, name="chronos-ibapi-reader", daemon=True)
        thread.start()
        self._app = app
        self._thread = thread
        import asyncio

        loop = asyncio.get_running_loop()
        connected = await loop.run_in_executor(
            None, self.bridge.connected_event.wait, _CONNECT_TIMEOUT_S
        )
        accounts_seen = await loop.run_in_executor(
            None, self.bridge.managed_accounts_event.wait, _CONNECT_TIMEOUT_S
        )
        if not connected or not accounts_seen:
            await self.disconnect()
            raise BrokerConnectionError(
                "TWS/Gateway did not complete the connection handshake "
                f"within {_CONNECT_TIMEOUT_S:.0f}s (is the API enabled on "
                f"{self._settings.ib_host}:{self._settings.ib_port}?)"
            )
        if self.bridge.next_valid_id is not None:
            self.order_ids.seed(self.bridge.next_valid_id)
        expected = self._settings.ib_account_id
        if expected not in self.bridge.managed_accounts:
            await self.disconnect()
            raise BrokerSafetyError(
                "connected session does not manage the configured account; refusing "
                f"(managed count={len(self.bridge.managed_accounts)})"
            )
        # Request live-quality data by default; per-request callbacks classify.
        app.reqMarketDataType(1)

    async def disconnect(self) -> None:
        app, self._app = self._app, None
        thread, self._thread = self._thread, None
        if app is not None:
            with contextlib.suppress(Exception):
                app.disconnect()
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    def _require_app(self) -> Any:
        if self._app is None or self.bridge.connection_closed.is_set():
            raise BrokerConnectionError("official IBKR adapter is not connected")
        return self._app

    async def connection_status(self) -> ConnectionStatus:
        connected = self._app is not None and not self.bridge.connection_closed.is_set()
        environment = (
            DisplayEnvironment.LIVE
            if self._settings.ib_environment is IBEnvironment.LIVE
            else DisplayEnvironment.PAPER
        )
        return ConnectionStatus(
            state=ConnectionState.CONNECTED if connected else ConnectionState.DISCONNECTED,
            environment=environment,
            connected=connected,
            account_id=self._settings.ib_account_id if connected else None,
            data_quality=DataQuality.UNKNOWN,
            message="official TWS API adapter (read-only until Milestone 5)",
        )

    # ------------------------------------------------------------------ #
    # Read paths
    # ------------------------------------------------------------------ #

    async def server_time(self) -> datetime:
        app = self._require_app()
        flight = self.bridge.start_current_time()
        app.reqCurrentTime()
        import asyncio

        loop = asyncio.get_running_loop()
        completed = await loop.run_in_executor(None, flight.done.wait, _REQUEST_TIMEOUT_S)
        if not completed or not flight.items:
            raise BrokerDataError("currentTime request timed out")
        return datetime.fromtimestamp(int(flight.items[0]), tz=UTC)

    async def account_summary(self) -> AccountSummary:
        app = self._require_app()
        request_id = self.registry.open()
        app.reqAccountSummary(request_id, "All", "NetLiquidation,TotalCashValue,BuyingPower")
        try:
            rows = await self.registry.wait(request_id, timeout=_REQUEST_TIMEOUT_S)
        finally:
            app.cancelAccountSummary(request_id)
        expected = self._settings.ib_account_id
        relevant = [row for row in rows if row[0] == expected]
        if not relevant:
            raise BrokerDataError("account summary returned no rows for the configured account")
        return account_summary_from_rows(
            [(expected, tag, value, currency) for _, tag, value, currency in relevant],
            expected_account=expected,
            as_of=datetime.now(tz=UTC),
        )

    async def positions(self) -> tuple[BrokerPosition, ...]:
        app = self._require_app()
        flight = self.bridge.start_positions()
        app.reqPositions()
        import asyncio

        loop = asyncio.get_running_loop()
        completed = await loop.run_in_executor(None, flight.done.wait, _REQUEST_TIMEOUT_S)
        app.cancelPositions()
        if not completed:
            raise BrokerDataError("positions request timed out")
        expected = self._settings.ib_account_id
        rows = [row for row in flight.items if row[0] == expected]
        normalized = tuple(
            position_from_row(row, expected_account=expected)
            for row in rows
            if float(row[2]) != 0.0
        )
        return normalized

    async def executions(self, since: datetime | None = None) -> tuple[BrokerExecution, ...]:
        app = self._require_app()
        _, _, _, ExecutionFilter = _load_ibapi()
        request_id = self.registry.open()
        exec_filter = ExecutionFilter()
        exec_filter.acctCode = self._settings.ib_account_id
        if since is not None:
            exec_filter.time = since.astimezone(UTC).strftime("%Y%m%d-%H:%M:%S")
        app.reqExecutions(request_id, exec_filter)
        pairs = await self.registry.wait(request_id, timeout=_REQUEST_TIMEOUT_S)
        results = []
        for contract, execution in pairs:
            exec_id = str(getattr(execution, "execId", "") or "")
            commission, currency = self.bridge.commission_for(exec_id)
            results.append(
                execution_from_pair(
                    contract,
                    execution,
                    commission=commission,
                    commission_currency=currency,
                )
            )
        return tuple(results)

    async def open_orders(self) -> tuple[BrokerOrder, ...]:
        # Deferred to Milestone 5 alongside the order tracker: normalizing
        # openOrder/orderStatus pairs needs the lifecycle model that milestone
        # defines. Reads used by M2 reconciliation come from positions and
        # executions; pretending to normalize orders without the lifecycle
        # semantics would be guesswork, so this fails closed until then.
        raise BrokerDataError(
            "open_orders is implemented in Milestone 5 with the order tracker; "
            "the official adapter refuses to guess order lifecycles until then"
        )

    async def qualify_underlying(self, symbol: str) -> UnderlyingContract:
        app = self._require_app()
        _, _, Contract, _ = _load_ibapi()
        contract = Contract()
        contract.symbol = symbol.strip().upper()
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        request_id = self.registry.open()
        app.reqContractDetails(request_id, contract)
        details = await self.registry.wait(request_id, timeout=_REQUEST_TIMEOUT_S)
        if not details:
            raise BrokerDataError(f"no contract details for underlying {symbol!r}")
        first = details[0]
        instrument = instrument_from_contract(getattr(first, "contract", first))
        if not isinstance(instrument, UnderlyingContract):
            raise BrokerDataError(f"underlying qualification returned a non-stock for {symbol!r}")
        return instrument

    async def option_chain_parameters(
        self,
        underlying: UnderlyingContract,
    ) -> tuple[OptionChainParameters, ...]:
        app = self._require_app()
        request_id = self.registry.open()
        app.reqSecDefOptParams(request_id, underlying.symbol, "", "STK", underlying.con_id)
        rows = await self.registry.wait(request_id, timeout=_REQUEST_TIMEOUT_S)
        return tuple(chain_parameters_from_row(row) for row in rows)

    async def qualify_option_contracts(
        self,
        contracts: Sequence[OptionContractSpec],
    ) -> tuple[OptionContract, ...]:
        app = self._require_app()
        _, _, Contract, _ = _load_ibapi()
        qualified: list[OptionContract] = []
        for spec in contracts:
            contract = Contract()
            contract.symbol = spec.symbol
            contract.secType = "OPT"
            contract.exchange = spec.exchange
            contract.currency = spec.currency
            contract.lastTradeDateOrContractMonth = spec.expiration.strftime("%Y%m%d")
            contract.strike = float(spec.strike)
            contract.right = spec.right.value[0]
            contract.multiplier = str(spec.multiplier)
            contract.tradingClass = spec.trading_class
            request_id = self.registry.open()
            app.reqContractDetails(request_id, contract)
            details = await self.registry.wait(request_id, timeout=_REQUEST_TIMEOUT_S)
            for detail in details:
                instrument = instrument_from_contract(getattr(detail, "contract", detail))
                if isinstance(instrument, OptionContract):
                    min_tick_raw = getattr(detail, "minTick", None)
                    if min_tick_raw:
                        instrument = instrument.model_copy(
                            update={"min_tick": Decimal(str(min_tick_raw))}
                        )
                    qualified.append(instrument)
        return tuple(qualified)

    async def request_underlying_quote(self, contract: UnderlyingContract) -> MarketQuote:
        return await self._snapshot_quote(contract, con_id=contract.con_id, generic_ticks="")

    async def request_option_quotes(
        self,
        contracts: Sequence[OptionContract],
    ) -> tuple[MarketQuote, ...]:
        quotes = []
        for option in contracts:
            quotes.append(
                await self._snapshot_quote(option, con_id=option.con_id, generic_ticks="")
            )
        return tuple(quotes)

    async def _snapshot_quote(
        self, instrument: Instrument, *, con_id: int, generic_ticks: str
    ) -> MarketQuote:
        app = self._require_app()
        _, _, Contract, _ = _load_ibapi()
        contract = Contract()
        contract.conId = con_id
        contract.exchange = "SMART"
        request_id = self.registry.open()
        self.bridge.open_quote(request_id)
        app.reqMktData(request_id, contract, generic_ticks, True, False, [])
        try:
            await self.registry.wait(request_id, timeout=_QUOTE_TIMEOUT_S)
        finally:
            state = self.bridge.close_quote(request_id)
        if state is None:
            raise BrokerDataError(f"no quote state for conId {con_id}")
        return quote_from_state(state, contract=instrument, timestamp=datetime.now(tz=UTC))

    async def cancel_market_data(self, contract_ids: Sequence[int]) -> None:
        # Snapshot requests self-terminate; nothing persistent to cancel.
        del contract_ids

    # ------------------------------------------------------------------ #
    # Order surface: refused until the Milestone 5-7 order service exists
    # ------------------------------------------------------------------ #

    async def preview_order(self, request: OrderRequest) -> OrderPreview:
        raise BrokerSafetyError(_ORDER_PATH_GUIDANCE)

    async def submit_order(self, request: OrderRequest) -> OrderSubmission:
        raise BrokerSafetyError(_ORDER_PATH_GUIDANCE)

    async def modify_order(self, request: OrderModification) -> OrderSubmission:
        raise BrokerSafetyError(_ORDER_PATH_GUIDANCE)

    async def cancel_order(self, broker_order_id: int) -> CancellationResult:
        raise BrokerSafetyError(_ORDER_PATH_GUIDANCE)
