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

from chronos.config.limits import (
    MAX_BROKER_CODE_CHARS,
    MAX_BROKER_LOCAL_SYMBOL_CHARS,
    MAX_BROKER_SESSION_CHARS,
    MAX_BROKER_TIME_ZONE_CHARS,
    MAX_OPTION_DELIVERABLE_ASSET_CHARS,
    MAX_OPTION_SELECTION_OTHER_ASSETS,
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
    # Broker-OBSERVED evidence (ADR-0009 §3): the gateway's managed-account ids
    # as reported by the session itself — never derived from configuration.
    # Empty means unobserved, which the live grant treats as UNKNOWN (denies).
    managed_accounts: tuple[str, ...] = ()


class AccountSummary(ChronosModel):
    account_id: str
    net_liquidation: Decimal = Field(ge=0)
    total_cash: Decimal
    buying_power: Decimal = Field(ge=0)
    currency: str = "USD"
    as_of: AwareDatetime


class UnderlyingContract(ChronosModel):
    con_id: PositiveInt
    symbol: str = Field(min_length=1, max_length=MAX_BROKER_CODE_CHARS)
    security_type: Literal[SecurityType.STOCK] = SecurityType.STOCK
    exchange: str = Field(default="SMART", min_length=1, max_length=MAX_BROKER_CODE_CHARS)
    primary_exchange: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_BROKER_CODE_CHARS,
    )
    currency: str = Field(default="USD", min_length=1, max_length=MAX_BROKER_CODE_CHARS)
    #: IBKR contract-detail session evidence (M9, closes R-26). Empty when the
    #: adapter did not supply it — the demo broker, an older qualification path,
    #: a gateway that answered without details. Absent evidence must read as
    #: *unknown*, which leaves the session gate AMBIGUOUS and blocking; it must
    #: never be mistaken for "no restrictions".
    liquid_hours: str = Field(default="", max_length=MAX_BROKER_SESSION_CHARS)
    #: The zone ``liquid_hours`` is expressed in, as IBKR names it (``US/Eastern``).
    #: Useless without the hours and vice versa, so they travel together.
    time_zone_id: str = Field(default="", max_length=MAX_BROKER_TIME_ZONE_CHARS)

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
    symbol: str = Field(min_length=1, max_length=MAX_BROKER_CODE_CHARS)
    underlying_con_id: PositiveInt | None = None
    expiration: date
    strike: Decimal = Field(gt=0)
    right: OptionRight
    exchange: str = Field(default="SMART", min_length=1, max_length=MAX_BROKER_CODE_CHARS)
    currency: str = Field(default="USD", min_length=1, max_length=MAX_BROKER_CODE_CHARS)
    multiplier: Decimal = Field(gt=0)
    trading_class: str = Field(min_length=1, max_length=MAX_BROKER_CODE_CHARS)

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
    local_symbol: str = Field(min_length=1, max_length=MAX_BROKER_LOCAL_SYMBOL_CHARS)
    #: Session evidence, as on :class:`UnderlyingContract` and for the same
    #: reason (M9, R-26). Options observe the equity holiday calendar, so the
    #: family that most needs a holiday told to it is this one.
    liquid_hours: str = Field(default="", max_length=MAX_BROKER_SESSION_CHARS)
    time_zone_id: str = Field(default="", max_length=MAX_BROKER_TIME_ZONE_CHARS)
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


class OptionPriceIncrement(ChronosModel):
    """One broker-observed price band from an option market rule.

    ``low_edge`` is inclusive.  A complete rule is evaluated by selecting the
    band with the greatest edge not exceeding the proposed price; callers must
    not collapse a banded rule into one guessed scalar tick.
    """

    low_edge: Decimal = Field(ge=0)
    increment: Decimal = Field(gt=0)

    @field_validator("low_edge", "increment")
    @classmethod
    def require_finite_increment(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("option price increments must be finite")
        return value


class OptionMarketRule(ChronosModel):
    """Exact read-only market-rule evidence for one qualified option."""

    con_id: PositiveInt
    exchange: str = Field(min_length=1, max_length=MAX_BROKER_CODE_CHARS)
    market_rule_id: PositiveInt
    price_increments: tuple[OptionPriceIncrement, ...]
    source: str = Field(min_length=1, max_length=128)

    @field_validator("exchange")
    @classmethod
    def normalize_market_rule_exchange(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("market-rule exchange must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_price_bands(self) -> OptionMarketRule:
        if not self.price_increments:
            raise ValueError("an option market rule must carry at least one price increment")
        edges = tuple(item.low_edge for item in self.price_increments)
        if edges[0] != 0:
            raise ValueError("an option market rule must begin at low_edge 0")
        if edges != tuple(sorted(set(edges))):
            raise ValueError("option market-rule low edges must be unique and increasing")
        return self


class OptionDeliverableFacts(ChronosModel):
    """Authoritative-or-unknown deliverable facts for one exact option.

    This deliberately does not reuse ``OptionContract.deliverable_verified``.
    That legacy flag is currently populated from an OCC naming convention,
    which is useful as a negative screen but is not an authoritative reading of
    the deliverable schedule.  Autonomous option selection requires
    ``authoritative=True`` plus complete share/cash/other-asset fields.
    """

    con_id: PositiveInt
    authoritative: bool
    source: str = Field(min_length=1, max_length=128)
    underlying_con_id: PositiveInt | None = None
    share_quantity: Decimal | None = Field(default=None, ge=0)
    cash_amount: Decimal | None = Field(default=None, ge=0)
    other_assets: tuple[str, ...] | None = None

    @field_validator("share_quantity", "cash_amount")
    @classmethod
    def require_finite_deliverable_number(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("deliverable quantities must be finite")
        return value

    @field_validator("other_assets")
    @classmethod
    def bound_other_assets(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        if len(value) > MAX_OPTION_SELECTION_OTHER_ASSETS:
            raise ValueError("option deliverable assets exceed the hard evidence bound")
        if any(len(item) > MAX_OPTION_DELIVERABLE_ASSET_CHARS for item in value):
            raise ValueError("option deliverable asset text exceeds the hard evidence bound")
        return value

    @model_validator(mode="after")
    def validate_authoritative_shape(self) -> OptionDeliverableFacts:
        if self.authoritative and (
            self.underlying_con_id is None
            or self.share_quantity is None
            or self.cash_amount is None
            or self.other_assets is None
        ):
            raise ValueError(
                "authoritative deliverable facts require underlying, shares, cash, and "
                "other-assets evidence"
            )
        return self

    def is_standard_for(self, contract: OptionContract) -> bool:
        """Whether exact evidence proves a share-only standard deliverable."""

        return (
            self.authoritative
            and self.con_id == contract.con_id
            and self.underlying_con_id == contract.underlying_con_id
            and self.share_quantity == contract.multiplier
            and self.cash_amount == 0
            and self.other_assets == ()
        )


class CryptoContract(ChronosModel):
    """IBKR spot crypto contract (ADR-0010, Milestone 7C).

    Venue metadata (``min_tick``/``min_size``/``size_increment``) is populated
    ONLY from qualified contract details at qualification time — the read path
    (positions/executions/open-orders normalization) leaves them ``None``, and
    every dependent risk check reports UNKNOWN (fail closed) when absent. The
    ``exchange`` default is the qualification hint; the qualified value echoes
    the gateway's own routing (entity-dependent: Paxos vs Zero Hash).
    """

    con_id: PositiveInt
    symbol: str
    security_type: Literal[SecurityType.CRYPTO] = SecurityType.CRYPTO
    exchange: str = "PAXOS"
    currency: str = "USD"
    min_tick: Decimal | None = Field(default=None, gt=0)
    min_size: Decimal | None = Field(default=None, gt=0)
    size_increment: Decimal | None = Field(default=None, gt=0)

    @field_validator("symbol", "exchange", "currency")
    @classmethod
    def normalize_required_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("Instrument code fields must not be blank")
        return normalized


Instrument = UnderlyingContract | OptionContract | CryptoContract


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
    exchange: str = Field(min_length=1, max_length=MAX_BROKER_CODE_CHARS)
    underlying_con_id: PositiveInt
    trading_class: str = Field(min_length=1, max_length=MAX_BROKER_CODE_CHARS)
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
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal = Field(gt=0)
    order_ref: str
    transmit: bool = False
    # Family-conditional TIF (ADR-0010 §3): the adapter maps this to the venue
    # order's tif. Options/stocks are always DAY; crypto may be DAY or IOC.
    time_in_force: Literal["DAY", "IOC"] = "DAY"
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
