"""Deterministic, account-free broker fixtures for the complete demo workflow."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from chronos.broker.base import (
    BrokerConnectionError,
    BrokerDataError,
    BrokerSafetyError,
    BrokerSendGuard,
)
from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    DemoCase,
    DemoProfile,
    DisplayEnvironment,
    OptionRight,
    OrderLifecycle,
    OrderSide,
)
from chronos.domain.models import (
    AccountSummary,
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    CancellationResult,
    ConnectionStatus,
    CryptoContract,
    DemoFixtureCase,
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

DEMO_ACCOUNT_ID = "DU1234567"
DEMO_NOW = datetime(2026, 7, 15, 15, 30, tzinfo=UTC)
_EMPTY_ACCOUNT_OPTION_ID = 2002


def demo_now() -> datetime:
    """Return the fixed demo clock used for stable tests and screenshots."""

    return DEMO_NOW


class DemoBroker:
    """In-memory fixtures with no randomness, sockets, credentials, or order transmission."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = demo_now,
        profile: DemoProfile = DemoProfile.SAFETY_CASES,
    ) -> None:
        self._clock = clock
        self._profile = DemoProfile(profile)
        self._connected = False
        self._last_sync: datetime | None = None
        self._underlyings = self._make_underlyings()
        self._cryptos = self._make_cryptos()
        self._option_contracts: dict[int, OptionContract] = {}
        self._underlying_quotes: dict[int, MarketQuote] = {}
        self._option_quotes: dict[int, MarketQuote] = {}
        self._crypto_quotes: dict[int, MarketQuote] = {}
        self._positions: tuple[BrokerPosition, ...] = ()
        self._orders: tuple[BrokerOrder, ...] = ()
        self._executions: tuple[BrokerExecution, ...] = ()
        self._logger = logging.getLogger("chronos.broker.demo")

    @property
    def profile(self) -> DemoProfile:
        """Return the deterministic fixture profile selected for this broker."""

        return self._profile

    @property
    def fixture_cases(self) -> tuple[DemoFixtureCase, ...]:
        if self._profile is DemoProfile.EMPTY_ACCOUNT:
            return (
                DemoFixtureCase(
                    symbol="AAPL",
                    case=DemoCase.FLAT_PUT,
                    explanation=(
                        "The whole account is empty so the locked candidate, risk-preview, "
                        "and what-if journey can be demonstrated."
                    ),
                ),
            )
        return (
            DemoFixtureCase(
                symbol="AAPL",
                case=DemoCase.FLAT_PUT,
                explanation="No stock or short option; liquid demo puts are available.",
            ),
            DemoFixtureCase(
                symbol="MSFT",
                case=DemoCase.COVERED_CALL,
                explanation="Two hundred shares leave capacity for up to two covered calls.",
            ),
            DemoFixtureCase(
                symbol="SPY",
                case=DemoCase.ACTIVE_SHORT_PUT,
                explanation="An open short put locks another opening action.",
            ),
            DemoFixtureCase(
                symbol="AMD",
                case=DemoCase.ACTIVE_SHORT_CALL,
                explanation="One short call is fully covered by one hundred shares.",
            ),
            DemoFixtureCase(
                symbol="NVDA",
                case=DemoCase.STALE_DATA,
                explanation="The underlying quote is deliberately older than the freshness limit.",
            ),
            DemoFixtureCase(
                symbol="IWM",
                case=DemoCase.NO_VALID_CANDIDATE,
                explanation="Quotes deliberately lack the liquidity or Greeks needed to trade.",
            ),
            DemoFixtureCase(
                symbol="TSLA",
                case=DemoCase.PARTIAL_FILL_WARNING,
                explanation="One of two put contracts filled; reconciliation requires review.",
            ),
        )

    async def connect(self) -> None:
        as_of = self._clock().astimezone(UTC)
        self._build_fixtures(as_of)
        self._connected = True
        self._last_sync = as_of
        self._logger.info("Demo broker connected", extra={"event": "broker_connected"})

    async def disconnect(self) -> None:
        self._connected = False
        self._logger.info("Demo broker disconnected", extra={"event": "broker_disconnected"})

    async def connection_status(self) -> ConnectionStatus:
        return ConnectionStatus(
            state=ConnectionState.CONNECTED if self._connected else ConnectionState.DISCONNECTED,
            environment=DisplayEnvironment.DEMO,
            connected=self._connected,
            account_id=DEMO_ACCOUNT_ID if self._connected else None,
            data_quality=DataQuality.DEMO if self._connected else DataQuality.UNKNOWN,
            last_successful_sync=self._last_sync,
            message="Deterministic local fixtures"
            if self._connected
            else "Demo broker disconnected",
        )

    async def server_time(self) -> datetime:
        self._require_connection()
        return self._clock().astimezone(UTC)

    async def account_summary(self) -> AccountSummary:
        self._require_connection()
        return AccountSummary(
            account_id=DEMO_ACCOUNT_ID,
            net_liquidation=Decimal("250000.00"),
            total_cash=Decimal("125000.00"),
            buying_power=Decimal("240000.00"),
            as_of=self._last_sync or self._clock().astimezone(UTC),
        )

    async def positions(self) -> tuple[BrokerPosition, ...]:
        self._require_connection()
        return self._positions

    async def executions(self, since: datetime | None = None) -> tuple[BrokerExecution, ...]:
        self._require_connection()
        if since is None:
            return self._executions
        return tuple(execution for execution in self._executions if execution.timestamp >= since)

    async def open_orders(self) -> tuple[BrokerOrder, ...]:
        self._require_connection()
        return self._orders

    async def qualify_underlying(self, symbol: str) -> UnderlyingContract:
        self._require_connection()
        try:
            return self._underlyings[symbol.upper()]
        except KeyError as error:
            raise BrokerDataError(f"No deterministic demo contract for {symbol.upper()}") from error

    async def qualify_crypto(self, symbol: str) -> CryptoContract:
        self._require_connection()
        try:
            return self._cryptos[symbol.upper()]
        except KeyError as error:
            raise BrokerDataError(
                f"No deterministic demo crypto contract for {symbol.upper()}"
            ) from error

    async def option_chain_parameters(
        self,
        underlying: UnderlyingContract,
    ) -> tuple[OptionChainParameters, ...]:
        self._require_connection()
        contracts = [
            contract
            for contract in self._option_contracts.values()
            if contract.symbol == underlying.symbol
        ]
        if self._profile is DemoProfile.EMPTY_ACCOUNT:
            contracts = [
                contract for contract in contracts if contract.con_id == _EMPTY_ACCOUNT_OPTION_ID
            ]
        if not contracts:
            raise BrokerDataError(f"No deterministic option chain for {underlying.symbol}")
        return (
            OptionChainParameters(
                exchange="SMART",
                underlying_con_id=underlying.con_id,
                trading_class=underlying.symbol,
                multiplier=Decimal("100"),
                expirations=tuple(sorted({contract.expiration for contract in contracts})),
                strikes=tuple(sorted({contract.strike for contract in contracts})),
            ),
        )

    async def qualify_option_contracts(
        self,
        contracts: Sequence[OptionContractSpec],
    ) -> tuple[OptionContract, ...]:
        self._require_connection()
        qualified: list[OptionContract] = []
        for requested in contracts:
            match = next(
                (
                    contract
                    for contract in self._option_contracts.values()
                    if contract.symbol == requested.symbol
                    and contract.expiration == requested.expiration
                    and contract.strike == requested.strike
                    and contract.right is requested.right
                    and (
                        requested.underlying_con_id is None
                        or contract.underlying_con_id == requested.underlying_con_id
                    )
                    and contract.exchange == requested.exchange
                    and contract.currency == requested.currency
                    and contract.multiplier == requested.multiplier
                    and contract.trading_class == requested.trading_class
                ),
                None,
            )
            if match is None:
                raise BrokerDataError(
                    f"Demo contract is not qualified: {requested.symbol} "
                    f"{requested.expiration} {requested.strike} {requested.right}"
                )
            qualified.append(match)
        return tuple(qualified)

    async def request_underlying_quote(self, contract: UnderlyingContract) -> MarketQuote:
        self._require_connection()
        try:
            return self._underlying_quotes[contract.con_id]
        except KeyError as error:
            raise BrokerDataError(f"No deterministic quote for {contract.symbol}") from error

    async def request_crypto_quote(self, contract: CryptoContract) -> MarketQuote:
        self._require_connection()
        try:
            return self._crypto_quotes[contract.con_id]
        except KeyError as error:
            raise BrokerDataError(f"No deterministic crypto quote for {contract.symbol}") from error

    async def request_option_quotes(
        self,
        contracts: Sequence[OptionContract],
    ) -> tuple[MarketQuote, ...]:
        self._require_connection()
        quotes: list[MarketQuote] = []
        for contract in contracts:
            try:
                quotes.append(self._option_quotes[contract.con_id])
            except KeyError as error:
                raise BrokerDataError(
                    f"No deterministic option quote for {contract.local_symbol}"
                ) from error
        return tuple(quotes)

    async def cancel_market_data(self, contract_ids: Sequence[int]) -> None:
        self._require_connection()
        self._logger.info(
            "Demo market data cancelled",
            extra={"event": "market_data_cancelled", "contract_ids": tuple(contract_ids)},
        )

    async def preview_order(self, request: OrderRequest) -> OrderPreview:
        self._require_connection()
        warnings: list[str] = ["Demo preview only; no broker order will be created."]
        accepted = True
        if request.account_id != DEMO_ACCOUNT_ID:
            accepted = False
            warnings.append("Configured account does not match the connected demo account.")
        if not request.order_ref.startswith("CHR-"):
            accepted = False
            warnings.append("Order reference is not Chronos-owned.")
        if request.transmit:
            accepted = False
            warnings.append("Demo mode cannot transmit orders.")
        return OrderPreview(
            request=request,
            accepted=accepted,
            estimated_commission=Decimal("0.65") * request.quantity,
            initial_margin_change=Decimal("0.00"),
            maintenance_margin_change=Decimal("0.00"),
            equity_with_loan_change=Decimal("0.00"),
            warnings=tuple(warnings),
            broker_message="Deterministic demo what-if response",
            previewed_at=self._clock().astimezone(UTC),
        )

    async def submit_order(
        self,
        request: OrderRequest,
        *,
        send_guard: BrokerSendGuard | None = None,
    ) -> OrderSubmission:
        del request, send_guard
        self._require_connection()
        raise BrokerSafetyError("DemoBroker cannot submit orders")

    async def modify_order(self, request: OrderModification) -> OrderSubmission:
        del request
        self._require_connection()
        raise BrokerSafetyError("DemoBroker cannot modify orders")

    async def cancel_order(self, broker_order_id: int) -> CancellationResult:
        self._require_connection()
        order = next(
            (
                candidate
                for candidate in self._orders
                if candidate.broker_order_id == broker_order_id
            ),
            None,
        )
        if order is None:
            return CancellationResult(
                broker_order_id=broker_order_id,
                requested=False,
                lifecycle=OrderLifecycle.REJECTED,
                message="Order is not open in the demo fixture.",
                timestamp=self._clock().astimezone(UTC),
            )
        if not (order.order_ref or "").startswith("CHR-"):
            raise BrokerSafetyError("Chronos will not cancel an order it does not own")
        return CancellationResult(
            broker_order_id=broker_order_id,
            requested=True,
            lifecycle=OrderLifecycle.CANCELLED,
            message="Deterministic cancellation accepted.",
            timestamp=self._clock().astimezone(UTC),
        )

    async def snapshot(self) -> BrokerSnapshot:
        self._require_connection()
        return BrokerSnapshot(
            account=await self.account_summary(),
            positions=await self.positions(),
            open_orders=await self.open_orders(),
            executions=await self.executions(),
            captured_at=self._clock().astimezone(UTC),
        )

    def _require_connection(self) -> None:
        if not self._connected:
            raise BrokerConnectionError("Demo broker is disconnected")

    @staticmethod
    def _make_underlyings() -> dict[str, UnderlyingContract]:
        values = (
            (1001, "AAPL", "NASDAQ"),
            (1002, "MSFT", "NASDAQ"),
            (1003, "SPY", "ARCA"),
            (1004, "AMD", "NASDAQ"),
            (1005, "NVDA", "NASDAQ"),
            (1006, "IWM", "ARCA"),
            (1007, "TSLA", "NASDAQ"),
        )
        return {
            symbol: UnderlyingContract(
                con_id=con_id,
                symbol=symbol,
                primary_exchange=primary_exchange,
            )
            for con_id, symbol, primary_exchange in values
        }

    @staticmethod
    def _make_cryptos() -> dict[str, CryptoContract]:
        # Venue metadata mirrors qualified ContractDetails: min_size/size_increment
        # are always populated here so the demo exercises the CONFORMING path
        # (the UNKNOWN/absent-metadata path is exercised by the risk unit tests).
        values = (
            (3001, "BTC", "0.01", "0.0001", "0.0001"),
            (3002, "ETH", "0.01", "0.001", "0.001"),
        )
        return {
            symbol: CryptoContract(
                con_id=con_id,
                symbol=symbol,
                min_tick=Decimal(min_tick),
                min_size=Decimal(min_size),
                size_increment=Decimal(size_increment),
            )
            for con_id, symbol, min_tick, min_size, size_increment in values
        }

    def _build_fixtures(self, as_of: datetime) -> None:
        expiry_14 = (as_of + timedelta(days=14)).date()
        expiry_21 = (as_of + timedelta(days=21)).date()
        expiry_35 = (as_of + timedelta(days=35)).date()

        options = (
            self._option(2001, "AAPL", expiry_14, "180", OptionRight.PUT),
            self._option(2002, "AAPL", expiry_21, "185", OptionRight.PUT),
            self._option(2003, "AAPL", expiry_35, "175", OptionRight.PUT),
            self._option(2101, "MSFT", expiry_14, "430", OptionRight.CALL),
            self._option(2102, "MSFT", expiry_21, "440", OptionRight.CALL),
            self._option(2103, "MSFT", expiry_35, "450", OptionRight.CALL),
            self._option(2201, "SPY", expiry_21, "540", OptionRight.PUT),
            self._option(2301, "AMD", expiry_14, "175", OptionRight.CALL),
            self._option(2401, "NVDA", expiry_21, "145", OptionRight.PUT),
            self._option(2501, "IWM", expiry_21, "215", OptionRight.PUT),
            self._option(2601, "TSLA", expiry_21, "230", OptionRight.PUT),
        )
        self._option_contracts = {contract.con_id: contract for contract in options}

        stock_values = {
            "AAPL": ("190.20", "190.30", "190.25", DataQuality.DEMO, as_of),
            "MSFT": ("421.70", "421.90", "421.80", DataQuality.DEMO, as_of),
            "SPY": ("552.35", "552.45", "552.40", DataQuality.DEMO, as_of),
            "AMD": ("168.10", "168.22", "168.16", DataQuality.DEMO, as_of),
            "NVDA": (
                "151.30",
                "151.44",
                "151.38",
                DataQuality.STALE,
                as_of - timedelta(seconds=60),
            ),
            "IWM": ("224.18", "224.28", "224.24", DataQuality.DEMO, as_of),
            "TSLA": ("247.60", "247.85", "247.72", DataQuality.DEMO, as_of),
        }
        self._underlying_quotes = {
            self._underlyings[symbol].con_id: MarketQuote(
                contract=self._underlyings[symbol],
                timestamp=timestamp,
                data_quality=quality,
                bid=Decimal(bid),
                ask=Decimal(ask),
                last=Decimal(last),
                close=Decimal(last),
                volume=1_000_000,
            )
            for symbol, (bid, ask, last, quality, timestamp) in stock_values.items()
        }

        crypto_values = {
            "BTC": ("63990.00", "64010.00", "64000.00"),
            "ETH": ("2999.00", "3001.00", "3000.00"),
        }
        self._crypto_quotes = {
            self._cryptos[symbol].con_id: MarketQuote(
                contract=self._cryptos[symbol],
                timestamp=as_of,
                data_quality=DataQuality.DEMO,
                bid=Decimal(bid),
                ask=Decimal(ask),
                last=Decimal(last),
                close=Decimal(last),
                volume=1_000_000,
            )
            for symbol, (bid, ask, last) in crypto_values.items()
        }

        option_values: dict[int, tuple[str, str, str | None, int | None, int | None]] = {
            2001: ("1.88", "1.98", "-0.28", 842, 3_204),
            2002: ("3.15", "3.30", "-0.34", 1_125, 5_630),
            2003: ("1.60", "1.72", "-0.21", 485, 2_117),
            2101: ("4.55", "4.75", "0.39", 1_042, 4_400),
            2102: ("3.05", "3.20", "0.30", 1_886, 8_215),
            2103: ("2.62", "2.78", "0.23", 611, 3_002),
            2201: ("3.90", "4.05", "-0.29", 9_025, 42_100),
            2301: ("2.38", "2.52", "0.31", 622, 2_904),
            2401: ("3.10", "3.35", "-0.30", 1_203, 6_710),
            2501: ("0.00", "1.25", None, None, None),
            2601: ("4.80", "5.05", "-0.32", 2_532, 10_115),
        }
        self._option_quotes = {}
        for con_id, (bid, ask, delta, volume, open_interest) in option_values.items():
            contract = self._option_contracts[con_id]
            quality = DataQuality.STALE if con_id == 2401 else DataQuality.DEMO
            timestamp = as_of - timedelta(seconds=60) if con_id == 2401 else as_of
            self._option_quotes[con_id] = MarketQuote(
                contract=contract,
                timestamp=timestamp,
                data_quality=quality,
                bid=Decimal(bid),
                ask=Decimal(ask),
                last=(Decimal(bid) + Decimal(ask)) / Decimal("2"),
                volume=volume,
                open_interest=open_interest,
                greeks=(
                    ModelGreeks(
                        delta=Decimal(delta),
                        gamma=Decimal("0.02"),
                        theta=Decimal("-0.08"),
                        implied_volatility=Decimal("0.24"),
                    )
                    if delta is not None
                    else None
                ),
            )

        if self._profile is DemoProfile.EMPTY_ACCOUNT:
            self._positions = ()
            self._orders = ()
            self._executions = ()
            return

        self._positions = (
            self._stock_position("MSFT", "200", "390.00", "421.80"),
            self._option_position(2201, "-1", "2.45", "3.98"),
            self._stock_position("AMD", "100", "145.00", "168.16"),
            self._option_position(2301, "-1", "1.95", "2.45"),
            self._option_position(2601, "-1", "4.75", "4.92"),
        )
        tsla_order = BrokerOrder(
            broker_order_id=7001,
            permanent_id=9001,
            client_id=17,
            account_id=DEMO_ACCOUNT_ID,
            order_ref="CHR-DEMO-PARTIAL",
            contract=self._option_contracts[2601],
            side=OrderSide.SELL,
            quantity=Decimal("2"),
            filled_quantity=Decimal("1"),
            remaining_quantity=Decimal("1"),
            limit_price=Decimal("4.75"),
            lifecycle=OrderLifecycle.PARTIALLY_FILLED,
        )
        self._orders = (tsla_order,)
        self._executions = (
            BrokerExecution(
                execution_id="DEMO-EXEC-0001",
                account_id=DEMO_ACCOUNT_ID,
                broker_order_id=7001,
                permanent_id=9001,
                client_id=17,
                order_ref=tsla_order.order_ref,
                contract=self._option_contracts[2601],
                side=OrderSide.SELL,
                quantity=Decimal("1"),
                price=Decimal("4.75"),
                timestamp=as_of - timedelta(minutes=3),
                commission=Decimal("0.65"),
                commission_currency="USD",
            ),
        )

    def _option(
        self,
        con_id: int,
        symbol: str,
        expiration: date,
        strike: str,
        right: OptionRight,
    ) -> OptionContract:
        expiration_text = expiration.strftime("%y%m%d")
        local_symbol = f"{symbol} {expiration_text}{right.value}{int(Decimal(strike) * 1000):08d}"
        return OptionContract(
            con_id=con_id,
            symbol=symbol,
            underlying_con_id=self._underlyings[symbol].con_id,
            expiration=expiration,
            strike=Decimal(strike),
            right=right,
            multiplier=Decimal("100"),
            trading_class=symbol,
            local_symbol=local_symbol,
            deliverable_shares=Decimal("100"),
            deliverable_verified=True,
        )

    def _stock_position(
        self,
        symbol: str,
        quantity: str,
        average_cost: str,
        market_price: str,
    ) -> BrokerPosition:
        return BrokerPosition(
            account_id=DEMO_ACCOUNT_ID,
            contract=self._underlyings[symbol],
            quantity=Decimal(quantity),
            average_cost=Decimal(average_cost),
            market_price=Decimal(market_price),
            unrealized_pnl=((Decimal(market_price) - Decimal(average_cost)) * Decimal(quantity)),
        )

    def _option_position(
        self,
        con_id: int,
        quantity: str,
        average_cost: str,
        market_price: str,
    ) -> BrokerPosition:
        return BrokerPosition(
            account_id=DEMO_ACCOUNT_ID,
            contract=self._option_contracts[con_id],
            quantity=Decimal(quantity),
            average_cost=Decimal(average_cost),
            market_price=Decimal(market_price),
            unrealized_pnl=None,
        )
