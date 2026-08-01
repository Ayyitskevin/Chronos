"""Read-only Interactive Brokers adapter built on :mod:`ib_async`.

Milestone 2 deliberately exposes portfolio and market-data reads only. Every
order method fails closed until the paper-order workflow and its guardrails are
implemented in a later milestone.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from functools import partial
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
    BrokerSendGuard,
    OptionChainResponse,
)
from chronos.broker.market_data import (
    MarketDataCancellationError,
    MarketDataPacingError,
    MarketDataPermissionError,
    MarketDataUnavailableError,
)
from chronos.config.limits import (
    MAX_CANDIDATE_REQUEST_CONTRACTS,
    MAX_OPTION_CHAIN_EXPIRATIONS_PER_ROW,
    MAX_OPTION_CHAIN_ROWS,
    MAX_OPTION_CHAIN_STRIKES_PER_ROW,
    MAX_OPTION_MARKET_RULE_INCREMENTS,
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
    CryptoContract,
    Instrument,
    MarketQuote,
    ModelGreeks,
    OptionChainParameters,
    OptionContract,
    OptionContractSpec,
    OptionDeliverableFacts,
    OptionMarketRule,
    OptionPriceIncrement,
    OrderModification,
    OrderPreview,
    OrderRequest,
    OrderSubmission,
    UnderlyingContract,
)
from chronos.marketdata.bars import BarInterval, BarSeries
from chronos.services.option_deliverable import assess_standard_deliverable
from chronos.utils.logging import mask_account_id

_LOGGER = logging.getLogger("chronos.broker.ibkr")

_OPTION_GENERIC_TICKS = "100,101"
_IB_UNSET_DOUBLE = Decimal(str(UNSET_DOUBLE))
_MARKET_DATA_PACING_CODES = frozenset({100, 420})
_MARKET_DATA_CAPACITY_CODES = frozenset({101})
_MARKET_DATA_PERMISSION_CODES = frozenset({354, 10089, 10090, 10091, 10186, 10189, 10197})
_CONNECTION_UNCERTAIN_CODES = frozenset({1100, 1101, 1102, 1300, 2110})
_PRICE_TICK_TYPES = frozenset({1, 2, 4, 9, 66, 67, 68, 75})
_BID_TICK_TYPES = frozenset({1, 66})
_ASK_TICK_TYPES = frozenset({2, 67})
_VOLUME_TICK_TYPES = frozenset({8, 74})
_CALL_OPEN_INTEREST_TICK = 27
_PUT_OPEN_INTEREST_TICK = 28
_CALL_VOLUME_TICK = 29
_PUT_VOLUME_TICK = 30


def utc_now() -> datetime:
    """Return an aware UTC observation timestamp."""

    return datetime.now(tz=UTC)


def _attach_quote_observations(
    error: BaseException,
    quotes: Sequence[MarketQuote],
    *,
    contract_ids: Sequence[int],
    observed_at: datetime,
) -> BaseException:
    ordered = tuple(
        sorted(
            quotes,
            key=lambda quote: (
                quote.contract.con_id,
                quote.timestamp,
                quote.bid if quote.bid is not None else Decimal("-1"),
                quote.ask if quote.ask is not None else Decimal("-1"),
                quote.model_dump_json(),
            ),
        )
    )
    vars(error).update(
        observed_values=ordered[:MAX_CANDIDATE_REQUEST_CONTRACTS],
        observed_at=observed_at,
        truncated=len(ordered) > MAX_CANDIDATE_REQUEST_CONTRACTS,
        contract_ids=tuple(sorted(set(contract_ids))),
    )
    return error


def _attach_rule_observations(
    error: BaseException,
    observations: Sequence[object],
    *,
    contract_ids: Sequence[int],
    observed_at: datetime,
    truncated: bool = False,
    include_existing: bool = True,
) -> BaseException:
    existing = tuple(getattr(error, "observed_values", ())) if include_existing else ()

    def observation_key(value: object) -> tuple[object, ...]:
        payload = (
            value.model_dump_json() if hasattr(value, "model_dump_json") else type(value).__name__
        )
        return (
            type(value).__name__,
            int(getattr(value, "con_id", 0) or 0),
            getattr(value, "low_edge", Decimal("-1")),
            payload,
        )

    ordered = tuple(sorted((*existing, *observations), key=observation_key))
    limit = MAX_CANDIDATE_REQUEST_CONTRACTS + MAX_OPTION_MARKET_RULE_INCREMENTS
    existing_ids = tuple(getattr(error, "contract_ids", ()))
    vars(error).update(
        observed_values=ordered[:limit],
        observed_at=observed_at,
        truncated=(truncated or bool(getattr(error, "truncated", False)) or len(ordered) > limit),
        contract_ids=tuple(sorted(set((*existing_ids, *contract_ids)))),
    )
    return error


def _preferred_market_data_error(errors: Sequence[BaseException]) -> BaseException:
    for error_type in (
        MarketDataCancellationError,
        MarketDataPermissionError,
        MarketDataPacingError,
        MarketDataUnavailableError,
        BrokerDataError,
    ):
        matching = next((error for error in errors if isinstance(error, error_type)), None)
        if matching is not None:
            return matching
    return errors[0]


def _clone_market_data_error(error: BrokerDataError) -> BrokerDataError:
    if isinstance(error, MarketDataPermissionError):
        cloned: BrokerDataError = MarketDataPermissionError(str(error))
    elif isinstance(error, MarketDataPacingError):
        cloned = MarketDataPacingError(str(error))
    elif isinstance(error, MarketDataUnavailableError):
        cloned = MarketDataUnavailableError(
            str(error),
            contract_ids=getattr(error, "contract_ids", ()),
            observed_values=getattr(error, "observed_values", ()),
            observed_at=getattr(error, "observed_at", None),
            truncated=bool(getattr(error, "truncated", False)),
            chain_response=getattr(error, "chain_response", None),
        )
    else:
        cloned = BrokerDataError(str(error))
    vars(cloned).update(vars(error))
    return cloned


def _bounded_smallest(
    values: Iterable[object],
    *,
    limit: int,
    normalize: Callable[[object], object],
) -> tuple[tuple[object, ...], bool]:
    retained = heapq.nsmallest(limit + 1, (normalize(value) for value in values))
    return tuple(retained[:limit]), len(retained) > limit


def _ib_async_chain_key(chain: OptionChainParameters) -> tuple[object, ...]:
    return (
        chain.underlying_con_id,
        chain.exchange,
        chain.trading_class,
        chain.multiplier,
        chain.expirations,
        chain.strikes,
    )


@dataclass(frozen=True, slots=True)
class _OptionDetails:
    """The ``ContractDetails`` fields option qualification needs.

    Grouped rather than returned as a widening tuple because M11 added two of
    them, and the next reader should be able to tell which is which.
    """

    min_tick: Decimal
    underlying_con_id: int
    underlying_symbol: str
    underlying_security_type: str
    liquid_hours: str
    time_zone_id: str


IBErrorHandler = Callable[[int, int, str, Contract | None], None]


class _MarketRuleIncrement(Protocol):
    lowEdge: object
    increment: object


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

    async def reqMarketRuleAsync(self, marketRuleId: int) -> Sequence[object] | None: ...

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
        on_connection_uncertain: Callable[[str], object] | None = None,
    ) -> None:
        if quote_timeout_seconds <= 0:
            raise ValueError("quote_timeout_seconds must be positive")
        self._settings = settings
        self._client = client if client is not None else cast(IBClient, IB())
        self._clock = clock
        self._quote_timeout_seconds = quote_timeout_seconds
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
        self._option_evidence_error_waiters: set[asyncio.Future[BrokerDataError]] = set()
        self._option_evidence_scope_waiter: ContextVar[asyncio.Future[BrokerDataError] | None] = (
            ContextVar(f"chronos_option_evidence_waiter_{id(self)}", default=None)
        )
        self._logger = logging.getLogger("chronos.broker.ibkr")
        self._on_connection_uncertain = on_connection_uncertain
        self._client.errorEvent.connect(self._on_ib_error)
        connected_event = getattr(self._client, "connectedEvent", None)
        if connected_event is not None:
            connected_event.connect(self._on_connected)
        disconnected_event = getattr(self._client, "disconnectedEvent", None)
        if disconnected_event is not None:
            disconnected_event.connect(self._on_disconnected)

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
        self._option_evidence_error_waiters.clear()
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
        self._option_evidence_error_waiters.clear()
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
            commission = self._commission(fill)
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
                    commission=commission[0] if commission is not None else None,
                    commission_currency=commission[1] if commission is not None else None,
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
        try:
            contract = (await self._qualify((requested,)))[0]
        except BrokerDataError as error:
            qualified = cast(tuple[Contract, ...], getattr(error, "qualified_contracts", ()))
            if len(qualified) != 1:
                raise
            try:
                mapped = self._qualified_underlying(
                    qualified[0],
                    requested=requested,
                    symbol=normalized_symbol,
                )
            except BrokerDataError as evidence_error:
                raise error from evidence_error
            vars(error).update(
                observed_values=(mapped,),
                observed_at=self._now(),
                truncated=False,
                contract_ids=(),
            )
            raise
        mapped = self._qualified_underlying(
            contract,
            requested=requested,
            symbol=normalized_symbol,
        )
        self._cache_contract(contract, mapped)
        self._last_sync = self._now()
        return mapped

    async def qualify_crypto(self, symbol: str) -> CryptoContract:
        del symbol
        raise BrokerSafetyError(
            "the ib_async adapter does not support crypto; the official adapter "
            "is the crypto path (ADR-0010)"
        )

    async def option_chain_parameters(
        self,
        underlying: UnderlyingContract,
    ) -> OptionChainResponse:
        async with self._option_evidence_scope():
            response = await self._option_chain_parameters(underlying)
            if error := self._current_option_evidence_error():
                vars(error).update(
                    contract_ids=(underlying.con_id,),
                    observed_at=response.observed_at,
                    truncated=response.truncated,
                    chain_response=response,
                )
                raise error
            return response

    async def _option_chain_parameters(
        self,
        underlying: UnderlyingContract,
    ) -> OptionChainResponse:
        self._require_connection()
        classified_error: BrokerDataError | None = None
        try:
            chains = await self._await_option_evidence(
                lambda: self._client.reqSecDefOptParamsAsync(
                    underlying.symbol,
                    "",
                    SecurityType.STOCK.value,
                    underlying.con_id,
                )
            )
        except BrokerDataError as error:
            existing_ids = tuple(getattr(error, "contract_ids", ()))
            vars(error).update(
                contract_ids=tuple(sorted(set((*existing_ids, underlying.con_id)))),
                observed_at=getattr(error, "observed_at", None) or self._now(),
            )
            if "completed_result" not in vars(error):
                raise
            classified_error = error
            chains = cast(list[OptionChain], vars(error)["completed_result"])
        except Exception as error:
            raise BrokerDataError(
                f"IBKR option-chain metadata is unavailable for {underlying.symbol}"
            ) from error
        if not chains:
            raise BrokerDataError(f"IBKR returned no option chains for {underlying.symbol}")

        mapped: list[OptionChainParameters] = []
        truncated = False
        try:
            for chain in chains:
                if chain.underlyingConId != underlying.con_id:
                    raise BrokerDataError(
                        "IBKR option-chain metadata references another underlying"
                    )
                if not chain.tradingClass or not chain.exchange:
                    raise BrokerDataError("IBKR option-chain metadata is missing routing identity")
                raw_expirations, expiration_overflow = _bounded_smallest(
                    chain.expirations,
                    limit=MAX_OPTION_CHAIN_EXPIRATIONS_PER_ROW,
                    normalize=lambda value: self._expiration(str(value)),
                )
                raw_strikes, strike_overflow = _bounded_smallest(
                    chain.strikes,
                    limit=MAX_OPTION_CHAIN_STRIKES_PER_ROW,
                    normalize=lambda value: self._required_positive_decimal(
                        value, "option-chain strike"
                    ),
                )
                expirations = tuple(cast(date, value) for value in raw_expirations)
                strikes = tuple(cast(Decimal, value) for value in raw_strikes)
                truncated = truncated or expiration_overflow or strike_overflow
                if not expirations or not strikes:
                    raise BrokerDataError("IBKR option-chain metadata is incomplete")
                normalized = OptionChainParameters(
                    exchange=chain.exchange,
                    underlying_con_id=chain.underlyingConId,
                    trading_class=chain.tradingClass,
                    multiplier=self._required_positive_decimal(
                        chain.multiplier, "option-chain multiplier"
                    ),
                    expirations=expirations,
                    strikes=strikes,
                )
                if len(mapped) < MAX_OPTION_CHAIN_ROWS:
                    mapped.append(normalized)
                    continue
                truncated = True
                worst_index = max(
                    range(len(mapped)),
                    key=lambda index: _ib_async_chain_key(mapped[index]),
                )
                if _ib_async_chain_key(normalized) < _ib_async_chain_key(mapped[worst_index]):
                    mapped[worst_index] = normalized
        except (BrokerDataError, InvalidOperation, TypeError, ValueError) as parse_error:
            observed_at = self._now()
            response = OptionChainResponse(
                parameters=tuple(sorted(mapped, key=_ib_async_chain_key)),
                complete=True,
                truncated=truncated,
                completion_marker="securityDefinitionOptionParameterEnd",
                observed_at=observed_at,
                source="ib_async-v1",
            )
            chain_error = classified_error or MarketDataUnavailableError(
                f"IBKR option-chain metadata is malformed for {underlying.symbol}"
            )
            vars(chain_error).update(
                contract_ids=(underlying.con_id,),
                observed_at=observed_at,
                truncated=truncated,
                chain_response=response,
            )
            raise chain_error from parse_error
        observed_at = self._now()
        self._last_sync = observed_at
        response = OptionChainResponse(
            parameters=tuple(sorted(mapped, key=_ib_async_chain_key)),
            complete=True,
            truncated=truncated,
            completion_marker="securityDefinitionOptionParameterEnd",
            observed_at=observed_at,
            source="ib_async-v1",
        )
        if classified_error is not None:
            vars(classified_error).update(
                contract_ids=(underlying.con_id,),
                observed_at=observed_at,
                truncated=truncated,
                chain_response=response,
            )
            raise classified_error
        if truncated:
            raise MarketDataUnavailableError(
                f"IBKR option-chain metadata exceeded its hard evidence bound for "
                f"{underlying.symbol}",
                contract_ids=(underlying.con_id,),
                observed_at=observed_at,
                truncated=True,
                chain_response=response,
            )
        return response

    async def qualify_option_contracts(
        self,
        contracts: Sequence[OptionContractSpec],
    ) -> tuple[OptionContract, ...]:
        async with self._option_evidence_scope():
            qualified = await self._qualify_option_contracts(contracts)
            if error := self._current_option_evidence_error():
                ordered = tuple(
                    sorted(
                        qualified,
                        key=lambda item: (item.con_id, item.model_dump_json()),
                    )
                )
                vars(error).update(
                    observed_values=ordered[:MAX_CANDIDATE_REQUEST_CONTRACTS],
                    observed_at=self._now(),
                    truncated=len(ordered) > MAX_CANDIDATE_REQUEST_CONTRACTS,
                    contract_ids=(),
                )
                raise error
            return qualified

    async def _qualify_option_contracts(
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
            try:
                option = self._option_from_ib(
                    contract,
                    details=await self._option_details(contract),
                )
                if (
                    option.symbol != specification.symbol
                    or (
                        specification.underlying_con_id is not None
                        and option.underlying_con_id != specification.underlying_con_id
                    )
                    or option.expiration != specification.expiration
                    or option.strike != specification.strike
                    or option.right is not specification.right
                    or option.exchange != specification.exchange
                    or option.currency != specification.currency
                    or option.multiplier != specification.multiplier
                    or option.trading_class != specification.trading_class
                ):
                    raise BrokerDataError("IBKR qualified an option different from the request")
            except asyncio.CancelledError as cancellation:
                error = MarketDataUnavailableError(
                    "ib_async option qualification was interrupted",
                    contract_ids=(contract.conId,),
                    observed_values=tuple(mapped),
                    observed_at=self._now(),
                )
                raise error from cancellation
            except BrokerDataError as error:
                vars(error).update(
                    observed_values=tuple(
                        sorted(mapped, key=lambda item: (item.con_id, item.model_dump_json()))
                    )[:MAX_CANDIDATE_REQUEST_CONTRACTS],
                    observed_at=self._now(),
                    truncated=len(mapped) > MAX_CANDIDATE_REQUEST_CONTRACTS,
                    contract_ids=(contract.conId,),
                )
                raise
            self._cache_contract(contract, option)
            mapped.append(option)
        self._last_sync = self._now()
        return tuple(mapped)

    async def option_market_rules(
        self,
        contracts: Sequence[OptionContract],
    ) -> tuple[OptionMarketRule, ...]:
        """Read complete price-band schedules for exact qualified routes."""

        async with self._option_evidence_scope():
            rules = await self._option_market_rules(contracts)
            if error := self._current_option_evidence_error():
                _attach_rule_observations(
                    error,
                    rules,
                    contract_ids=(),
                    observed_at=self._now(),
                    include_existing=False,
                )
                raise error
            return rules

    async def _option_market_rules(
        self,
        contracts: Sequence[OptionContract],
    ) -> tuple[OptionMarketRule, ...]:
        self._require_connection()
        if len({option.con_id for option in contracts}) != len(contracts):
            raise BrokerDataError("option market-rule request contains duplicate conIds")

        ib_contracts: list[Contract] = []
        for option in contracts:
            ib_contract = self._ib_contracts.get(option.con_id)
            if ib_contract is None:
                raise BrokerDataError(
                    f"option conId {option.con_id} was not qualified in this broker session"
                )
            ib_contracts.append(ib_contract)
        try:
            detail_results = await asyncio.gather(
                *(
                    self._await_option_evidence(
                        partial(self._client.reqContractDetailsAsync, contract)
                    )
                    for contract in ib_contracts
                ),
                return_exceptions=True,
            )
        except asyncio.CancelledError as cancellation:
            raise MarketDataUnavailableError(
                "ib_async market-rule contract-detail batch was interrupted",
                contract_ids=tuple(option.con_id for option in contracts),
            ) from cancellation

        rule_ids_by_contract: dict[int, int] = {}
        for option, detail_result in zip(contracts, detail_results, strict=True):
            if isinstance(detail_result, BaseException):
                if isinstance(detail_result, BrokerDataError) and "completed_result" in vars(
                    detail_result
                ):
                    details = cast(list[ContractDetails], vars(detail_result)["completed_result"])
                elif isinstance(detail_result, BrokerDataError):
                    existing_ids = tuple(getattr(detail_result, "contract_ids", ()))
                    vars(detail_result).update(
                        contract_ids=tuple(sorted(set((*existing_ids, option.con_id)))),
                        observed_at=getattr(detail_result, "observed_at", None) or self._now(),
                    )
                    raise detail_result
                raise BrokerDataError(
                    "IBKR market-rule contract details are unavailable"
                ) from detail_result
            else:
                details = detail_result
            exact = [
                detail
                for detail in details
                if detail.contract is not None and detail.contract.conId == option.con_id
            ]
            if len(exact) != 1:
                raise BrokerDataError("IBKR market-rule contract details are missing or ambiguous")
            exchanges = tuple(
                value.strip().upper()
                for value in str(getattr(exact[0], "validExchanges", "") or "").split(",")
                if value.strip()
            )
            raw_ids = tuple(
                value.strip()
                for value in str(getattr(exact[0], "marketRuleIds", "") or "").split(",")
                if value.strip()
            )
            if len(exchanges) != len(raw_ids):
                raise BrokerDataError("IBKR market-rule routing metadata is incomplete")
            try:
                matching_ids = {
                    int(rule_id)
                    for exchange, rule_id in zip(exchanges, raw_ids, strict=True)
                    if exchange == option.exchange and int(rule_id) > 0
                }
            except ValueError as error:
                raise BrokerDataError("IBKR market-rule identifier is invalid") from error
            if len(matching_ids) != 1:
                raise BrokerDataError("IBKR market rule for the qualified route is ambiguous")
            rule_ids_by_contract[option.con_id] = matching_ids.pop()

        rule_ids = tuple(sorted(set(rule_ids_by_contract.values())))
        rule_tasks = {
            rule_id: asyncio.ensure_future(
                self._await_option_evidence(partial(self._client.reqMarketRuleAsync, rule_id))
            )
            for rule_id in rule_ids
        }
        interruption: asyncio.CancelledError | None = None
        rule_results: list[object]
        try:
            rule_results = list(
                await asyncio.gather(
                    *rule_tasks.values(),
                    return_exceptions=True,
                )
            )
            result_rule_ids = rule_ids
        except asyncio.CancelledError as cancellation:
            interruption = cancellation
            completed_results: list[object] = []
            completed_rule_ids: list[int] = []
            for rule_id, task in rule_tasks.items():
                if not task.done() or task.cancelled():
                    continue
                completed_rule_ids.append(rule_id)
                try:
                    completed_results.append(task.result())
                except BaseException as error:
                    completed_results.append(error)
            rule_results = completed_results
            result_rule_ids = tuple(completed_rule_ids)
        schedules: dict[int, tuple[OptionPriceIncrement, ...]] = {}
        rule_errors: list[tuple[int, BaseException]] = []
        for rule_id, rule_result in zip(result_rule_ids, rule_results, strict=True):
            simultaneous_error: BrokerDataError | None = None
            if isinstance(rule_result, BaseException):
                if isinstance(rule_result, BrokerDataError) and "completed_result" in vars(
                    rule_result
                ):
                    simultaneous_error = rule_result
                    rule_result = vars(rule_result)["completed_result"]
                elif isinstance(rule_result, BrokerDataError):
                    result_error: BaseException = rule_result
                else:
                    result_error = BrokerDataError(f"IBKR market rule {rule_id} is unavailable")
                    result_error.__cause__ = rule_result
                if simultaneous_error is None:
                    rule_errors.append((rule_id, result_error))
                    continue
            if rule_result is None:
                # ib_async returns None when its internal market-rule timeout
                # expires; that is missing evidence, never an empty schedule.
                rule_errors.append(
                    (
                        rule_id,
                        MarketDataUnavailableError(f"IBKR market rule {rule_id} did not complete"),
                    )
                )
                if simultaneous_error is not None:
                    rule_errors.append((rule_id, simultaneous_error))
                continue
            parsed_items: list[OptionPriceIncrement] = []
            truncated = False
            try:
                for index, item in enumerate(cast(Iterable[object], rule_result)):
                    if index >= MAX_OPTION_MARKET_RULE_INCREMENTS:
                        truncated = True
                        raise ValueError(
                            "the price increment schedule exceeded its hard evidence bound"
                        )
                    raw_item = cast(_MarketRuleIncrement, item)
                    low_edge = raw_item.lowEdge
                    increment = raw_item.increment
                    if low_edge is None or increment is None:
                        raise ValueError("market-rule row is missing a required field")
                    parsed_items.append(
                        OptionPriceIncrement(
                            low_edge=Decimal(str(low_edge)),
                            increment=Decimal(str(increment)),
                        )
                    )
                if not parsed_items:
                    raise ValueError("the response contained no price increments")
                parsed = tuple(sorted(parsed_items, key=lambda item: item.low_edge))
                # Constructing the rule here validates the whole schedule (zero
                # first edge, unique increasing bands) before any candidate can
                # consume it.
                representative = next(
                    option for option in contracts if rule_ids_by_contract[option.con_id] == rule_id
                )
                validated = OptionMarketRule(
                    con_id=representative.con_id,
                    exchange=representative.exchange,
                    market_rule_id=rule_id,
                    price_increments=parsed,
                    source="ibkr-tws-market-rule-v1",
                )
            except (AttributeError, InvalidOperation, TypeError, ValueError) as parse_error:
                malformed = BrokerDataError(f"IBKR market rule {rule_id} is malformed")
                malformed.__cause__ = parse_error
                contract_ids = tuple(
                    option.con_id
                    for option in contracts
                    if rule_ids_by_contract[option.con_id] == rule_id
                )
                _attach_rule_observations(
                    malformed,
                    parsed_items,
                    contract_ids=contract_ids,
                    observed_at=self._now(),
                    truncated=truncated,
                    include_existing=False,
                )
                rule_errors.append((rule_id, malformed))
                if simultaneous_error is not None:
                    rule_errors.append((rule_id, simultaneous_error))
                continue
            schedules[rule_id] = validated.price_increments
            if simultaneous_error is not None:
                rule_errors.append((rule_id, simultaneous_error))

        complete_rules = tuple(
            OptionMarketRule(
                con_id=option.con_id,
                exchange=option.exchange,
                market_rule_id=rule_ids_by_contract[option.con_id],
                price_increments=schedules[rule_ids_by_contract[option.con_id]],
                source="ibkr-tws-market-rule-v1",
            )
            for option in contracts
            if rule_ids_by_contract[option.con_id] in schedules
        )
        if interruption is not None:
            missing_rule_ids = set(rule_ids) - set(schedules)
            fallback = MarketDataUnavailableError(
                "ib_async market-rule batch was interrupted",
                contract_ids=tuple(
                    option.con_id
                    for option in contracts
                    if rule_ids_by_contract[option.con_id] in missing_rule_ids
                ),
            )
            preferred = _preferred_market_data_error(
                (*tuple(error for _rule_id, error in rule_errors), fallback)
            )
            retained = tuple(
                observation
                for _rule_id, error in rule_errors
                for observation in getattr(error, "observed_values", ())
            )
            _attach_rule_observations(
                preferred,
                (*complete_rules, *retained),
                contract_ids=tuple(
                    option.con_id
                    for option in contracts
                    if rule_ids_by_contract[option.con_id] in missing_rule_ids
                ),
                observed_at=self._now(),
                truncated=any(
                    bool(getattr(error, "truncated", False)) for _rule_id, error in rule_errors
                ),
                include_existing=False,
            )
            vars(preferred).update(
                request_completed=False,
                failure_kind="interrupted",
            )
            raise preferred from interruption
        if rule_errors:
            preferred = _preferred_market_data_error(
                tuple(error for _rule_id, error in rule_errors)
            )
            failed_rule_ids = {
                rule_id for rule_id, _error in rule_errors if rule_id not in schedules
            }
            failed_contract_ids = tuple(
                option.con_id
                for option in contracts
                if rule_ids_by_contract[option.con_id] in failed_rule_ids
            )
            retained = tuple(
                observation
                for _rule_id, error in rule_errors
                for observation in getattr(error, "observed_values", ())
            )
            _attach_rule_observations(
                preferred,
                (*complete_rules, *retained),
                contract_ids=failed_contract_ids,
                observed_at=self._now(),
                truncated=any(
                    bool(getattr(error, "truncated", False)) for _rule_id, error in rule_errors
                ),
                include_existing=False,
            )
            raise preferred

        self._last_sync = self._now()
        return complete_rules

    async def option_deliverable_facts(
        self,
        contracts: Sequence[OptionContract],
    ) -> tuple[OptionDeliverableFacts, ...]:
        """Return explicit UNKNOWN until a schedule source exists."""

        self._require_connection()
        self._raise_market_data_error(tuple(contract.con_id for contract in contracts))
        return tuple(
            OptionDeliverableFacts(
                con_id=contract.con_id,
                authoritative=False,
                source="ibkr-tws-no-deliverable-schedule-v1",
            )
            for contract in contracts
        )

    async def request_underlying_quote(
        self,
        contract: UnderlyingContract,
    ) -> MarketQuote:
        return (await self._request_quotes((contract,)))[0]

    async def request_crypto_quote(
        self,
        contract: CryptoContract,
    ) -> MarketQuote:
        del contract
        raise BrokerSafetyError(
            "the ib_async adapter does not support crypto; the official adapter "
            "is the crypto path (ADR-0010)"
        )

    async def request_option_quotes(
        self,
        contracts: Sequence[OptionContract],
    ) -> tuple[MarketQuote, ...]:
        if not contracts:
            return ()
        return await self._request_quotes(tuple(contracts))

    async def historical_bars(
        self,
        contract: UnderlyingContract,
        *,
        interval: BarInterval = BarInterval.DAY_1,
        lookback_days: int = 180,
    ) -> BarSeries:
        """Refused here, the same way crypto is, and for the same reason.

        ``official_ibkr`` is the production adapter and the one whose historical
        path is wired and tested; this one is the optional secondary. Adding a
        second bar implementation would mean two request-pacing behaviours and
        two parsers against a gateway neither can be verified against from here,
        and a chart that silently differed by adapter is worse than one that says
        which adapter it needs.
        """

        del contract, interval, lookback_days
        raise BrokerSafetyError(
            "the ib_async adapter does not serve historical bars; the official "
            "adapter is the historical path (BROKER_ADAPTER=official_ibkr)"
        )

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
            raise MarketDataCancellationError(
                "IBKR could not issue a cancellation request for one or more Chronos subscriptions",
                contract_ids=tuple(failures),
                observed_at=self._now(),
            )

    async def preview_order(self, request: OrderRequest) -> OrderPreview:
        del request
        raise BrokerSafetyError("IBKR order previews are disabled in read-only Milestone 2")

    async def submit_order(
        self,
        request: OrderRequest,
        *,
        send_guard: BrokerSendGuard | None = None,
    ) -> OrderSubmission:
        del request, send_guard
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
        classified_error: BrokerDataError | None = None
        try:
            results = await self._await_option_evidence(
                lambda: self._client.qualifyContractsAsync(*contracts)
            )
        except BrokerDataError as error:
            if "completed_result" not in vars(error):
                raise
            classified_error = error
            results = cast(
                list[Contract | list[Contract | None] | None],
                vars(error)["completed_result"],
            )
        except Exception as error:
            raise BrokerDataError("IBKR contract qualification failed") from error
        try:
            if len(results) != len(contracts):
                raise BrokerDataError("IBKR returned an incomplete contract-qualification result")
            qualified: list[Contract] = []
            for result in results:
                if result is None or isinstance(result, list):
                    raise BrokerDataError("IBKR contract qualification was missing or ambiguous")
                if result.conId <= 0:
                    raise BrokerDataError("IBKR qualified contract has no valid identifier")
                qualified.append(result)
        except BrokerDataError as evidence_error:
            if classified_error is not None:
                raise classified_error from evidence_error
            raise
        if classified_error is not None:
            vars(classified_error).update(
                qualified_contracts=tuple(qualified),
                contract_ids=tuple(sorted(contract.conId for contract in qualified)),
                observed_at=getattr(classified_error, "observed_at", None) or self._now(),
            )
            raise classified_error
        return tuple(qualified)

    @classmethod
    def _qualified_underlying(
        cls,
        contract: Contract,
        *,
        requested: Contract,
        symbol: str,
    ) -> UnderlyingContract:
        if (
            contract.secType != SecurityType.STOCK.value
            or contract.symbol != symbol
            or contract.exchange != requested.exchange
            or contract.currency != requested.currency
        ):
            raise BrokerDataError("IBKR qualified an unexpected underlying contract")
        return cls._underlying_from_ib(contract)

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
                details=await self._option_details(contract),
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

    async def _option_details(self, contract: Contract) -> _OptionDetails:
        try:
            details = await self._await_option_evidence(
                lambda: self._client.reqContractDetailsAsync(contract)
            )
        except BrokerDataError:
            raise
        except Exception as error:
            raise BrokerDataError("IBKR option contract details are unavailable") from error
        exact = [
            detail
            for detail in details
            if detail.contract is not None and detail.contract.conId == contract.conId
        ]
        if len(exact) != 1:
            raise BrokerDataError("IBKR option contract details are missing or ambiguous")
        underlying_con_id = exact[0].underConId
        if underlying_con_id <= 0:
            raise BrokerDataError(
                "IBKR option contract details are missing the underlying contract identifier"
            )
        return _OptionDetails(
            min_tick=self._required_positive_decimal(exact[0].minTick, "option minimum tick"),
            underlying_con_id=underlying_con_id,
            # Absent rather than raising: these feed the deliverable screen,
            # which refuses on absence anyway (M11, R-27). A gateway that answers
            # without them yields an unverified contract, not an exception.
            underlying_symbol=str(getattr(exact[0], "underSymbol", "") or ""),
            underlying_security_type=str(getattr(exact[0], "underSecType", "") or ""),
            liquid_hours=str(getattr(exact[0], "liquidHours", "") or ""),
            time_zone_id=str(getattr(exact[0], "timeZoneId", "") or ""),
        )

    @classmethod
    def _option_from_ib(
        cls,
        contract: Contract,
        *,
        details: _OptionDetails,
    ) -> OptionContract:
        if contract.conId <= 0 or contract.secType != SecurityType.OPTION.value:
            raise BrokerDataError("IBKR option contract is incomplete or unsupported")
        if not contract.symbol or not contract.localSymbol or not contract.tradingClass:
            raise BrokerDataError("IBKR option contract is missing identifying metadata")
        try:
            right = OptionRight(contract.right.upper()[0])
        except (ValueError, IndexError) as error:
            raise BrokerDataError("IBKR option contract has an invalid right") from error
        multiplier = cls._required_positive_decimal(contract.multiplier, "option multiplier")
        option = OptionContract(
            con_id=contract.conId,
            symbol=contract.symbol,
            underlying_con_id=details.underlying_con_id,
            expiration=cls._expiration(contract.lastTradeDateOrContractMonth),
            strike=cls._required_positive_decimal(contract.strike, "option strike"),
            right=right,
            exchange=contract.exchange,
            currency=contract.currency,
            multiplier=multiplier,
            trading_class=contract.tradingClass,
            local_symbol=contract.localSymbol,
            liquid_hours=details.liquid_hours,
            time_zone_id=details.time_zone_id,
            min_tick=details.min_tick,
        )
        # M11 (R-27). An option that fails the screen is returned exactly as it
        # was built — unverified, and refused by the risk engine, which is what
        # happened to every option on this adapter before M11.
        assessment = assess_standard_deliverable(
            symbol=option.symbol,
            trading_class=option.trading_class,
            local_symbol=option.local_symbol,
            multiplier=option.multiplier,
            underlying_con_id=details.underlying_con_id,
            underlying_symbol=details.underlying_symbol,
            underlying_security_type=details.underlying_security_type,
        )
        if not assessment.standard:
            _LOGGER.warning(
                "Option %s refused a standard deliverable: %s",
                option.local_symbol,
                "; ".join(assessment.reasons),
                extra={"event": "option_deliverable_not_standard"},
            )
            return option
        return option.model_copy(
            update={"deliverable_shares": multiplier, "deliverable_verified": True}
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
        elif isinstance(contract, OptionContract):
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
        else:
            raise BrokerDataError(
                "the ib_async adapter does not support crypto contracts; the "
                "official adapter is the crypto path (ADR-0010)"
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

        def completed_quotes() -> tuple[MarketQuote, ...]:
            retained: list[MarketQuote] = []
            for domain_contract, ticker, _handler, waiter in started:
                if not waiter.done() or waiter.cancelled():
                    continue
                try:
                    observed_at = waiter.result()
                except BaseException:
                    continue
                try:
                    retained.append(self._quote_from_ticker(domain_contract, ticker, observed_at))
                except Exception:
                    # Evidence collection must not mask the primary request
                    # failure or prevent cancellation cleanup.
                    continue
            return tuple(retained)

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
                    expected_contract: Instrument = domain_contract,
                    request_started_at: datetime = requested_at,
                ) -> None:
                    if not self._quote_is_complete(expected_contract, updated):
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
                timeout_error = MarketDataUnavailableError(
                    "IBKR did not provide every required quote callback before the bounded timeout",
                    contract_ids=tuple(
                        contract_id
                        for contract_id, waiter in (
                            (contract.con_id, waiter) for contract, *_prefix, waiter in started
                        )
                        if waiter.cancelled() or not waiter.done()
                    ),
                )
                _attach_quote_observations(
                    timeout_error,
                    completed_quotes(),
                    contract_ids=timeout_error.contract_ids,
                    observed_at=self._now(),
                )
                raise timeout_error from error
            self._raise_market_data_error(contract_ids)
            quotes = tuple(
                self._quote_from_ticker(contract, ticker, observed_at)
                for (contract, ticker, _handler, _waiter), observed_at in zip(
                    started,
                    observed_times,
                    strict=True,
                )
            )
        except BaseException as request_failure:
            request_cancellation = (
                request_failure if isinstance(request_failure, asyncio.CancelledError) else None
            )
            request_error: BaseException = (
                MarketDataUnavailableError(
                    "ib_async option-quote batch was interrupted before completion"
                )
                if request_cancellation is not None
                else request_failure
            )
            observed = completed_quotes()
            if isinstance(request_error, BrokerDataError):
                observed_ids = {quote.contract.con_id for quote in observed}
                _attach_quote_observations(
                    request_error,
                    observed,
                    contract_ids=tuple(
                        contract.con_id
                        for contract, *_rest in started
                        if contract.con_id not in observed_ids
                    ),
                    observed_at=self._now(),
                )
            try:
                self._cancel_started_market_data(
                    tuple(contract.con_id for contract, *_rest in started)
                )
            except BrokerDataError as cleanup_error:
                _attach_quote_observations(
                    cleanup_error,
                    observed,
                    contract_ids=tuple(contract.con_id for contract, *_rest in started),
                    observed_at=self._now(),
                )
                raise cleanup_error from request_error
            if request_cancellation is not None:
                raise request_error from request_cancellation
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

    @classmethod
    def _quote_is_complete(cls, contract: Instrument, ticker: Ticker) -> bool:
        if not isinstance(contract, OptionContract):
            return cls._has_price_update(ticker)

        tick_types = {tick.tickType for tick in ticker.ticks}
        right_volume = _CALL_VOLUME_TICK if contract.right is OptionRight.CALL else _PUT_VOLUME_TICK
        right_open_interest = (
            _CALL_OPEN_INTEREST_TICK
            if contract.right is OptionRight.CALL
            else _PUT_OPEN_INTEREST_TICK
        )
        return all(
            (
                bool(tick_types & _BID_TICK_TYPES),
                bool(tick_types & _ASK_TICK_TYPES),
                bool(tick_types & _VOLUME_TICK_TYPES) or right_volume in tick_types,
                right_open_interest in tick_types,
                ticker.modelGreeks is not None,
                ticker.marketDataType in {1, 2, 3, 4},
            )
        )

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
            raise MarketDataCancellationError(
                "IBKR could not clean up one or more failed market-data requests",
                contract_ids=tuple(failures),
                observed_at=self._now(),
            )

    def _on_connected(self, *_args: object) -> None:
        del _args
        self._invalidate_connection("ib_async connection established; reconciliation required")

    def _on_disconnected(self, *_args: object) -> None:
        del _args
        self._invalidate_connection("ib_async connection lost; reconciliation required")

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

    def _on_ib_error(
        self,
        _request_id: int,
        error_code: int,
        _error_message: str,
        contract: Contract | None,
    ) -> None:
        if error_code in _CONNECTION_UNCERTAIN_CODES:
            self._invalidate_connection(f"ib_async connectivity event {error_code}")
            return
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
        vars(error).update(
            broker_error_code=error_code,
            observed_at=self._now(),
        )

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
            quote_waiter = self._market_data_waiters.get(contract_id)
            if quote_waiter is not None and not quote_waiter.done():
                quote_waiter.set_exception(_clone_market_data_error(error))
            else:
                self._market_data_errors[contract_id] = error
        else:
            pending_waiters = tuple(
                waiter for waiter in self._market_data_waiters.values() if not waiter.done()
            )
            pending_evidence_waiters = tuple(
                waiter for waiter in self._option_evidence_error_waiters if not waiter.done()
            )
            if pending_waiters or pending_evidence_waiters:
                for quote_waiter in pending_waiters:
                    quote_waiter.set_exception(_clone_market_data_error(error))
                for evidence_waiter in pending_evidence_waiters:
                    evidence_waiter.set_result(_clone_market_data_error(error))
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

    async def _await_option_evidence[T](
        self,
        operation_call: Callable[[], Awaitable[T]],
    ) -> T:
        """Race one option-evidence read against an id-less classified error."""

        error_waiter = self._option_evidence_scope_waiter.get()
        if error_waiter is None:
            async with self._option_evidence_scope():
                return await self._await_option_evidence(operation_call)
        if error_waiter.done():
            raise _clone_market_data_error(error_waiter.result())

        operation_task: asyncio.Future[T] | None = None
        try:
            operation_task = asyncio.ensure_future(operation_call())
            done, _pending = await asyncio.wait(
                (operation_task, error_waiter),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if error_waiter in done:
                classified = _clone_market_data_error(error_waiter.result())
                if operation_task in done and not operation_task.cancelled():
                    operation_error = operation_task.exception()
                    if operation_error is None:
                        vars(classified).update(completed_result=operation_task.result())
                else:
                    operation_task.cancel()
                    await asyncio.gather(operation_task, return_exceptions=True)
                raise classified
            return await operation_task
        except BaseException:
            if operation_task is not None and not operation_task.done():
                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
            raise

    def _current_option_evidence_error(self) -> BrokerDataError | None:
        error_waiter = self._option_evidence_scope_waiter.get()
        if error_waiter is None or not error_waiter.done():
            return None
        return _clone_market_data_error(error_waiter.result())

    @asynccontextmanager
    async def _option_evidence_scope(self) -> AsyncIterator[None]:
        """Keep one global-error signal alive across a logical option read."""

        inherited = self._option_evidence_scope_waiter.get()
        if inherited is not None:
            yield
            return

        # A classified error received while idle belongs to the next evidence
        # request. Check before registering or constructing any broker coroutine.
        self._raise_market_data_error(())
        error_waiter: asyncio.Future[BrokerDataError] = asyncio.get_running_loop().create_future()
        token = self._option_evidence_scope_waiter.set(error_waiter)
        self._option_evidence_error_waiters.add(error_waiter)
        try:
            yield
        except BaseException:
            raise
        else:
            if error := self._current_option_evidence_error():
                raise error
        finally:
            self._option_evidence_scope_waiter.reset(token)
            self._option_evidence_error_waiters.discard(error_waiter)
            if not error_waiter.done():
                error_waiter.cancel()

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
    def _commission(cls, fill: Fill) -> tuple[Decimal, str] | None:
        report = fill.commissionReport
        if not report.execId or report.execId != fill.execution.execId:
            return None
        amount = cls._optional_decimal(report.commission, nonnegative=True)
        if amount is None:
            return None
        currency = report.currency.strip().upper()
        if not currency:
            raise BrokerDataError("IBKR commission report is missing its currency")
        return amount, currency

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
