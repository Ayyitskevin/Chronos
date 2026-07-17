"""Risk policy schema with deny-by-default values (Phase 8).

Every financially meaningful field defaults to zero, empty, or disabled.
A policy file must explicitly grant each allowance. Loading validates against
this schema and rejects unknown keys, so a typo cannot silently widen a limit.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field

NonNegativeMoney = Annotated[float, Field(ge=0)]
Fraction = Annotated[float, Field(ge=0, le=1)]


class RiskPolicy(BaseModel):
    """Frozen, schema-validated risk policy. All defaults deny."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = "0"

    allowed_symbols: tuple[str, ...] = ()
    allowed_strategy_ids: tuple[str, ...] = ()
    allow_long_entries: bool = False
    allow_short_entries: bool = False

    max_bot_capital_usd: NonNegativeMoney = 0.0
    max_position_notional_usd: NonNegativeMoney = 0.0
    max_aggregate_exposure_usd: NonNegativeMoney = 0.0
    max_symbol_exposure_fraction: Fraction = 0.0
    max_risk_per_trade_fraction: Fraction = 0.0
    max_simultaneous_positions: int = Field(default=0, ge=0)
    max_open_orders: int = Field(default=0, ge=0)

    max_daily_loss_usd: NonNegativeMoney = 0.0
    max_weekly_loss_usd: NonNegativeMoney = 0.0
    max_drawdown_fraction: Fraction = 0.0
    max_consecutive_losses: int = Field(default=0, ge=0)

    max_quote_age_seconds: float = Field(default=0.0, ge=0)
    max_bar_age_seconds: float = Field(default=0.0, ge=0)
    max_price_deviation_fraction: Fraction = 0.0

    max_order_rejections_per_day: int = Field(default=0, ge=0)
    cooldown_bars_after_loss_halt: int = Field(default=0, ge=0)

    allow_market_orders: bool = False
    allow_margin: bool = False
    allow_overnight_positions: bool = False
    allow_averaging_down: bool = False
    allow_pyramiding: bool = False
    allow_options: bool = False

    @property
    def config_hash(self) -> str:
        canonical = repr(sorted(self.model_dump().items()))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def load_risk_policy(path: Path) -> RiskPolicy:
    """Load and validate a YAML risk policy, fail-closed on any error."""

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: risk policy must be a mapping")
    return RiskPolicy.model_validate(raw)
