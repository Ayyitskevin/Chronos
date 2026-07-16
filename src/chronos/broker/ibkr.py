"""Read-only Interactive Brokers adapter built on :mod:`ib_async`.

Milestone 2 deliberately exposes portfolio and market-data reads only. Every
order method fails closed until the paper-order workflow and its guardrails are
implemented in a later milestone.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast

from ib_async import (
    IB,
    AccountValue,
    Contract,
    ContractDetails,
    ExecutionFilter,
    Fill,
    OptionChain,
    Position,
    Stock,
    Ticker,
    Trade,
)
from ib_async import (
    Option as IBOption,
)
from ib_async.util import UNSET_DOUBLE

from chronos.broker.base import (
    BrokerConnectionError,
    BrokerDataError,
    BrokerSafetyError,
)
from chronos.broker.market_data import (
    MarketDataPacingError,
    MarketDataPermissionError,
    MarketDataUnavailableError,
)
from chronos.config.settings import Settings
from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    DisplayEnvironment,
    IBEnvironment,
    OptionRight,
    OrderLifecycle,
    OrderSide,
    SecurityType,
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
from chronos.utils.logging import mask_account_id

_OPTION_GENERIC_TICKS = "100,101,106"
_IB_UNSET_DOUBLE = Decimal(str(UNSET_DOUBLE))
_MARKET_DATA_PACING_CODES = frozenset({100, 420})
_MARKET_DATA_CAPACITY_CODES = frozenset({101})
_MARKET_DATA_PERMISSION_CODES = frozenset({354, 10089, 10090, 10091, 10186, 10189, 10197})
_PRICE_TICK_TYPES = frozenset({1, 2, 4, 9, 66, 67, 68, 75})


def utc_now() -> datetime:
    """Return an aware UTC observation timestamp."""

    return datetime.now(tz=UTC)


IBErrorHandler = Callable[[int, int, str, Contract | None], None]


class IBErrorEvent(Protocol):
    """Small event surface used to observe asynchronous TWS request failures."""

    def connect(self, handler: IBErrorHandler) -> object: ...


class IBClient(Protocol):
    """Narrow `ib_async.IB` surface used by the adapter and its fakes."""

    @property
    def errorEvent(self) -> IBErrorEvent: ...

    async def connectAsync(
        self,
        host: str,
        port: int,
        clientId: int,
        timeout: float | None,
        readonly: bool,
        account: str,
        raiseSyncErrors: bool,
    ) -> object: ...

    def disconnect(self) -> object: ...

    def isConnected(self) -> bool: ...

    def managedAccounts(self) -> list[str]: ...

    def positions(self, account: str = "") -> list[Position]: ...

    async def accountSummaryAsync(self, account: str = "") -> list[AccountValue]: ...

    async def reqCurrentTimeAsync(self) -> datetime: ...

    async def reqExecutionsAsync(
        self,
        execFilter: ExecutionFilter | None = None,
    ) -> list[Fill]: ...

    async def reqAllOpenOrdersAsync(self) -> list[Trade]: ...

    async def qualifyContractsAsync(
        self,
        *contracts: Contract,
        returnAll: bool = False,
    ) -> list[Contract | list[Contract | None] | None]: ...

    async def reqContractDetailsAsync(self, contract: Contract) -> list[ContractDetails]: ...

    async def reqSecDefOptParamsAsync(
        self,
        underlyingSymbol: str,
        futFopExchange: str,
        underlyingSecType: str,
        underlyingConId: int,
    ) -> list[OptionChain]: ...

    def reqMktData(
        self,
        contract: Contract,
        genericTickList: str = "",
        snapshot: bool = False,
        regulatorySnapshot: bool = False,
    ) -> Ticker: ...

    def cancelMktData(self, contract: Contract) -> bool: ...


class IBKRBroker:
    """Fail-closed, read-only adapter for one configured IBKR account."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: IBClient | None = None,
        clock: Callable[[], datetime] = utc_now,
        quote_timeout_seconds: float = 5.0,
        quote_settle_seconds: float = 0.1,
    ) -> None:
        if quote_timeout_seconds <= 0:
            raise ValueError("quote_timeout_seconds must be positive")
        if quote_settle_seconds < 0:
            raise ValueError("quote_settle_seconds must be non-negative")
        self._settings = settings
        self._client = client if client is not None else cast(IBClient, IB())
        self._clock = clock
        self._quote_timeout_seconds = quote_timeout_seconds
        self._quote_settle_seconds = quote_settle_seconds
        self._account_id: str | None = None
        self._last_sync: datetime | None = None
        self._data_quality = DataQuality.UNKNOWN
        self._ib_contracts: dict[int, Contract] = {}
        self._domain_contracts: dict[int, Instrument] = {}
        self._active_market_data: dict[int, Contract] = {}
        self._starting_market_data: set[int] = set()
        self._market_data_waiters: dict[int, asyncio.Future[datetime]] = {}
        self._market_data_errors: dict[int, BrokerDataError] = {}
        self._global_market_data_error: BrokerDataError | None = None
        self._logger = logging.getLogger("chronos.broker.ibkr")
        self._client.errorEvent.connect(self._on_ib_error)

    async def connect(self) -> None:
        """Connect read-only and explicitly synchronize all-account open orders."""

        if self._is_connected():
            if self._account_id is not None:
                return
            self._safe_disconnect()
        self._active_market_data.clear()
        self._starting_market_data.clear()
        self._market_data_waiters.clear()
        self._market_data_errors.clear()
        self._global_market_data_error = None
        self._ib_contracts.clear()
        self._domain_contracts.clear()
        configured_account = self._settings.ib_account_id.strip()
        try:
            await self._client.connectAsync(
                host=self._settings.ib_host,
                port=self._settings.ib_port,
                clientId=self._settings.ib_client_id,
                timeout=10.0,
                readonly=True,
                account=configured_account,
                raiseSyncErrors=True,
            )
            if not self._is_connected():
                raise BrokerConnectionError("IBKR socket was not ready after connection")
            account_id = self._resolve_account_id(configured_account)
            self._account_id = account_id
            # ib_async skips open-order startup sync when readonly=True. Chronos
            # still needs this all-client snapshot for safe reconciliation.
            await self._client.reqAllOpenOrdersAsync()
            self._last_sync = self._now()
        except Exception as error:
            self._account_id = None
            self._data_quality = DataQuality.UNKNOWN
            self._safe_disconnect()
            if isinstance(error, BrokerConnectionError):
                raise
            raise BrokerConnectionError("Unable to establish the read-only IBKR session") from error

        self._logger.info(
            "IBKR read-only broker connected to %s",
            mask_account_id(account_id),
            extra={"event": "broker_connected"},
        )

    async def disconnect(self) -> None:
        """Cancel only Chronos-owned market data, then disconnect cleanly."""

        cancellation_error: BrokerDataError | None = None
        if self._active_market_data and self._is_connected():
            try:
                await self.cancel_market_data(tuple(self._active_market_data))
            except BrokerDataError as error:
                cancellation_error = error
                self._logger.warning(
                    "One or more market-data cancellations failed during disconnect",
                    extra={"event": "market_data_cancel_failed"},
                )
        self._safe_disconnect()
        self._active_market_data.clear()
        self._starting_market_data.clear()
        self._market_data_waiters.clear()
        self._market_data_errors.clear()
        self._global_market_data_error = None
        self._ib_contracts.clear()
        self._domain_contracts.clear()
        self._account_id = None
        self._data_quality = DataQuality.UNKNOWN
        self._logger.info("IBKR broker disconnected", extra={"event": "broker_disconnected"})
        if cancellation_error is not None:
            raise cancellation_error

    async def connection_status(self) -> ConnectionStatus:
        connected = self._is_connected()
        account_ready = connected and self._account_id is not None
        if account_ready:
            state = ConnectionState.CONNECTED
            message = "Read-only IBKR session"
        elif connected:
            state = ConnectionState.DEGRADED
            message = "IBKR connected without an unambiguous account"
        else:
            state = ConnectionState.DISCONNECTED
            message = "IBKR broker disconnected"
        return ConnectionStatus(
            state=state,
            environment=self._display_environment,
            connected=connected,
            account_id=self._account_id,
            data_quality=self._data_quality if connected else DataQuality.UNKNOWN,
            last_successful_sync=self._last_sync,
            message=message,
        )

    async def server_time(self) -> datetime:
        self._require_connection()
        try:
            server_time = await self._client.reqCurrentTimeAsync()
            normalized = self._aware_utc(server_time, "IBKR server time")
        except BrokerDataError:
            raise
        except Exception as error:
            raise BrokerDataError("IBKR server time is unavailable") from error
        self._last_sync = self._now()
        return normalized

    async def account_summary(self) -> AccountSummary:
        account_id = self._require_connection()
        try:
            values = await self._client.accountSummaryAsync(account_id)
        except Exception as error:
            raise BrokerDataError("IBKR account summary is unavailable") from error

        net = self._one_account_value(values, account_id, "NetLiquidation")
        cash = self._one_account_value(values, account_id, "TotalCashValue")
        buying_power = self._one_account_value(values, account_id, "BuyingPower")
        currencies = {value.currency for value in (net, cash, buying_power) if value.currency}
        if len(currencies) != 1:
            raise BrokerDataError("IBKR account-summary currencies are missing or inconsistent")
        net_value = self._required_decimal(net.value, "NetLiquidation")
        cash_value = self._required_decimal(cash.value, "TotalCashValue")
        buying_power_value = self._required_decimal(buying_power.value, "BuyingPower")
        if net_value < 0 or buying_power_value < 0:
            raise BrokerDataError("IBKR account summary contains an invalid negative balance")
        observed_at = self._now()
        self._last_sync = observed_at
        return AccountSummary(
            account_id=account_id,
            net_liquidation=net_value,
            total_cash=cash_value,
            buying_power=buying_power_value,
            currency=next(iter(currencies)),
            as_of=observed_at,
        )

    async def positions(self) -> tuple[BrokerPosition, ...]:
        account_id = self._require_connection()
        try:
            positions = self._client.positions(account_id)
        except Exception as error:
            raise BrokerDataError("IBKR positions are unavailable") from error

        mapped: list[BrokerPosition] = []
        for position in positions:
            if position.account != account_id:
                continue
            mapped.append(
                BrokerPosition(
                    account_id=account_id,
                    contract=await self._instrument_from_ib(position.contract),
                    quantity=self._required_decimal(position.position, "position quantity"),
                    average_cost=self._required_decimal(position.avgCost, "position average cost"),
                    market_price=None,
                    unrealized_pnl=None,
                )
            )
        self._last_sync = self._now()
        return tuple(mapped)

    async def executions(self, since: datetime | None = None) -> tuple[BrokerExecution, ...]:
        account_id = self._require_connection()
        normalized_since = self._aware_utc(since, "execution filter") if since else None
        try:
            fills = await self._client.reqExecutionsAsync(ExecutionFilter(acctCode=account_id))
        except Exception as error:
            raise BrokerDataError("IBKR executions are unavailable") from error

        mapped: list[BrokerExecution] = []
        for fill in fills:
            execution = fill.execution
            if execution.acctNumber != account_id:
                continue
            timestamp = self._execution_timestamp(fill)
            if normalized_since is not None and timestamp < normalized_since:
                continue
            if not execution.execId:
                raise BrokerDataError("IBKR execution is missing its execution identifier")
            mapped.append(
                BrokerExecution(
                    execution_id=execution.execId,
                    account_id=account_id,
                    broker_order_id=execution.orderId,
                    permanent_id=execution.permId if execution.permId > 0 else None,
                    client_id=execution.clientId,
                    order_ref=execution.orderRef or None,
                    contract=await self._instrument_from_ib(fill.contract),
                    side=self._order_side(execution.side),
                    quantity=self._required_positive_decimal(
                        execution.shares, "execution quantity"
                    ),
                    price=self._required_positive_decimal(execution.price, "execution price"),
                    timestamp=timestamp,
                    commission=self._commission(fill),
                )
            )
        self._last_sync = self._now()
        return tuple(mapped)

    async def open_orders(self) -> tuple[BrokerOrder, ...]:
        account_id = self._require_connection()
        try:
            trades = await self._client.reqAllOpenOrdersAsync()
        except Exception as error:
            raise BrokerDataError("IBKR open orders are unavailable") from error

        mapped: list[BrokerOrder] = []
        for trade in trades:
            order = trade.order
            if not order.account:
                raise BrokerDataError("IBKR returned an open order without an account identifier")
            if order.account != account_id:
                continue
            if order.orderType.upper() != "LMT":
                raise BrokerDataError("IBKR returned a non-limit open order for this account")
            quantity = self._required_positive_decimal(order.totalQuantity, "order quantity")
            filled = self._required_decimal(trade.orderStatus.filled, "filled quantity")
            remaining = self._required_decimal(trade.orderStatus.remaining, "remaining quantity")
            if filled < 0 or remaining < 0 or filled + remaining != quantity:
                raise BrokerDataError("IBKR open-order quantities are internally inconsistent")
            mapped.append(
                BrokerOrder(
                    broker_order_id=order.orderId,
                    permanent_id=self._permanent_id(trade),
                    client_id=order.clientId or trade.orderStatus.clientId,
                    account_id=account_id,
                    order_ref=order.orderRef or None,
                    contract=await self._instrument_from_ib(trade.contract),
                    side=self._order_side(order.action),
                    quantity=quantity,
                    filled_quantity=filled,
                    remaining_quantity=remaining,
                    limit_price=self._required_positive_decimal(order.lmtPrice, "limit price"),
                    lifecycle=self._order_lifecycle(trade.orderStatus.status, filled),
                    transmit=bool(order.transmit),
                    outside_rth=bool(order.outsideRth),
                )
            )
        self._last_sync = self._now()
        return tuple(mapped)

    async def qualify_underlying(self, symbol: str) -> UnderlyingContract:
        self._require_connection()
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol or not normalized_symbol.isalnum():
            raise BrokerDataError("Underlying symbol must be a non-empty alphanumeric value")
        if normalized_symbol not in self._settings.symbol_allowlist:
            raise BrokerSafetyError("Underlying symbol is not in SYMBOL_ALLOWLIST")
        requested = Stock(normalized_symbol, "SMART", "USD")
        contract = (await self._qualify((requested,)))[0]
        if contract.secType != SecurityType.STOCK.value or contract.symbol != normalized_symbol:
            raise BrokerDataError("IBKR qualified an unexpected underlying contract")
        mapped = self._underlying_from_ib(contract)
        self._cache_contract(contract, mapped)
        self._last_sync = self._now()
        return mapped

    async def option_chain_parameters(
        self,
        underlying: UnderlyingContract,
    ) -> tuple[OptionChainParameters, ...]:
        self._require_connection()
        try:
            chains = await self._client.reqSecDefOptParamsAsync(
                underlying.symbol,
                "",
                SecurityType.STOCK.value,
                underlying.con_id,
            )
        except Exception as error:
            raise BrokerDataError(
                f"IBKR option-chain metadata is unavailable for {underlying.symbol}"
            ) from error
        if not chains:
            raise BrokerDataError(f"IBKR returned no option chains for {underlying.symbol}")

        mapped: list[OptionChainParameters] = []
        for chain in chains:
            if chain.underlyingConId != underlying.con_id:
                raise BrokerDataError("IBKR option-chain metadata references another underlying")
            if not chain.tradingClass or not chain.exchange:
                raise BrokerDataError("IBKR option-chain metadata is missing routing identity")
            expirations = tuple(sorted({self._expiration(value) for value in chain.expirations}))
            strikes = tuple(
                sorted(
                    {
                        self._required_positive_decimal(value, "option-chain strike")
                        for value in chain.strikes
                    }
                )
            )
            if not expirations or not strikes:
                raise BrokerDataError("IBKR option-chain metadata is incomplete")
            mapped.append(
                OptionChainParameters(
                    exchange=chain.exchange,
                    underlying_con_id=chain.underlyingConId,
                    trading_class=chain.tradingClass,
                    multiplier=self._required_positive_decimal(
                        chain.multiplier, "option-chain multiplier"
                    ),
                    expirations=expirations,
                    strikes=strikes,
                )
            )
        self._last_sync = self._now()
        return tuple(mapped)

    async def qualify_option_contracts(
        self,
        contracts: Sequence[OptionContractSpec],
    ) -> tuple[OptionContract, ...]:
        self._require_connection()
        if not contracts:
            return ()
        requested = tuple(self._option_request(specification) for specification in contracts)
        qualified = await self._qualify(requested)
        mapped: list[OptionContract] = []
        for specification, contract in zip(contracts, qualified, strict=True):
            min_tick = await self._option_min_tick(contract)
            option = self._option_from_ib(contract, min_tick=min_tick)
            if (
                option.symbol != specification.symbol
                or option.expiration != specification.expiration
                or option.strike != specification.strike
                or option.right is not specification.right
                or option.multiplier != specification.multiplier
                or option.trading_class != specification.trading_class
            ):
                raise BrokerDataError("IBKR qualified an option different from the request")
            self._cache_contract(contract, option)
            mapped.append(option)
        self._last_sync = self._now()
        return tuple(mapped)

    async def request_underlying_quote(
        self,
        contract: UnderlyingContract,
    ) -> MarketQuote:
        return (await self._request_quotes((contract,)))[0]

    async def request_option_quotes(
        self,
        contracts: Sequence[OptionContract],
    ) -> tuple[MarketQuote, ...]:
        if not contracts:
            return ()
        return await self._request_quotes(tuple(contracts))

    async def cancel_market_data(self, contract_ids: Sequence[int]) -> None:
        self._require_connection()
        failures: list[int] = []
        for contract_id in dict.fromkeys(contract_ids):
            contract = self._active_market_data.get(contract_id)
            if contract is None:
                continue
            try:
                cancelled = self._client.cancelMktData(contract)
            except Exception:
                failures.append(contract_id)
                continue
            if not cancelled:
                failures.append(contract_id)
                continue
            self._active_market_data.pop(contract_id, None)
            self._logger.debug(
                "Sent Chronos market-data cancellation",
                extra={"event": "market_data_cancel_sent", "contract_id": contract_id},
            )
        if failures:
            raise BrokerDataError(
                "IBKR could not issue a cancellation request for one or more Chronos subscriptions"
            )

    async def preview_order(self, request: OrderRequest) -> OrderPreview:
        del request
        raise BrokerSafetyError("IBKR order previews are disabled in read-only Milestone 2")

    async def submit_order(self, request: OrderRequest) -> OrderSubmission:
        del request
        raise BrokerSafetyError("IBKR order submission is disabled in read-only Milestone 2")

    async def modify_order(self, request: OrderModification) -> OrderSubmission:
        del request
        raise BrokerSafetyError("IBKR order modification is disabled in read-only Milestone 2")

    async def cancel_order(self, broker_order_id: int) -> CancellationResult:
        del broker_order_id
        raise BrokerSafetyError("IBKR order cancellation is disabled in read-only Milestone 2")

    @property
    def _display_environment(self) -> DisplayEnvironment:
        if self._settings.ib_environment is IBEnvironment.PAPER:
            return DisplayEnvironment.PAPER
        return DisplayEnvironment.LIVE

    def _is_connected(self) -> bool:
        try:
            return self._client.isConnected()
        except Exception:
            return False

    def _safe_disconnect(self) -> None:
        try:
            self._client.disconnect()
        except Exception:
            self._logger.warning(
                "IBKR client disconnect failed",
                extra={"event": "broker_disconnect_failed"},
            )

    def _resolve_account_id(self, configured_account: str) -> str:
        accounts = tuple(account for account in self._client.managedAccounts() if account)
        if configured_account:
            if configured_account not in accounts:
                raise BrokerConnectionError(
                    "Configured IBKR account is not managed by this session"
                )
            return configured_account
        if len(accounts) != 1:
            raise BrokerConnectionError(
                "IB_ACCOUNT_ID is required when the session has zero or multiple accounts"
            )
        return accounts[0]

    def _require_connection(self) -> str:
        if not self._is_connected() or self._account_id is None:
            raise BrokerConnectionError("IBKR broker is not connected to an unambiguous account")
        return self._account_id

    def _now(self) -> datetime:
        return self._aware_utc(self._clock(), "local observation clock")

    @staticmethod
    def _aware_utc(value: datetime, label: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise BrokerDataError(f"{label} must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _one_account_value(
        values: Sequence[AccountValue],
        account_id: str,
        tag: str,
    ) -> AccountValue:
        matches = [value for value in values if value.account == account_id and value.tag == tag]
        unique = {(value.value, value.currency) for value in matches}
        if len(unique) != 1:
            raise BrokerDataError(f"IBKR account summary has missing or ambiguous {tag}")
        return matches[0]

    @staticmethod
    def _optional_decimal(value: object, *, nonnegative: bool = False) -> Decimal | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            converted = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if not converted.is_finite() or converted == _IB_UNSET_DOUBLE:
            return None
        if nonnegative and converted < 0:
            return None
        return converted

    @classmethod
    def _required_decimal(cls, value: object, label: str) -> Decimal:
        converted = cls._optional_decimal(value)
        if converted is None:
            raise BrokerDataError(f"IBKR {label} is missing or invalid")
        return converted

    @classmethod
    def _required_positive_decimal(cls, value: object, label: str) -> Decimal:
        converted = cls._required_decimal(value, label)
        if converted <= 0:
            raise BrokerDataError(f"IBKR {label} must be positive")
        return converted

    @classmethod
    def _optional_integer(cls, *values: object) -> int | None:
        for value in values:
            converted = cls._optional_decimal(value, nonnegative=True)
            if converted is None or converted != converted.to_integral_value():
                continue
            return int(converted)
        return None

    async def _qualify(self, contracts: Sequence[Contract]) -> tuple[Contract, ...]:
        try:
            results = await self._client.qualifyContractsAsync(*contracts)
        except Exception as error:
            raise BrokerDataError("IBKR contract qualification failed") from error
        if len(results) != len(contracts):
            raise BrokerDataError("IBKR returned an incomplete contract-qualification result")
        qualified: list[Contract] = []
        for result in results:
            if result is None or isinstance(result, list):
                raise BrokerDataError("IBKR contract qualification was missing or ambiguous")
            if result.conId <= 0:
                raise BrokerDataError("IBKR qualified contract has no valid identifier")
            qualified.append(result)
        return tuple(qualified)

    @staticmethod
    def _underlying_from_ib(contract: Contract) -> UnderlyingContract:
        if contract.conId <= 0 or contract.secType != SecurityType.STOCK.value:
            raise BrokerDataError("IBKR stock contract is incomplete or unsupported")
        if not contract.symbol:
            raise BrokerDataError("IBKR stock contract is missing its symbol")
        return UnderlyingContract(
            con_id=contract.conId,
            symbol=contract.symbol,
            exchange=contract.exchange,
            primary_exchange=contract.primaryExchange or None,
            currency=contract.currency,
        )

    async def _instrument_from_ib(self, contract: Contract) -> Instrument:
        cached = self._domain_contracts.get(contract.conId)
        if cached is not None:
            return cached
        if contract.secType == SecurityType.STOCK.value:
            mapped: Instrument = self._underlying_from_ib(contract)
        elif contract.secType == SecurityType.OPTION.value:
            mapped = self._option_from_ib(
                contract,
                min_tick=await self._option_min_tick(contract),
            )
        else:
            raise BrokerDataError(
                f"Chronos cannot safely map IBKR security type {contract.secType or 'UNKNOWN'}"
            )
        self._cache_contract(contract, mapped)
        return mapped

    @staticmethod
    def _option_request(specification: OptionContractSpec) -> Contract:
        return IBOption(
            symbol=specification.symbol,
            lastTradeDateOrContractMonth=specification.expiration.strftime("%Y%m%d"),
            strike=float(specification.strike),
            right=specification.right.value,
            exchange=specification.exchange,
            multiplier=str(specification.multiplier),
            currency=specification.currency,
            tradingClass=specification.trading_class,
        )

    async def _option_min_tick(self, contract: Contract) -> Decimal:
        try:
            details = await self._client.reqContractDetailsAsync(contract)
        except Exception as error:
            raise BrokerDataError("IBKR option contract details are unavailable") from error
        exact = [
            detail
            for detail in details
            if detail.contract is not None and detail.contract.conId == contract.conId
        ]
        if len(exact) != 1:
            raise BrokerDataError("IBKR option contract details are missing or ambiguous")
        return self._required_positive_decimal(exact[0].minTick, "option minimum tick")

    @classmethod
    def _option_from_ib(cls, contract: Contract, *, min_tick: Decimal) -> OptionContract:
        if contract.conId <= 0 or contract.secType != SecurityType.OPTION.value:
            raise BrokerDataError("IBKR option contract is incomplete or unsupported")
        if not contract.symbol or not contract.localSymbol or not contract.tradingClass:
            raise BrokerDataError("IBKR option contract is missing identifying metadata")
        try:
            right = OptionRight(contract.right.upper()[0])
        except (ValueError, IndexError) as error:
            raise BrokerDataError("IBKR option contract has an invalid right") from error
        return OptionContract(
            con_id=contract.conId,
            symbol=contract.symbol,
            expiration=cls._expiration(contract.lastTradeDateOrContractMonth),
            strike=cls._required_positive_decimal(contract.strike, "option strike"),
            right=right,
            exchange=contract.exchange,
            currency=contract.currency,
            multiplier=cls._required_positive_decimal(contract.multiplier, "option multiplier"),
            trading_class=contract.tradingClass,
            local_symbol=contract.localSymbol,
            min_tick=min_tick,
        )

    @staticmethod
    def _expiration(value: str) -> date:
        normalized = value.strip()[:8]
        if len(normalized) != 8 or not normalized.isdigit():
            raise BrokerDataError("IBKR option expiration is missing or invalid")
        try:
            return date.fromisoformat(f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:]}")
        except ValueError as error:
            raise BrokerDataError("IBKR option expiration is invalid") from error

    def _cache_contract(self, ib_contract: Contract, domain_contract: Instrument) -> None:
        self._ib_contracts[domain_contract.con_id] = ib_contract
        self._domain_contracts[domain_contract.con_id] = domain_contract

    def _ib_contract_for(self, contract: Instrument) -> Contract:
        cached = self._ib_contracts.get(contract.con_id)
        if cached is not None:
            return cached
        if isinstance(contract, UnderlyingContract):
            converted: Contract = Stock(
                symbol=contract.symbol,
                exchange=contract.exchange,
                currency=contract.currency,
                conId=contract.con_id,
                primaryExchange=contract.primary_exchange or "",
            )
        else:
            converted = IBOption(
                symbol=contract.symbol,
                lastTradeDateOrContractMonth=contract.expiration.strftime("%Y%m%d"),
                strike=float(contract.strike),
                right=contract.right.value,
                exchange=contract.exchange,
                multiplier=str(contract.multiplier),
                currency=contract.currency,
                conId=contract.con_id,
                localSymbol=contract.local_symbol,
                tradingClass=contract.trading_class,
            )
        self._cache_contract(converted, contract)
        return converted

    async def _request_quotes(
        self,
        contracts: Sequence[Instrument],
    ) -> tuple[MarketQuote, ...]:
        self._require_connection()
        started: list[
            tuple[
                Instrument,
                Ticker,
                Callable[[Ticker], None],
                asyncio.Future[datetime],
            ]
        ] = []
        try:
            for domain_contract in contracts:
                if domain_contract.con_id in self._active_market_data:
                    raise BrokerDataError(
                        "Market data is already active for this contract; cancel it first"
                    )
                ib_contract = self._ib_contract_for(domain_contract)
                generic_ticks = (
                    _OPTION_GENERIC_TICKS if isinstance(domain_contract, OptionContract) else ""
                )
                requested_at = self._now()
                self._starting_market_data.add(domain_contract.con_id)
                try:
                    ticker = self._client.reqMktData(
                        ib_contract,
                        genericTickList=generic_ticks,
                        snapshot=False,
                        regulatorySnapshot=False,
                    )
                finally:
                    self._starting_market_data.discard(domain_contract.con_id)
                self._clear_reused_ticker(ticker)
                waiter = asyncio.get_running_loop().create_future()

                def on_update(
                    updated: Ticker,
                    *,
                    contract_id: int = domain_contract.con_id,
                    request_started_at: datetime = requested_at,
                ) -> None:
                    if not self._has_price_update(updated):
                        return
                    observed_at = self._now()
                    if observed_at < request_started_at:
                        return
                    pending = self._market_data_waiters.get(contract_id)
                    if pending is not None and not pending.done():
                        pending.set_result(observed_at)

                ticker.updateEvent.connect(on_update, keep_ref=True)
                self._market_data_waiters[domain_contract.con_id] = waiter
                self._active_market_data[domain_contract.con_id] = ib_contract
                started.append((domain_contract, ticker, on_update, waiter))
                self._logger.debug(
                    "Started Chronos market data",
                    extra={
                        "event": "market_data_requested",
                        "contract_id": domain_contract.con_id,
                    },
                )
            contract_ids = tuple(contract.con_id for contract, *_rest in started)
            self._raise_market_data_error(contract_ids)
            try:
                observed_times = await asyncio.wait_for(
                    asyncio.gather(*(waiter for *_prefix, waiter in started)),
                    timeout=self._quote_timeout_seconds,
                )
            except TimeoutError as error:
                raise MarketDataUnavailableError(
                    "IBKR did not provide a current price update before the bounded timeout",
                    contract_ids=contract_ids,
                ) from error
            if self._quote_settle_seconds:
                await asyncio.sleep(self._quote_settle_seconds)
            self._raise_market_data_error(contract_ids)
            quotes = tuple(
                self._quote_from_ticker(contract, ticker, observed_at)
                for (contract, ticker, _handler, _waiter), observed_at in zip(
                    started,
                    observed_times,
                    strict=True,
                )
            )
        except BaseException as request_error:
            try:
                self._cancel_started_market_data(
                    tuple(contract.con_id for contract, *_rest in started)
                )
            except BrokerDataError as cleanup_error:
                raise cleanup_error from request_error
            raise
        finally:
            for contract, ticker, handler, waiter in started:
                ticker.updateEvent.disconnect(handler)
                current = self._market_data_waiters.get(contract.con_id)
                if current is waiter:
                    self._market_data_waiters.pop(contract.con_id, None)
                if not waiter.done():
                    waiter.cancel()

        self._data_quality = self._worst_quality(quote.data_quality for quote in quotes)
        self._last_sync = self._now()
        return quotes

    @staticmethod
    def _clear_reused_ticker(ticker: Ticker) -> None:
        """Remove values cached by ib_async before waiting for this request's update."""

        missing = float("nan")
        ticker.time = None
        ticker.marketDataType = 0
        ticker.bid = missing
        ticker.ask = missing
        ticker.last = missing
        ticker.close = missing
        ticker.volume = missing
        ticker.openInterest = missing
        ticker.putVolume = missing
        ticker.callVolume = missing
        ticker.putOpenInterest = missing
        ticker.callOpenInterest = missing
        ticker.modelGreeks = None
        ticker.ticks.clear()

    @staticmethod
    def _has_price_update(ticker: Ticker) -> bool:
        return any(tick.tickType in _PRICE_TICK_TYPES for tick in ticker.ticks)

    def _cancel_started_market_data(self, contract_ids: Sequence[int]) -> None:
        failures: list[int] = []
        for contract_id in contract_ids:
            contract = self._active_market_data.get(contract_id)
            if contract is None:
                continue
            try:
                cancelled = self._client.cancelMktData(contract)
            except Exception:
                failures.append(contract_id)
                continue
            if not cancelled:
                failures.append(contract_id)
                continue
            self._active_market_data.pop(contract_id, None)
        if failures:
            self._logger.error(
                "IBKR cleanup of a failed market-data request did not complete",
                extra={"event": "market_data_cleanup_failed"},
            )
            raise BrokerDataError("IBKR could not clean up one or more failed market-data requests")

    def _on_ib_error(
        self,
        _request_id: int,
        error_code: int,
        _error_message: str,
        contract: Contract | None,
    ) -> None:
        if error_code in _MARKET_DATA_PACING_CODES:
            error: BrokerDataError = MarketDataPacingError(
                "IBKR reported a market-data pacing violation"
            )
        elif error_code in _MARKET_DATA_PERMISSION_CODES:
            error = MarketDataPermissionError(
                "IBKR market-data permissions are unavailable for this request"
            )
        elif error_code in _MARKET_DATA_CAPACITY_CODES:
            error = MarketDataUnavailableError("IBKR market-data line capacity is exhausted")
        else:
            return

        contract_id = contract.conId if contract is not None else 0
        if contract_id > 0:
            if (
                contract_id not in self._active_market_data
                and contract_id not in self._starting_market_data
            ):
                self._logger.debug(
                    "Ignoring a late error for an inactive market-data request",
                    extra={
                        "event": "late_market_data_error_ignored",
                        "error_code": error_code,
                        "contract_id": contract_id,
                    },
                )
                return
            waiter = self._market_data_waiters.get(contract_id)
            if waiter is not None and not waiter.done():
                waiter.set_exception(error)
            else:
                self._market_data_errors[contract_id] = error
        else:
            pending_waiters = tuple(
                waiter for waiter in self._market_data_waiters.values() if not waiter.done()
            )
            if pending_waiters:
                for waiter in pending_waiters:
                    waiter.set_exception(error)
            else:
                self._global_market_data_error = error
        self._logger.warning(
            "IBKR rejected a market-data request",
            extra={
                "event": "market_data_rejected",
                "error_code": error_code,
                "contract_id": contract_id or None,
            },
        )

    def _raise_market_data_error(self, contract_ids: Sequence[int]) -> None:
        errors: list[BrokerDataError] = []
        if self._global_market_data_error is not None:
            errors.append(self._global_market_data_error)
            self._global_market_data_error = None
        for contract_id in contract_ids:
            error = self._market_data_errors.pop(contract_id, None)
            if error is not None:
                errors.append(error)
        if not errors:
            return
        for error_type in (
            MarketDataPermissionError,
            MarketDataPacingError,
            MarketDataUnavailableError,
        ):
            matching = next((error for error in errors if isinstance(error, error_type)), None)
            if matching is not None:
                raise matching
        raise errors[0]

    def _quote_from_ticker(
        self,
        contract: Instrument,
        ticker: Ticker,
        observed_at: datetime,
    ) -> MarketQuote:
        quality = self._market_data_quality(ticker.marketDataType)
        timestamp = self._aware_utc(observed_at, "IBKR quote observation timestamp")

        if isinstance(contract, OptionContract):
            volume = self._optional_integer(
                ticker.volume,
                ticker.callVolume if contract.right is OptionRight.CALL else ticker.putVolume,
            )
            open_interest = self._optional_integer(
                ticker.openInterest,
                ticker.callOpenInterest
                if contract.right is OptionRight.CALL
                else ticker.putOpenInterest,
            )
        else:
            volume = self._optional_integer(ticker.volume)
            open_interest = None
        return MarketQuote(
            contract=contract,
            timestamp=timestamp,
            data_quality=quality,
            bid=self._optional_decimal(ticker.bid, nonnegative=True),
            ask=self._optional_decimal(ticker.ask, nonnegative=True),
            last=self._optional_decimal(ticker.last, nonnegative=True),
            close=self._optional_decimal(ticker.close, nonnegative=True),
            volume=volume,
            open_interest=open_interest,
            greeks=self._model_greeks(ticker),
        )

    @classmethod
    def _model_greeks(cls, ticker: Ticker) -> ModelGreeks | None:
        if ticker.modelGreeks is None:
            return None
        delta = cls._optional_greek(ticker.modelGreeks.delta)
        gamma = cls._optional_greek(ticker.modelGreeks.gamma)
        theta = cls._optional_greek(ticker.modelGreeks.theta)
        implied_volatility = cls._optional_decimal(
            ticker.modelGreeks.impliedVol,
            nonnegative=True,
        )
        if all(value is None for value in (delta, gamma, theta, implied_volatility)):
            return None
        return ModelGreeks(
            delta=delta,
            gamma=gamma,
            theta=theta,
            implied_volatility=implied_volatility,
        )

    @classmethod
    def _optional_greek(cls, value: object) -> Decimal | None:
        converted = cls._optional_decimal(value)
        return None if converted == Decimal("-2") else converted

    @staticmethod
    def _market_data_quality(market_data_type: int) -> DataQuality:
        return {
            1: DataQuality.LIVE,
            2: DataQuality.FROZEN,
            3: DataQuality.DELAYED,
            # IBKR calls type 4 delayed-frozen. Chronos has no combined
            # value, so choose the more restrictive delayed classification.
            4: DataQuality.DELAYED,
        }.get(market_data_type, DataQuality.UNKNOWN)

    @staticmethod
    def _worst_quality(qualities: Iterable[DataQuality]) -> DataQuality:
        priority = {
            DataQuality.DEMO: 0,
            DataQuality.LIVE: 1,
            DataQuality.FROZEN: 2,
            DataQuality.DELAYED: 3,
            DataQuality.UNKNOWN: 4,
            DataQuality.STALE: 5,
        }
        values = tuple(qualities)
        return max(values, key=priority.__getitem__) if values else DataQuality.UNKNOWN

    @classmethod
    def _commission(cls, fill: Fill) -> Decimal | None:
        report = fill.commissionReport
        if not report.execId or report.execId != fill.execution.execId:
            return None
        return cls._optional_decimal(report.commission, nonnegative=True)

    @classmethod
    def _execution_timestamp(cls, fill: Fill) -> datetime:
        for candidate in (fill.time, fill.execution.time):
            if candidate.year <= 1970:
                continue
            return cls._aware_utc(candidate, "IBKR execution timestamp")
        raise BrokerDataError("IBKR execution timestamp is missing")

    @staticmethod
    def _order_side(value: str) -> OrderSide:
        normalized = value.strip().upper()
        if normalized in {"BUY", "BOT"}:
            return OrderSide.BUY
        if normalized in {"SELL", "SLD"}:
            return OrderSide.SELL
        raise BrokerDataError("IBKR order or execution has an unknown side")

    @staticmethod
    def _permanent_id(trade: Trade) -> int | None:
        permanent_id = trade.order.permId or trade.orderStatus.permId
        return permanent_id if permanent_id > 0 else None

    @staticmethod
    def _order_lifecycle(status: str, filled: Decimal) -> OrderLifecycle:
        normalized = status.strip()
        if normalized == "Filled":
            return OrderLifecycle.FILLED
        if normalized in {"Cancelled", "ApiCancelled"}:
            return OrderLifecycle.CANCELLED
        if normalized == "Inactive":
            return OrderLifecycle.REJECTED
        if normalized in {
            "PendingSubmit",
            "PendingCancel",
            "ApiPending",
            "PreSubmitted",
            "Submitted",
            "ValidationError",
            "ApiUpdate",
        }:
            if filled > 0:
                return OrderLifecycle.PARTIALLY_FILLED
            return OrderLifecycle.SUBMITTED
        raise BrokerDataError("IBKR open order has an unknown lifecycle status")
