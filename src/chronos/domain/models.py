"""Typed, broker-neutral models used at every Chronos boundary."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PositiveInt, model_validator

from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    DemoCase,
    DisplayEnvironment,
    OptionRight,
    OrderIntent,
    OrderLifecycle,
    OrderSide,
    SecurityType,
)


class ChronosModel(BaseModel):
    """Immutable value model with strict field names."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ConnectionStatus(ChronosModel):
    state: ConnectionState
    environment: DisplayEnvironment
    connected: bool
    account_id: str | None = None
    data_quality: DataQuality = DataQuality.UNKNOWN
    last_successful_sync: AwareDatetime | None = None
    message: str = ""


class AccountSummary(ChronosModel):
    account_id: str
    net_liquidation: Decimal = Field(ge=0)
    total_cash: Decimal
    buying_power: Decimal = Field(ge=0)
    currency: str = "USD"
    as_of: AwareDatetime


class UnderlyingContract(ChronosModel):
    con_id: PositiveInt
    symbol: str
    security_type: SecurityType = SecurityType.STOCK
    exchange: str = "SMART"
    primary_exchange: str | None = None
    currency: str = "USD"


class OptionContractSpec(ChronosModel):
    symbol: str
    expiration: date
    strike: Decimal = Field(gt=0)
    right: OptionRight
    exchange: str = "SMART"
    currency: str = "USD"
    multiplier: Decimal = Field(gt=0)
    trading_class: str


class OptionContract(OptionContractSpec):
    con_id: PositiveInt
    security_type: SecurityType = SecurityType.OPTION
    local_symbol: str
    min_tick: Decimal = Field(gt=0, default=Decimal("0.01"))


Instrument = UnderlyingContract | OptionContract


class ModelGreeks(ChronosModel):
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    implied_volatility: Decimal | None = Field(default=None, ge=0)


class MarketQuote(ChronosModel):
    contract: Instrument
    timestamp: AwareDatetime
    data_quality: DataQuality
    bid: Decimal | None = Field(default=None, ge=0)
    ask: Decimal | None = Field(default=None, ge=0)
    last: Decimal | None = Field(default=None, ge=0)
    close: Decimal | None = Field(default=None, ge=0)
    volume: int | None = Field(default=None, ge=0)
    open_interest: int | None = Field(default=None, ge=0)
    greeks: ModelGreeks | None = None

    @property
    def midpoint(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / Decimal("2")


class OptionChainParameters(ChronosModel):
    exchange: str
    underlying_con_id: PositiveInt
    trading_class: str
    multiplier: Decimal = Field(gt=0)
    expirations: tuple[date, ...]
    strikes: tuple[Decimal, ...]


class BrokerPosition(ChronosModel):
    account_id: str
    contract: Instrument
    quantity: Decimal
    average_cost: Decimal
    market_price: Decimal | None = Field(default=None, ge=0)
    unrealized_pnl: Decimal | None = None


class BrokerExecution(ChronosModel):
    execution_id: str
    account_id: str
    broker_order_id: int
    permanent_id: int | None = None
    client_id: int
    order_ref: str | None = None
    contract: Instrument
    side: OrderSide
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    timestamp: AwareDatetime
    commission: Decimal | None = Field(default=None, ge=0)


class BrokerOrder(ChronosModel):
    broker_order_id: int
    permanent_id: int | None = None
    client_id: int
    account_id: str
    order_ref: str | None = None
    contract: Instrument
    side: OrderSide
    quantity: Decimal = Field(gt=0)
    filled_quantity: Decimal = Field(ge=0)
    remaining_quantity: Decimal = Field(ge=0)
    limit_price: Decimal = Field(gt=0)
    lifecycle: OrderLifecycle
    transmit: bool = False
    outside_rth: bool = False

    @model_validator(mode="after")
    def validate_quantities(self) -> BrokerOrder:
        if self.filled_quantity + self.remaining_quantity != self.quantity:
            raise ValueError("Filled and remaining quantities must equal order quantity")
        return self


class OrderRequest(ChronosModel):
    correlation_id: str
    account_id: str
    contract: OptionContract
    intent: OrderIntent
    side: OrderSide
    quantity: PositiveInt
    limit_price: Decimal = Field(gt=0)
    order_ref: str
    transmit: bool = False
    outside_rth: bool = False


class OrderPreview(ChronosModel):
    request: OrderRequest
    accepted: bool
    estimated_commission: Decimal | None = Field(default=None, ge=0)
    initial_margin_change: Decimal | None = None
    maintenance_margin_change: Decimal | None = None
    equity_with_loan_change: Decimal | None = None
    warnings: tuple[str, ...] = ()
    broker_message: str = ""
    previewed_at: AwareDatetime


class OrderSubmission(ChronosModel):
    correlation_id: str
    broker_order_id: int
    permanent_id: int | None = None
    client_id: int
    lifecycle: OrderLifecycle
    submitted_at: AwareDatetime
    message: str = ""


class OrderModification(ChronosModel):
    correlation_id: str
    broker_order_id: int
    new_limit_price: Decimal = Field(gt=0)


class CancellationResult(ChronosModel):
    broker_order_id: int
    requested: bool
    lifecycle: OrderLifecycle
    message: str
    timestamp: AwareDatetime


class BrokerSnapshot(ChronosModel):
    account: AccountSummary
    positions: tuple[BrokerPosition, ...]
    open_orders: tuple[BrokerOrder, ...]
    executions: tuple[BrokerExecution, ...]
    captured_at: AwareDatetime


class DemoFixtureCase(ChronosModel):
    symbol: str
    case: DemoCase
    explanation: str
