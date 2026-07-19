"""Option-chain snapshot schema for forward capture (ADR-0012 §1).

A histdata-local, versioned schema for one EOD snapshot of an underlying's option
chain — decoupled from the live ``chronos.domain`` option models (C1 precedent) so a
domain refactor can never silently change a stored snapshot or its provenance hash.

**Staleness is first-class.** Every market field is Optional and ``None`` when the
gateway did not return it (never fabricated), and every row carries its quote's
``SnapshotQuality`` (mirroring ``domain.DataQuality`` without importing it). A
snapshot exposes a quality histogram and a worst-case, so a $0-tier delayed/EOD
capture is labeled, never presented as live. This module imports only the standard
library.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any


class OptionRight(StrEnum):
    CALL = "call"
    PUT = "put"


class SnapshotQuality(StrEnum):
    """Data quality of a captured quote (mirrors ``domain.DataQuality``)."""

    LIVE = "LIVE"
    FROZEN = "FROZEN"
    DELAYED = "DELAYED"
    DELAYED_FROZEN = "DELAYED_FROZEN"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


# Severity order (best → worst); ``worst_quality`` returns the most severe present.
_QUALITY_SEVERITY: dict[SnapshotQuality, int] = {
    SnapshotQuality.LIVE: 0,
    SnapshotQuality.DELAYED: 1,
    SnapshotQuality.FROZEN: 2,
    SnapshotQuality.DELAYED_FROZEN: 3,
    SnapshotQuality.STALE: 4,
    SnapshotQuality.UNKNOWN: 5,
}


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


@dataclass(frozen=True, slots=True)
class OptionQuoteRow:
    """One captured option quote. Every market field is Optional (None if absent)."""

    expiration: date
    strike: float
    right: OptionRight
    data_quality: SnapshotQuality
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    close: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    implied_volatility: float | None = None

    def __post_init__(self) -> None:
        if not self.strike > 0:
            raise ValueError(f"strike must be > 0, got {self.strike!r}")
        if self.implied_volatility is not None and self.implied_volatility < 0:
            raise ValueError(f"implied_volatility must be >= 0, got {self.implied_volatility!r}")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> OptionQuoteRow:
        try:
            return cls(
                expiration=date.fromisoformat(str(data["expiration"])),
                strike=float(data["strike"]),
                right=OptionRight(str(data["right"])),
                data_quality=SnapshotQuality(str(data["data_quality"])),
                bid=_opt_float(data.get("bid")),
                ask=_opt_float(data.get("ask")),
                last=_opt_float(data.get("last")),
                close=_opt_float(data.get("close")),
                volume=_opt_int(data.get("volume")),
                open_interest=_opt_int(data.get("open_interest")),
                delta=_opt_float(data.get("delta")),
                gamma=_opt_float(data.get("gamma")),
                theta=_opt_float(data.get("theta")),
                implied_volatility=_opt_float(data.get("implied_volatility")),
            )
        except (KeyError, ValueError) as error:
            raise ValueError(f"invalid option quote row {dict(data)!r}: {error}") from error

    def to_mapping(self) -> dict[str, Any]:
        return {
            "expiration": self.expiration.isoformat(),
            "strike": self.strike,
            "right": self.right.value,
            "data_quality": self.data_quality.value,
            "bid": self.bid,
            "ask": self.ask,
            "last": self.last,
            "close": self.close,
            "volume": self.volume,
            "open_interest": self.open_interest,
            "delta": self.delta,
            "gamma": self.gamma,
            "theta": self.theta,
            "implied_volatility": self.implied_volatility,
        }


@dataclass(frozen=True, slots=True)
class OptionChainSnapshot:
    """One EOD snapshot of an underlying's (bounded) option chain."""

    underlying: str
    captured_at: str  # ISO-8601, passed in — never generated in this pure module
    source: str
    expiry_horizon_days: int
    strike_window_pct: float
    spot: float | None = None
    reason: str = ""  # why the snapshot is empty, when it is
    rows: tuple[OptionQuoteRow, ...] = ()

    def quality_histogram(self) -> dict[str, int]:
        histogram: dict[str, int] = {}
        for row in self.rows:
            histogram[row.data_quality.value] = histogram.get(row.data_quality.value, 0) + 1
        return dict(sorted(histogram.items()))

    def worst_quality(self) -> SnapshotQuality:
        if not self.rows:
            return SnapshotQuality.UNKNOWN
        return max(
            (row.data_quality for row in self.rows),
            key=lambda quality: _QUALITY_SEVERITY[quality],
        )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> OptionChainSnapshot:
        try:
            return cls(
                underlying=str(data["underlying"]),
                captured_at=str(data["captured_at"]),
                source=str(data["source"]),
                expiry_horizon_days=int(data["expiry_horizon_days"]),
                strike_window_pct=float(data["strike_window_pct"]),
                spot=_opt_float(data.get("spot")),
                reason=str(data.get("reason", "")),
                rows=tuple(OptionQuoteRow.from_mapping(row) for row in data.get("rows", ())),
            )
        except (KeyError, ValueError) as error:
            raise ValueError(f"invalid option chain snapshot: {error}") from error

    def to_mapping(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "captured_at": self.captured_at,
            "source": self.source,
            "expiry_horizon_days": self.expiry_horizon_days,
            "strike_window_pct": self.strike_window_pct,
            "spot": self.spot,
            "reason": self.reason,
            "quality_histogram": self.quality_histogram(),
            "worst_quality": self.worst_quality().value,
            "rows": [row.to_mapping() for row in self.rows],
        }
