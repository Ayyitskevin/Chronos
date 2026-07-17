"""Strategy protocol and proposal vocabulary.

Invariants enforced here:

- A proposal is advice, not an order. It has no share quantity and no account
  or broker fields, so a strategy physically cannot express "submit X shares
  to account Y".
- ``desired_exposure_fraction`` is bounded to [0, 1] of whatever capital the
  portfolio layer decides to make available; the risk engine caps it again.
- Proposals reference the bars that produced them (``source_bar_ids``) for
  audit reconstruction.
- Strategies are deterministic: same bar history + same params => same output.
  The replay tests assert this property.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from chronos.marketdata.bars import Bar


class ProposalDirection(StrEnum):
    ENTER_LONG = "ENTER_LONG"
    EXIT_LONG = "EXIT_LONG"
    HOLD = "HOLD"


class PositionSide(StrEnum):
    FLAT = "FLAT"
    LONG = "LONG"


@dataclass(frozen=True, slots=True)
class PositionState:
    """The strategy-visible position summary for one instrument."""

    side: PositionSide
    bars_held: int
    entry_price: float | None

    @staticmethod
    def flat() -> PositionState:
        return PositionState(side=PositionSide.FLAT, bars_held=0, entry_price=None)


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Everything a strategy may see when deciding on a closed bar."""

    bars: Sequence[Bar]
    position: PositionState

    @property
    def decision_bar(self) -> Bar:
        if not self.bars:
            raise ValueError("StrategyContext requires at least one bar")
        return self.bars[-1]


@dataclass(frozen=True, slots=True)
class StrategyProposal:
    """Advisory output of one strategy for one instrument on one closed bar."""

    strategy_id: str
    strategy_version: str
    timestamp_utc: datetime
    symbol: str
    direction: ProposalDirection
    desired_exposure_fraction: float
    stop_fraction: float | None
    evidence: Mapping[str, float] = field(default_factory=dict)
    source_bar_ids: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.desired_exposure_fraction <= 1.0:
            raise ValueError("desired_exposure_fraction must be within [0, 1]")
        if self.stop_fraction is not None and not 0.0 < self.stop_fraction < 1.0:
            raise ValueError("stop_fraction must be within (0, 1) when present")
        if self.timestamp_utc.tzinfo is None:
            raise ValueError("proposal timestamp must be timezone-aware")
        if self.direction is ProposalDirection.ENTER_LONG and self.desired_exposure_fraction == 0.0:
            raise ValueError("ENTER_LONG requires a positive desired_exposure_fraction")


@runtime_checkable
class Strategy(Protocol):
    """Deterministic bar-close strategy."""

    @property
    def strategy_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def warmup_bars(self) -> int: ...

    def on_bar(self, context: StrategyContext) -> StrategyProposal | None:
        """Return a proposal for the just-closed bar, or None to do nothing."""
        ...
