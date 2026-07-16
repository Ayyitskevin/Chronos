"""Environment-backed, fail-closed Chronos settings."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BeforeValidator, Field, PositiveInt, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from chronos.domain.enums import BrokerMode, IBEnvironment


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
    ib_environment: IBEnvironment = IBEnvironment.PAPER
    ib_host: str = "127.0.0.1"
    ib_port: PositiveInt = 7497
    ib_client_id: Annotated[int, Field(ge=0)] = 17
    ib_account_id: str = ""

    allow_order_transmit: bool = False
    allow_live_trading: Literal[False] = False
    allow_outside_rth: bool = False

    symbol_allowlist: SymbolAllowlist = ("AAPL", "MSFT", "SPY")
    target_abs_delta: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.30")
    min_abs_delta: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.20")
    max_abs_delta: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.35")
    min_dte: Annotated[int, Field(ge=0)] = 7
    target_dte: Annotated[int, Field(ge=0)] = 21
    max_dte: Annotated[int, Field(ge=0)] = 45
    max_expirations: PositiveInt = 6
    max_strikes_per_expiration: PositiveInt = 12
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
