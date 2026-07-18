"""Typed, broker-neutral models used at every Chronos boundary."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    field_validator,
    model_validator,
)

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
from chronos.utils.identifiers import normalize_account_fingerprint


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
    security_type: Literal[SecurityType.STOCK] = SecurityType.STOCK
    exchange: str = "SMART"
    primary_exchange: str | None = None
    currency: str = "USD"

    @field_validator("symbol", "exchange", "currency")
    @classmethod
    def normalize_required_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("Instrument code fields must not be blank")
        return normalized

    @field_validator("primary_exchange")
    @classmethod
    def normalize_primary_exchange(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("primary_exchange must not be blank when supplied")
        return normalized


class OptionContractSpec(ChronosModel):
    symbol: str
    underlying_con_id: PositiveInt | None = None
    expiration: date
    strike: Decimal = Field(gt=0)
    right: OptionRight
    exchange: str = "SMART"
    currency: str = "USD"
    multiplier: Decimal = Field(gt=0)
    trading_class: str

    @field_validator("symbol", "exchange", "currency", "trading_class")
    @classmethod
    def normalize_required_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("Option code fields must not be blank")
        return normalized


class OptionContract(OptionContractSpec):
    con_id: PositiveInt
    security_type: Literal[SecurityType.OPTION] = SecurityType.OPTION
    local_symbol: str
    min_tick: Decimal = Field(gt=0, default=Decimal("0.01"))
    deliverable_shares: Decimal | None = Field(
        default=None,
        gt=0,
        description=(
            "Complete share-only deliverable for the exact underlying; no cash or other assets"
        ),
    )
    deliverable_verified: bool = False

    @field_validator("local_symbol")
    @classmethod
    def normalize_local_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("local_symbol must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_deliverable_evidence(self) -> OptionContract:
        if self.deliverable_verified and (
            self.underlying_con_id is None or self.deliverable_shares is None
        ):
            raise ValueError(
                "Verified option deliverables require an underlying contract ID and share quantity"
            )
        if self.deliverable_shares is not None and not self.deliverable_verified:
            raise ValueError("Option deliverable shares cannot be trusted without verification")
        return self

    @property
    def has_verified_standard_deliverable(self) -> bool:
        """Whether MVP assignment math can safely treat multiplier as delivered shares."""

        return (
            self.deliverable_verified
            and self.underlying_con_id is not None
            and self.deliverable_shares is not None
            and self.deliverable_shares == self.multiplier
        )


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

    @field_validator("exchange", "trading_class")
    @classmethod
    def normalize_required_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("Option-chain code fields must not be blank")
        return normalized


class StockAvailability(ChronosModel):
    """Unencumbered shares bound to one exact Wheel cycle and stock instrument."""

    wheel_cycle_id: str
    symbol: str
    account_fingerprint: str
    underlying_con_id: PositiveInt
    currency: str
    shares: Decimal = Field(ge=0)

    @field_validator("wheel_cycle_id")
    @classmethod
    def normalize_wheel_cycle_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("wheel_cycle_id must not be blank")
        return normalized

    @field_validator("account_fingerprint")
    @classmethod
    def validate_account_fingerprint(cls, value: str) -> str:
        return normalize_account_fingerprint(value)

    @field_validator("symbol", "currency")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol and currency must not be blank")
        return normalized


class BrokerPosition(ChronosModel):
    account_id: str
    contract: Instrument
    quantity: Decimal
    average_cost: Decimal
    market_price: Decimal | None = Field(default=None, ge=0)
    unrealized_pnl: Decimal | None = None

    @field_validator("account_id")
    @classmethod
    def validate_account_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("account_id must not be blank")
        return normalized


class BrokerExecution(ChronosModel):
    execution_id: str
    account_id: str
    broker_order_id: int
    permanent_id: int | None = None
    client_id: int = Field(ge=0)
    order_ref: str | None = None
    contract: Instrument
    side: OrderSide
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    timestamp: AwareDatetime
    commission: Decimal | None = Field(default=None, ge=0)
    commission_currency: str | None = None

    @field_validator("account_id")
    @classmethod
    def validate_account_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("account_id must not be blank")
        return normalized

    @field_validator("order_ref")
    @classmethod
    def normalize_order_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("commission_currency")
    @classmethod
    def normalize_commission_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("commission_currency must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_commission_evidence(self) -> BrokerExecution:
        if (self.commission is None) != (self.commission_currency is None):
            raise ValueError("Commission amount and currency must be supplied together")
        return self


class BrokerOrderIdentity(ChronosModel):
    """Composite broker identity used for affirmative order ownership."""

    account_id: str
    client_id: int = Field(ge=0)
    broker_order_id: int
    permanent_id: int | None = None
    order_ref: str | None = None
    symbol: str
    contract_id: PositiveInt
    side: OrderSide
    quantity: Decimal = Field(gt=0)

    @field_validator("account_id")
    @classmethod
    def validate_account_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("account_id must not be blank")
        return normalized

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be blank")
        return normalized

    @field_validator("order_ref")
    @classmethod
    def normalize_order_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class LocalReconciliationEvidence(ChronosModel):
    """One atomic local read used to prove or withhold broker reconciliation."""

    active_order_identities: frozenset[BrokerOrderIdentity] = frozenset()
    unresolved_symbols: frozenset[str] = frozenset()
    complete: bool = False
    reasons: tuple[str, ...] = ()

    @field_validator("unresolved_symbols")
    @classmethod
    def normalize_unresolved_symbols(cls, values: frozenset[str]) -> frozenset[str]:
        normalized = frozenset(value.strip().upper() for value in values)
        if any(not value for value in normalized):
            raise ValueError("unresolved_symbols must not contain blank symbols")
        return normalized


class BrokerOrder(ChronosModel):
    broker_order_id: int
    permanent_id: int | None = None
    client_id: int = Field(ge=0)
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

    @field_validator("account_id")
    @classmethod
    def validate_account_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("account_id must not be blank")
        return normalized

    @field_validator("order_ref")
    @classmethod
    def normalize_order_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_quantities(self) -> BrokerOrder:
        if self.filled_quantity + self.remaining_quantity != self.quantity:
            raise ValueError("Filled and remaining quantities must equal order quantity")
        return self

    @property
    def identity(self) -> BrokerOrderIdentity:
        """Return the immutable identity used by reconciliation evidence."""

        return BrokerOrderIdentity(
            account_id=self.account_id,
            client_id=self.client_id,
            broker_order_id=self.broker_order_id,
            permanent_id=self.permanent_id,
            order_ref=self.order_ref,
            symbol=self.contract.symbol,
            contract_id=self.contract.con_id,
            side=self.side,
            quantity=self.quantity,
        )


class OrderRequest(ChronosModel):
    correlation_id: str
    account_id: str
    # Instrument (UnderlyingContract | OptionContract) so equity orders (plan
    # §6b stock fold-in) traverse the same submission boundary as options.
    # OptionContract remains valid, so existing option callers are unaffected.
    contract: Instrument
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
