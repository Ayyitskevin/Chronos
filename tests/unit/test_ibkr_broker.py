from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from ib_async import (
    AccountValue,
    CommissionReport,
    Contract,
    ContractDetails,
    Execution,
    ExecutionFilter,
    Fill,
    OptionChain,
    OptionComputation,
    Order,
    OrderStatus,
    Position,
    Stock,
    TickData,
    Ticker,
    Trade,
)
from ib_async import (
    Option as IBOption,
)
from tests.conftest import FIXED_NOW

from chronos.broker.base import (
    Broker,
    BrokerConnectionError,
    BrokerDataError,
    BrokerSafetyError,
)
from chronos.broker.ibkr import IBKRBroker
from chronos.broker.market_data import (
    MarketDataManager,
    MarketDataPermissionError,
    MarketDataUnavailableError,
)
from chronos.config.settings import Settings
from chronos.domain.enums import (
    BrokerMode,
    ConnectionState,
    DataQuality,
    OptionRight,
    OrderIntent,
    OrderLifecycle,
    OrderSide,
)
from chronos.domain.models import OptionContract, OptionContractSpec, OrderRequest

ACCOUNT_ID = "DU1234567"
IBErrorHandler = Callable[[int, int, str, Contract | None], None]


class FakeErrorEvent:
    def __init__(self) -> None:
        self.handlers: list[IBErrorHandler] = []

    def connect(self, handler: IBErrorHandler) -> object:
        self.handlers.append(handler)
        return handler

    def emit(
        self,
        request_id: int,
        error_code: int,
        error_message: str,
        contract: Contract | None,
    ) -> None:
        for handler in tuple(self.handlers):
            handler(request_id, error_code, error_message, contract)


class FakeLifecycleEvent:
    def __init__(self) -> None:
        self.handlers: list[Callable[..., object]] = []

    def connect(self, handler: Callable[..., object]) -> object:
        self.handlers.append(handler)
        return handler

    def emit(self) -> None:
        for handler in tuple(self.handlers):
            handler()


def ib_stock() -> Stock:
    return Stock(
        symbol="AAPL",
        exchange="SMART",
        currency="USD",
        conId=101,
        primaryExchange="NASDAQ",
    )


def ib_option() -> IBOption:
    return IBOption(
        symbol="AAPL",
        lastTradeDateOrContractMonth="20260821",
        strike=185.0,
        right="P",
        exchange="SMART",
        multiplier="100",
        currency="USD",
        conId=202,
        localSymbol="AAPL  260821P00185000",
        tradingClass="AAPL",
    )


def domain_option() -> OptionContract:
    return OptionContract(
        con_id=202,
        symbol="AAPL",
        underlying_con_id=101,
        expiration=date(2026, 8, 21),
        strike=Decimal("185"),
        right=OptionRight.PUT,
        exchange="SMART",
        currency="USD",
        multiplier=Decimal("100"),
        trading_class="AAPL",
        local_symbol="AAPL  260821P00185000",
        min_tick=Decimal("0.05"),
    )


class FakeIB:
    def __init__(self) -> None:
        self.errorEvent = FakeErrorEvent()
        self.connectedEvent = FakeLifecycleEvent()
        self.disconnectedEvent = FakeLifecycleEvent()
        self.connected = False
        self.accounts = [ACCOUNT_ID]
        self.connect_kwargs: dict[str, object] = {}
        self.disconnect_calls = 0
        self.open_order_requests = 0
        self.position_account = ""
        self.execution_filter: ExecutionFilter | None = None
        self.min_tick = 0.05
        self.stock_currency = "USD"
        self.stock_exchange = "SMART"
        self.option_underlying_con_id = 101
        self.option_currency = "USD"
        self.option_exchange = "SMART"
        # A real gateway returns these on every option ContractDetails; the
        # deliverable screen reads them (M11, R-27).
        self.option_underlying_symbol = "AAPL"
        self.option_underlying_sec_type = "STK"
        self.account_values = [
            AccountValue(ACCOUNT_ID, "NetLiquidation", "250000.25", "USD", ""),
            AccountValue(ACCOUNT_ID, "TotalCashValue", "125000.50", "USD", ""),
            AccountValue(ACCOUNT_ID, "BuyingPower", "240000.75", "USD", ""),
        ]
        self.position_rows: list[Position] = []
        self.fill_rows: list[Fill] = []
        self.trade_rows: list[Trade] = []
        self.tickers: dict[int, Ticker] = {}
        self.live_tickers: dict[int, Ticker] = {}
        self.market_data_requests: list[tuple[int, str, bool, bool]] = []
        self.cancelled_contracts: list[int] = []
        self.cancel_succeeds = True
        self.market_data_errors: list[int | None] = []
        self.publish_market_data_updates = True

    async def connectAsync(
        self,
        host: str,
        port: int,
        clientId: int,
        timeout: float | None,
        readonly: bool,
        account: str,
        raiseSyncErrors: bool,
    ) -> object:
        self.connect_kwargs = {
            "host": host,
            "port": port,
            "clientId": clientId,
            "timeout": timeout,
            "readonly": readonly,
            "account": account,
            "raiseSyncErrors": raiseSyncErrors,
        }
        self.connected = True
        return self

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    def isConnected(self) -> bool:
        return self.connected

    def managedAccounts(self) -> list[str]:
        return self.accounts

    def positions(self, account: str = "") -> list[Position]:
        self.position_account = account
        return self.position_rows

    async def accountSummaryAsync(self, account: str = "") -> list[AccountValue]:
        return [value for value in self.account_values if not account or value.account == account]

    async def reqCurrentTimeAsync(self) -> datetime:
        return FIXED_NOW

    async def reqExecutionsAsync(
        self,
        execFilter: ExecutionFilter | None = None,
    ) -> list[Fill]:
        self.execution_filter = execFilter
        return self.fill_rows

    async def reqAllOpenOrdersAsync(self) -> list[Trade]:
        self.open_order_requests += 1
        return self.trade_rows

    async def qualifyContractsAsync(
        self,
        *contracts: Contract,
        returnAll: bool = False,
    ) -> list[Contract | list[Contract | None] | None]:
        del returnAll
        qualified: list[Contract | list[Contract | None] | None] = []
        for contract in contracts:
            if contract.secType == "STK":
                stock = ib_stock()
                stock.currency = self.stock_currency
                stock.exchange = self.stock_exchange
                qualified.append(stock)
            elif contract.secType == "OPT":
                option = ib_option()
                option.currency = self.option_currency
                option.exchange = self.option_exchange
                qualified.append(option)
            else:
                qualified.append(None)
        return qualified

    async def reqContractDetailsAsync(self, contract: Contract) -> list[ContractDetails]:
        return [
            ContractDetails(
                contract=contract,
                minTick=self.min_tick,
                underConId=self.option_underlying_con_id,
                underSymbol=self.option_underlying_symbol,
                underSecType=self.option_underlying_sec_type,
            )
        ]

    async def reqSecDefOptParamsAsync(
        self,
        underlyingSymbol: str,
        futFopExchange: str,
        underlyingSecType: str,
        underlyingConId: int,
    ) -> list[OptionChain]:
        assert underlyingSymbol == "AAPL"
        assert futFopExchange == ""
        assert underlyingSecType == "STK"
        return [
            OptionChain(
                exchange="SMART",
                underlyingConId=underlyingConId,
                tradingClass="AAPL",
                multiplier="100",
                expirations=["20260821", "20260814"],
                strikes=[190.0, 185.0],
            )
        ]

    def reqMktData(
        self,
        contract: Contract,
        genericTickList: str = "",
        snapshot: bool = False,
        regulatorySnapshot: bool = False,
    ) -> Ticker:
        self.market_data_requests.append(
            (contract.conId, genericTickList, snapshot, regulatorySnapshot)
        )
        if self.market_data_errors:
            error_code = self.market_data_errors.pop(0)
            if error_code is not None:
                self.errorEvent.emit(1, error_code, "simulated IBKR error", contract)
                return self.live_tickers.setdefault(
                    contract.conId,
                    Ticker(contract=contract),
                )
        template = self.tickers.get(
            contract.conId,
            Ticker(contract=contract, time=FIXED_NOW, marketDataType=1),
        )
        ticker = self.live_tickers.setdefault(contract.conId, Ticker(contract=contract))
        if self.publish_market_data_updates:
            asyncio.get_running_loop().call_soon(self._publish_ticker, ticker, template)
        return ticker

    @staticmethod
    def _publish_ticker(ticker: Ticker, template: Ticker) -> None:
        for field_name in (
            "bid",
            "ask",
            "last",
            "close",
            "volume",
            "openInterest",
            "putVolume",
            "callVolume",
            "putOpenInterest",
            "callOpenInterest",
            "modelGreeks",
        ):
            setattr(ticker, field_name, getattr(template, field_name))
        ticker.time = template.time or FIXED_NOW
        ticker.marketDataType = template.marketDataType
        ticker.ticks.append(TickData(FIXED_NOW, 2, ticker.ask, 1))
        ticker.updateEvent.emit(ticker)

    def cancelMktData(self, contract: Contract) -> bool:
        self.cancelled_contracts.append(contract.conId)
        return self.cancel_succeeds


def make_broker(
    client: FakeIB,
    *,
    account_id: str = ACCOUNT_ID,
    max_quote_age_seconds: int = 5,
    on_connection_uncertain: Callable[[str], object] | None = None,
) -> IBKRBroker:
    settings = Settings(
        broker_mode=BrokerMode.IBKR,
        ib_account_id=account_id,
        max_quote_age_seconds=max_quote_age_seconds,
    )
    return IBKRBroker(
        settings,
        client=client,
        clock=lambda: FIXED_NOW,
        quote_timeout_seconds=0.2,
        quote_settle_seconds=0,
        on_connection_uncertain=on_connection_uncertain,
    )


def test_lifecycle_events_invalidate_reconciliation_readiness() -> None:
    client = FakeIB()
    reasons: list[str] = []
    make_broker(client, on_connection_uncertain=reasons.append)

    client.connectedEvent.emit()
    client.disconnectedEvent.emit()

    assert reasons == [
        "ib_async connection established; reconciliation required",
        "ib_async connection lost; reconciliation required",
    ]


@pytest.mark.parametrize("code", [1100, 1101, 1102, 1300, 2110])
def test_connectivity_codes_invalidate_reconciliation_readiness(code: int) -> None:
    client = FakeIB()
    reasons: list[str] = []
    broker = make_broker(client, on_connection_uncertain=reasons.append)

    broker._on_ib_error(-1, code, "connectivity changed", None)

    assert reasons == [f"ib_async connectivity event {code}"]


@pytest.mark.asyncio
async def test_connect_is_readonly_syncs_all_orders_and_reports_account() -> None:
    client = FakeIB()
    broker = make_broker(client)

    assert isinstance(broker, Broker)
    await broker.connect()
    status = await broker.connection_status()

    assert client.connect_kwargs == {
        "host": "127.0.0.1",
        "port": 7497,
        "clientId": 17,
        "timeout": 10.0,
        "readonly": True,
        "account": ACCOUNT_ID,
        "raiseSyncErrors": True,
    }
    assert client.open_order_requests == 1
    assert status.state is ConnectionState.CONNECTED
    assert status.account_id == ACCOUNT_ID
    assert status.data_quality is DataQuality.UNKNOWN
    assert status.last_successful_sync == FIXED_NOW


@pytest.mark.asyncio
async def test_connect_rejects_configured_account_mismatch() -> None:
    client = FakeIB()
    client.accounts = ["DU7654321"]
    broker = make_broker(client)

    with pytest.raises(BrokerConnectionError, match="not managed"):
        await broker.connect()

    assert client.connected is False
    assert client.disconnect_calls == 1


@pytest.mark.asyncio
async def test_account_summary_requires_every_authoritative_value() -> None:
    client = FakeIB()
    broker = make_broker(client)
    await broker.connect()

    summary = await broker.account_summary()

    assert summary.net_liquidation == Decimal("250000.25")
    assert summary.total_cash == Decimal("125000.50")
    assert summary.buying_power == Decimal("240000.75")
    assert summary.currency == "USD"
    assert summary.as_of == FIXED_NOW

    client.account_values = [value for value in client.account_values if value.tag != "BuyingPower"]
    with pytest.raises(BrokerDataError, match="BuyingPower"):
        await broker.account_summary()


@pytest.mark.asyncio
async def test_positions_preserve_unavailable_portfolio_fields_and_min_tick() -> None:
    client = FakeIB()
    client.position_rows = [
        Position(ACCOUNT_ID, ib_stock(), 100.0, 180.25),
        Position(ACCOUNT_ID, ib_option(), -1.0, 325.0),
    ]
    broker = make_broker(client)
    await broker.connect()

    positions = await broker.positions()

    assert client.position_account == ACCOUNT_ID
    assert positions[0].quantity == Decimal("100.0")
    assert positions[0].market_price is None
    assert positions[0].unrealized_pnl is None
    assert isinstance(positions[1].contract, OptionContract)
    assert positions[1].contract.min_tick == Decimal("0.05")


@pytest.mark.asyncio
async def test_executions_filter_locally_and_do_not_invent_commission() -> None:
    client = FakeIB()
    option = ib_option()
    old_time = FIXED_NOW - timedelta(days=1)
    client.fill_rows = [
        Fill(
            option,
            Execution(
                execId="OLD",
                time=old_time,
                acctNumber=ACCOUNT_ID,
                side="SLD",
                shares=1,
                price=3.0,
                permId=10,
                clientId=17,
                orderId=20,
            ),
            CommissionReport(execId="OLD", commission=0.65),
            old_time,
        ),
        Fill(
            option,
            Execution(
                execId="NEW",
                time=FIXED_NOW,
                acctNumber=ACCOUNT_ID,
                side="BOT",
                shares=1,
                price=2.5,
                permId=11,
                clientId=17,
                orderId=21,
            ),
            CommissionReport(),
            FIXED_NOW,
        ),
    ]
    broker = make_broker(client)
    await broker.connect()

    executions = await broker.executions(since=FIXED_NOW - timedelta(hours=1))

    assert client.execution_filter is not None
    assert client.execution_filter.acctCode == ACCOUNT_ID
    assert len(executions) == 1
    assert executions[0].execution_id == "NEW"
    assert executions[0].side is OrderSide.BUY
    assert executions[0].commission is None


@pytest.mark.asyncio
async def test_open_orders_fetch_all_accounts_and_map_partial_fill() -> None:
    client = FakeIB()
    client.trade_rows = [
        Trade(
            contract=ib_option(),
            order=Order(
                orderId=41,
                clientId=17,
                permId=51,
                action="SELL",
                totalQuantity=2,
                orderType="LMT",
                lmtPrice=3.25,
                orderRef="CHR-OPEN-1",
                transmit=False,
                account=ACCOUNT_ID,
            ),
            orderStatus=OrderStatus(
                orderId=41,
                status="Submitted",
                filled=1,
                remaining=1,
                permId=51,
                clientId=17,
            ),
        ),
        Trade(
            contract=ib_option(),
            order=Order(
                orderId=42,
                clientId=18,
                action="BUY",
                totalQuantity=1,
                orderType="LMT",
                lmtPrice=1.0,
                account="DU7654321",
            ),
            orderStatus=OrderStatus(
                orderId=42,
                status="Submitted",
                filled=0,
                remaining=1,
            ),
        ),
    ]
    broker = make_broker(client)
    await broker.connect()

    orders = await broker.open_orders()

    assert client.open_order_requests == 2
    assert len(orders) == 1
    assert orders[0].broker_order_id == 41
    assert orders[0].lifecycle is OrderLifecycle.PARTIALLY_FILLED
    assert orders[0].filled_quantity == Decimal("1")
    assert orders[0].remaining_quantity == Decimal("1")
    assert orders[0].transmit is False


@pytest.mark.asyncio
async def test_qualification_chain_and_contract_details_are_authoritative() -> None:
    client = FakeIB()
    broker = make_broker(client)
    await broker.connect()

    underlying = await broker.qualify_underlying("aapl")
    chains = await broker.option_chain_parameters(underlying)
    options = await broker.qualify_option_contracts(
        (
            OptionContractSpec(
                symbol="AAPL",
                underlying_con_id=101,
                expiration=date(2026, 8, 21),
                strike=Decimal("185"),
                right=OptionRight.PUT,
                exchange="SMART",
                currency="USD",
                multiplier=Decimal("100"),
                trading_class="AAPL",
            ),
        )
    )

    assert underlying.con_id == 101
    assert chains[0].expirations == (date(2026, 8, 14), date(2026, 8, 21))
    assert chains[0].strikes == (Decimal("185.0"), Decimal("190.0"))
    assert options[0].con_id == 202
    assert options[0].underlying_con_id == underlying.con_id
    assert options[0].min_tick == Decimal("0.05")
    # The assertion that could not have been written before M11. This line read
    # `is False` for six milestones, and it was pinning R-27: neither IBKR
    # adapter set the flag, so `standard_deliverable_verified` FAILed every
    # option order against a real gateway.
    assert options[0].deliverable_verified is True
    assert options[0].deliverable_shares == Decimal("100")
    assert options[0].has_verified_standard_deliverable is True

    client.min_tick = 0
    with pytest.raises(BrokerDataError, match="minimum tick"):
        await broker.qualify_option_contracts(
            (
                OptionContractSpec(
                    symbol="AAPL",
                    expiration=date(2026, 8, 21),
                    strike=Decimal("185"),
                    right=OptionRight.PUT,
                    multiplier=Decimal("100"),
                    trading_class="AAPL",
                ),
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("option_underlying_con_id", 999),
        ("option_currency", "EUR"),
        ("option_exchange", "CBOE"),
    ],
)
async def test_option_qualification_rejects_identity_mismatch(
    attribute: str,
    value: object,
) -> None:
    client = FakeIB()
    setattr(client, attribute, value)
    broker = make_broker(client)
    await broker.connect()

    with pytest.raises(BrokerDataError, match="different from the request"):
        await broker.qualify_option_contracts(
            (
                OptionContractSpec(
                    symbol="AAPL",
                    underlying_con_id=101,
                    expiration=date(2026, 8, 21),
                    strike=Decimal("185"),
                    right=OptionRight.PUT,
                    exchange="SMART",
                    currency="USD",
                    multiplier=Decimal("100"),
                    trading_class="AAPL",
                ),
            )
        )


@pytest.mark.asyncio
async def test_underlying_qualification_enforces_symbol_allowlist() -> None:
    client = FakeIB()
    broker = make_broker(client)
    await broker.connect()

    with pytest.raises(BrokerSafetyError, match="SYMBOL_ALLOWLIST"):
        await broker.qualify_underlying("TSLA")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attribute", "value"),
    [("stock_currency", "EUR"), ("stock_exchange", "EUREX")],
)
async def test_underlying_qualification_rejects_identity_mismatch(
    attribute: str,
    value: str,
) -> None:
    client = FakeIB()
    setattr(client, attribute, value)
    broker = make_broker(client)
    await broker.connect()

    with pytest.raises(BrokerDataError, match="unexpected underlying"):
        await broker.qualify_underlying("AAPL")


@pytest.mark.asyncio
async def test_option_quotes_stream_generic_ticks_preserve_missing_and_cancel() -> None:
    client = FakeIB()
    option = ib_option()
    ticker = Ticker(contract=option, time=FIXED_NOW, marketDataType=1)
    # ib_async intentionally initializes live tick fields to its unset sentinel;
    # assign fake updates after construction to model wrapper-delivered ticks.
    ticker.bid = 3.10
    ticker.ask = 3.25
    ticker.close = 3.0
    ticker.putVolume = 44
    ticker.putOpenInterest = 555
    ticker.modelGreeks = OptionComputation(
        tickAttrib=0,
        impliedVol=0.25,
        delta=-0.30,
        gamma=0.02,
        theta=-0.10,
    )
    client.tickers[option.conId] = ticker
    broker = make_broker(client)
    await broker.connect()

    quotes = await broker.request_option_quotes((domain_option(),))

    assert client.market_data_requests == [(202, "100,101,106", False, False)]
    assert quotes[0].data_quality is DataQuality.LIVE
    assert quotes[0].bid == Decimal("3.1")
    assert quotes[0].last is None
    assert quotes[0].volume == 44
    assert quotes[0].open_interest == 555
    assert quotes[0].greeks is not None
    assert quotes[0].greeks.delta == Decimal("-0.3")
    assert quotes[0].greeks.theta == Decimal("-0.1")

    await broker.cancel_market_data((202,))
    assert client.cancelled_contracts == [202]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market_data_type", "expected"),
    [
        (1, DataQuality.LIVE),
        (2, DataQuality.FROZEN),
        (3, DataQuality.DELAYED),
        (4, DataQuality.DELAYED),
        (99, DataQuality.UNKNOWN),
    ],
)
async def test_quote_data_quality_is_explicit_and_fail_closed(
    market_data_type: int,
    expected: DataQuality,
) -> None:
    client = FakeIB()
    stock = ib_stock()
    client.tickers[stock.conId] = Ticker(
        contract=stock,
        time=FIXED_NOW,
        marketDataType=market_data_type,
        bid=float("nan"),
        ask=-1,
        last=float("nan"),
        close=float("nan"),
        volume=float("nan"),
    )
    broker = make_broker(client)
    await broker.connect()
    underlying = await broker.qualify_underlying("AAPL")

    quote = await broker.request_underlying_quote(underlying)

    assert quote.data_quality is expected
    assert quote.bid is None
    assert quote.ask is None
    assert quote.last is None
    assert quote.volume is None
    await broker.cancel_market_data((underlying.con_id,))


@pytest.mark.asyncio
async def test_quote_waits_for_a_current_update_and_does_not_reuse_cached_values() -> None:
    client = FakeIB()
    option = ib_option()
    template = Ticker(contract=option, time=FIXED_NOW, marketDataType=1)
    template.bid = 1.0
    template.ask = 1.1
    client.tickers[option.conId] = template
    broker = make_broker(client)
    await broker.connect()

    first = (await broker.request_option_quotes((domain_option(),)))[0]
    await broker.cancel_market_data((option.conId,))
    template.bid = 2.0
    template.ask = 2.1
    second = (await broker.request_option_quotes((domain_option(),)))[0]

    assert first.bid == Decimal("1.0")
    assert second.bid == Decimal("2.0")
    assert client.live_tickers[option.conId] is not template
    await broker.cancel_market_data((option.conId,))


@pytest.mark.asyncio
async def test_quote_without_a_current_price_update_times_out_and_cleans_up() -> None:
    client = FakeIB()
    client.publish_market_data_updates = False
    broker = make_broker(client)
    await broker.connect()

    with pytest.raises(MarketDataUnavailableError, match="current price update"):
        await broker.request_option_quotes((domain_option(),))

    assert client.cancelled_contracts == [202]


@pytest.mark.asyncio
async def test_ib_async_missing_theta_sentinel_is_not_fabricated_as_a_greek() -> None:
    client = FakeIB()
    option = ib_option()
    ticker = Ticker(contract=option, time=FIXED_NOW, marketDataType=1)
    ticker.bid = 3.10
    ticker.ask = 3.25
    ticker.modelGreeks = OptionComputation(
        tickAttrib=0,
        impliedVol=0.25,
        delta=-0.30,
        gamma=0.02,
        theta=-2,
    )
    client.tickers[option.conId] = ticker
    broker = make_broker(client)
    await broker.connect()

    quote = (await broker.request_option_quotes((domain_option(),)))[0]

    assert quote.greeks is not None
    assert quote.greeks.theta is None
    await broker.cancel_market_data((option.conId,))


@pytest.mark.asyncio
async def test_disconnect_cancels_only_active_chronos_market_data() -> None:
    client = FakeIB()
    broker = make_broker(client)
    await broker.connect()
    await broker.request_option_quotes((domain_option(),))

    await broker.disconnect()

    assert client.cancelled_contracts == [202]
    assert client.connected is False
    status = await broker.connection_status()
    assert status.state is ConnectionState.DISCONNECTED


@pytest.mark.asyncio
async def test_market_data_cancellation_failure_is_explicit() -> None:
    client = FakeIB()
    broker = make_broker(client)
    await broker.connect()
    await broker.request_option_quotes((domain_option(),))
    client.cancel_succeeds = False

    with pytest.raises(BrokerDataError, match="cancellation"):
        await broker.cancel_market_data((202,))

    with pytest.raises(BrokerDataError, match="cancellation"):
        await broker.disconnect()
    assert client.cancelled_contracts == [202, 202]
    assert client.connected is False


@pytest.mark.asyncio
async def test_ibkr_permission_event_is_classified_and_subscription_is_cleaned() -> None:
    client = FakeIB()
    client.market_data_errors.append(10089)
    broker = make_broker(client)
    await broker.connect()

    with pytest.raises(MarketDataPermissionError, match="permissions"):
        await broker.request_option_quotes((domain_option(),))

    assert client.cancelled_contracts == [202]


@pytest.mark.asyncio
async def test_ibkr_pacing_event_drives_bounded_manager_retry() -> None:
    client = FakeIB()
    client.market_data_errors.extend([420, 420, None])
    delays: list[float] = []

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    broker = make_broker(client)
    manager = MarketDataManager(
        broker,
        clock=lambda: FIXED_NOW,
        max_attempts=3,
        initial_backoff_seconds=0.1,
        sleep=record_delay,
    )
    await broker.connect()

    managed = await manager.option_quotes((domain_option(),), force_refresh=True)

    assert managed[0].quote.contract.con_id == 202
    assert delays == [0.1, 0.2]
    assert len(client.market_data_requests) == 3
    assert client.cancelled_contracts == [202, 202, 202]


@pytest.mark.asyncio
async def test_every_order_operation_fails_closed() -> None:
    client = FakeIB()
    broker = make_broker(client)
    request = OrderRequest(
        correlation_id="CHR-IBKR-READONLY",
        account_id=ACCOUNT_ID,
        contract=domain_option(),
        intent=OrderIntent.OPEN_SHORT_PUT,
        side=OrderSide.SELL,
        quantity=1,
        limit_price=Decimal("3.20"),
        order_ref="CHR-IBKR-READONLY",
    )

    with pytest.raises(BrokerSafetyError, match="read-only Milestone 2"):
        await broker.preview_order(request)
    with pytest.raises(BrokerSafetyError, match="read-only Milestone 2"):
        await broker.submit_order(request)
    with pytest.raises(BrokerSafetyError, match="read-only Milestone 2"):
        await broker.cancel_order(41)
