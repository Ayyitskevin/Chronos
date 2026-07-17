"""Environment-backed, fail-closed Chronos settings."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BeforeValidator, Field, PositiveInt, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from chronos.config.limits import (
    MAX_CANDIDATE_EXPIRATIONS,
    MAX_CANDIDATE_REQUEST_CONTRACTS,
    MAX_CANDIDATE_STRIKES_PER_EXPIRATION,
)
from chronos.domain.enums import BrokerMode, DemoProfile, IBEnvironment


def _parse_symbol_allowlist(value: object) -> object:
    if isinstance(value, str):
        return tuple(part.strip().upper() for part in value.split(",") if part.strip())
    return value


SymbolAllowlist = Annotated[tuple[str, ...], BeforeValidator(_parse_symbol_allowlist)]


class Settings(BaseSettings):
    """Validated settings loaded from environment variables or a local `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    broker_mode: BrokerMode = BrokerMode.DEMO
    demo_profile: DemoProfile = DemoProfile.SAFETY_CASES
    ib_environment: IBEnvironment = IBEnvironment.PAPER
    ib_host: str = "127.0.0.1"
    ib_port: PositiveInt = 7497
    ib_client_id: Annotated[int, Field(ge=0)] = 17
    ib_account_id: str = ""

    allow_order_transmit: bool = False
    allow_live_trading: bool = False
    allow_outside_rth: bool = False

    # Live-wheel plan (Milestone 1): configuration surface added ahead of the
    # gate stack. The hard-raise on allow_live_trading below remains in force
    # until Milestone 6 replaces it with the full gated model (fail-closed at
    # every intermediate commit — docs/LIVE_WHEEL_GAME_PLAN.md §6b).
    ib_account_allowlist: SymbolAllowlist = ()
    enable_paper_trading: bool = True
    require_live_arming: bool = True
    live_arm_ttl_minutes: Annotated[int, Field(gt=0, le=120)] = 15
    require_typed_confirmation: bool = True
    order_confirmation_ttl_seconds: Annotated[int, Field(gt=0, le=300)] = 20
    max_open_short_option_contracts: Annotated[int, Field(ge=0)] = 5
    max_opening_orders_per_day: Annotated[int, Field(ge=0)] = 3
    max_gross_assignment_usd: Annotated[Decimal, Field(ge=0)] = Decimal("25000")
    min_cash_buffer_usd: Annotated[Decimal, Field(ge=0)] = Decimal("5000")
    min_cash_buffer_pct: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.10")
    min_excess_liquidity_usd: Annotated[Decimal, Field(ge=0)] = Decimal("10000")
    max_session_drawdown_usd: Annotated[Decimal, Field(ge=0)] = Decimal("1000")
    max_session_drawdown_pct: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.02")

    # Product families beyond options (owner-directed scope, plan §6b).
    # An empty crypto allowlist keeps the crypto family entirely disabled.
    crypto_allowlist: SymbolAllowlist = ()
    max_crypto_allocation_pct: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.10")
    max_crypto_notional_per_order_usd: Annotated[Decimal, Field(ge=0)] = Decimal("1000")

    # Local backend service (FastAPI); loopback-only by design.
    backend_host: str = "127.0.0.1"
    backend_port: Annotated[int, Field(gt=0, lt=65536)] = 8765
    backend_token_file: Path = Path("data/backend_api_token")

    symbol_allowlist: SymbolAllowlist = ("AAPL", "MSFT", "SPY")
    target_abs_delta: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.30")
    min_abs_delta: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.20")
    max_abs_delta: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.35")
    min_dte: Annotated[int, Field(ge=0)] = 7
    target_dte: Annotated[int, Field(ge=0)] = 21
    max_dte: Annotated[int, Field(ge=0)] = 45
    max_expirations: Annotated[int, Field(gt=0, le=MAX_CANDIDATE_EXPIRATIONS)] = 6
    max_strikes_per_expiration: Annotated[
        int, Field(gt=0, le=MAX_CANDIDATE_STRIKES_PER_EXPIRATION)
    ] = 12
    min_option_volume: Annotated[int, Field(ge=0)] = 10
    min_open_interest: Annotated[int, Field(ge=0)] = 100
    max_relative_spread: Annotated[Decimal, Field(gt=0)] = Decimal("0.20")
    max_quote_age_seconds: PositiveInt = 5
    market_timezone: str = "America/New_York"
    max_contracts_per_order: PositiveInt = 1
    max_symbol_allocation_pct: Annotated[Decimal, Field(gt=0, le=1)] = Decimal("0.25")
    max_total_wheel_allocation_pct: Annotated[Decimal, Field(gt=0, le=1)] = Decimal("0.60")

    delta_weight: Annotated[Decimal, Field(ge=0)] = Decimal("0.45")
    spread_weight: Annotated[Decimal, Field(ge=0)] = Decimal("0.30")
    dte_weight: Annotated[Decimal, Field(ge=0)] = Decimal("0.15")
    liquidity_weight: Annotated[Decimal, Field(ge=0)] = Decimal("0.10")

    assignment_near_zero_extrinsic: Annotated[Decimal, Field(ge=0)] = Decimal("0.05")
    assignment_meaningful_extrinsic: Annotated[Decimal, Field(ge=0)] = Decimal("0.10")
    assignment_elevated_abs_delta: Annotated[Decimal, Field(gt=0, le=1)] = Decimal("0.50")
    assignment_high_dte: Annotated[int, Field(ge=0)] = 3
    assignment_elevated_dte: Annotated[int, Field(ge=0)] = 5
    assignment_ex_dividend_window_days: Annotated[int, Field(ge=0)] = 5

    database_url: str = "sqlite:///data/chronos.db"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_file: Path = Path("logs/chronos.log")

    @model_validator(mode="after")
    def validate_safety_and_ranges(self) -> Settings:
        if self.allow_live_trading:
            raise ValueError("Live trading is hard-disabled in the Chronos MVP")
        if not self.symbol_allowlist:
            raise ValueError("SYMBOL_ALLOWLIST must contain at least one symbol")
        if len(set(self.symbol_allowlist)) != len(self.symbol_allowlist):
            raise ValueError("SYMBOL_ALLOWLIST must not contain duplicates")
        if any(not symbol.isalnum() for symbol in self.symbol_allowlist):
            raise ValueError("SYMBOL_ALLOWLIST entries must be alphanumeric")
        if not self.min_abs_delta <= self.target_abs_delta <= self.max_abs_delta:
            raise ValueError("TARGET_ABS_DELTA must be between MIN_ABS_DELTA and MAX_ABS_DELTA")
        if not self.min_dte <= self.target_dte <= self.max_dte:
            raise ValueError("TARGET_DTE must be between MIN_DTE and MAX_DTE")
        if self.max_expirations * self.max_strikes_per_expiration > MAX_CANDIDATE_REQUEST_CONTRACTS:
            raise ValueError(
                "MAX_EXPIRATIONS * MAX_STRIKES_PER_EXPIRATION must not exceed "
                f"{MAX_CANDIDATE_REQUEST_CONTRACTS}"
            )
        if self.ib_environment is IBEnvironment.LIVE and self.allow_order_transmit:
            raise ValueError("Live order transmission is hard-disabled in the Chronos MVP")
        if (
            self.broker_mode is BrokerMode.IBKR
            and self.ib_environment is IBEnvironment.PAPER
            and self.allow_order_transmit
            and not self.ib_account_id.strip()
        ):
            raise ValueError("IB_ACCOUNT_ID is required before paper order transmission")
        weight_total = (
            self.delta_weight + self.spread_weight + self.dte_weight + self.liquidity_weight
        )
        if weight_total <= 0:
            raise ValueError("At least one resolver weight must be positive")
        if self.assignment_near_zero_extrinsic > self.assignment_meaningful_extrinsic:
            raise ValueError(
                "ASSIGNMENT_NEAR_ZERO_EXTRINSIC must not exceed ASSIGNMENT_MEANINGFUL_EXTRINSIC"
            )
        if self.assignment_high_dte > self.assignment_elevated_dte:
            raise ValueError("ASSIGNMENT_HIGH_DTE must not exceed ASSIGNMENT_ELEVATED_DTE")
        try:
            ZoneInfo(self.market_timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("MARKET_TIMEZONE must name an installed IANA timezone") from error
        if len(set(self.ib_account_allowlist)) != len(self.ib_account_allowlist):
            raise ValueError("IB_ACCOUNT_ALLOWLIST must not contain duplicates")
        if any(not entry.isalnum() for entry in self.ib_account_allowlist):
            raise ValueError("IB_ACCOUNT_ALLOWLIST entries must be alphanumeric")
        if len(set(self.crypto_allowlist)) != len(self.crypto_allowlist):
            raise ValueError("CRYPTO_ALLOWLIST must not contain duplicates")
        if any(not symbol.isalnum() for symbol in self.crypto_allowlist):
            raise ValueError("CRYPTO_ALLOWLIST entries must be alphanumeric")
        if self.backend_host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(
                "BACKEND_HOST must be a loopback address; remote exposure of the "
                "order-writing backend is out of scope by design"
            )
        return self

    @property
    def transmission_possible(self) -> bool:
        """Whether configuration can enter the paper-order transmission path."""

        return (
            self.broker_mode is BrokerMode.IBKR
            and self.ib_environment is IBEnvironment.PAPER
            and self.allow_order_transmit
            and not self.allow_live_trading
            and bool(self.ib_account_id.strip())
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one validated settings object per process."""

    return Settings()
