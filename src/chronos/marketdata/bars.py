"""Immutable bar vocabulary shared by research, backtest, replay, and execution.

Design notes (ADR-0005):

- Timestamps are timezone-aware UTC. ``timestamp_utc`` is the bar *close* time;
  ``session_date`` preserves the exchange trading date so daily bars survive
  daylight-saving transitions unambiguously.
- Prices are ``float`` on purpose: Pine Script computes in IEEE-754 float64 and
  behavioural parity with the audited Pine sources is a stated goal. Everything
  that touches money or an order price re-quantizes through ``Decimal`` at the
  execution boundary (`chronos.execution`).
- A bar is only ever consumed by strategies once ``status`` is ``CLOSED``.
  There is no intrabar path in this platform, which removes the Pine
  ``calc_on_every_tick`` repainting class by construction.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum


class BarInterval(StrEnum):
    """Supported bar intervals. Only DAY_1 is validated for research today."""

    DAY_1 = "1d"
    HOUR_1 = "1h"
    MIN_5 = "5m"
    MIN_1 = "1m"


class BarStatus(StrEnum):
    CLOSED = "CLOSED"
    FORMING = "FORMING"


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV bar from one source for one instrument."""

    symbol: str
    source: str
    exchange: str
    interval: BarInterval
    session_date: date
    timestamp_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    adjusted_close: float | None = None
    status: BarStatus = BarStatus.CLOSED

    def __post_init__(self) -> None:
        if self.timestamp_utc.tzinfo is None or self.timestamp_utc.utcoffset() is None:
            raise ValueError("Bar timestamp_utc must be timezone-aware")
        if self.timestamp_utc.astimezone(UTC) != self.timestamp_utc:
            object.__setattr__(self, "timestamp_utc", self.timestamp_utc.astimezone(UTC))
        if not self.symbol or not self.symbol.isalnum():
            raise ValueError("Bar symbol must be non-empty and alphanumeric")

    @property
    def sequence_id(self) -> str:
        """Stable deduplication identifier."""

        return f"{self.source}:{self.symbol}:{self.interval}:{self.session_date.isoformat()}"


@dataclass(frozen=True, slots=True)
class BarSeries:
    """A chronologically ordered, single-symbol, single-interval bar container.

    The constructor enforces ordering and uniqueness; consumers can therefore
    rely on ``bars[i].session_date < bars[i + 1].session_date``.
    """

    symbol: str
    interval: BarInterval
    bars: tuple[Bar, ...] = field(default=())

    def __post_init__(self) -> None:
        previous: Bar | None = None
        for bar in self.bars:
            if bar.symbol != self.symbol:
                raise ValueError(f"Bar symbol {bar.symbol!r} does not match series {self.symbol!r}")
            if bar.interval is not self.interval:
                raise ValueError("Bar interval does not match series interval")
            if previous is not None and bar.timestamp_utc <= previous.timestamp_utc:
                raise ValueError(
                    "Bars must be strictly increasing in time; "
                    f"{bar.session_date} follows {previous.session_date}"
                )
            previous = bar

    def __len__(self) -> int:
        return len(self.bars)

    def __iter__(self) -> Iterator[Bar]:
        return iter(self.bars)

    def __getitem__(self, index: int) -> Bar:
        return self.bars[index]

    def closes(self) -> tuple[float, ...]:
        return tuple(bar.close for bar in self.bars)

    def slice_until(self, end_exclusive: int) -> Sequence[Bar]:
        """Bars visible to a strategy deciding at index ``end_exclusive - 1``."""

        return self.bars[:end_exclusive]
