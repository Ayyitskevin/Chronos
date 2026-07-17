"""Schema for canonical strategy specifications.

Specs live in ``specs/*.yaml`` and are the authority a Python implementation
is verified against. A spec records where it came from (Pine source hashes),
what it deliberately changes, and its current review status. Loading fails
closed on unknown keys or invalid ranges.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

Fraction = Annotated[float, Field(ge=0, le=1)]


class ParameterSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    value: float
    minimum: float
    maximum: float
    description: str = ""


class PineReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog_number: str
    filename: str
    sha256: str
    notes: str = ""


class StrategySpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_id: str
    version: str
    family: str
    description: str
    supported_symbols: tuple[str, ...]
    supported_intervals: tuple[str, ...]
    required_fields: tuple[str, ...] = ("open", "high", "low", "close", "volume")
    required_lookback_bars: int = Field(ge=1)
    signal_logic: str
    entry_rules: tuple[str, ...]
    exit_rules: tuple[str, ...]
    stop_logic: str
    target_logic: str = "none"
    sizing_logic: str
    cooldown_bars: int = Field(default=0, ge=0)
    session_rules: str = "regular US session daily bars only"
    regime_constraints: str = ""
    liquidity_constraints: str = ""
    parameters: tuple[ParameterSpec, ...] = ()
    expected_failure_regimes: tuple[str, ...] = ()
    prohibited_conditions: tuple[str, ...] = ()
    pine_references: tuple[PineReference, ...] = ()
    translation_notes: tuple[str, ...] = ()
    fixture_references: tuple[str, ...] = ()
    long_only: bool = True
    status: Literal[
        "draft",
        "research_prototype",
        "backtest_validated",
        "shadow_eligible",
        "paper_eligible",
        "not_eligible",
    ] = "draft"
    owner_approved: bool = False


def load_spec(path: Path) -> StrategySpec:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: spec must be a mapping")
    return StrategySpec.model_validate(raw)


def load_specs_dir(directory: Path) -> dict[str, StrategySpec]:
    specs: dict[str, StrategySpec] = {}
    for path in sorted(directory.glob("*.yaml")):
        spec = load_spec(path)
        if spec.strategy_id in specs:
            raise ValueError(f"duplicate strategy_id {spec.strategy_id} in {path}")
        specs[spec.strategy_id] = spec
    return specs
