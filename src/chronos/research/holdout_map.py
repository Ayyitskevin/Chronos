"""Pure holdout-map types and complete-tiling validation."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from chronos.marketdata.bars import BarSeries
from chronos.research.certified_data import DataClassification


class HoldoutMapError(ValueError):
    """A holdout map does not classify its supplied data exactly once."""


class HoldoutStatus(StrEnum):
    """How much of research has already seen a span of dates."""

    CLEAN = "clean"
    SEEN = "seen"
    BURNED = "burned"

    @property
    def catalog_classification(self) -> DataClassification:
        if self is HoldoutStatus.CLEAN:
            return DataClassification.HOLDOUT
        return DataClassification.ORDINARY


@dataclass(frozen=True, slots=True)
class HoldoutSpan:
    """One inclusive date range of one symbol, with its declared status."""

    symbol: str
    name: str
    start: date
    end: date
    status: HoldoutStatus
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("HoldoutSpan.symbol must be non-empty")
        if not self.name:
            raise ValueError("HoldoutSpan.name must be non-empty")
        if self.start > self.end:
            raise ValueError(f"{self.symbol}/{self.name}: start must not follow end")
        if self.status is HoldoutStatus.BURNED and not self.reason.strip():
            raise ValueError(
                f"{self.symbol}/{self.name}: a burned span must record why it was consumed"
            )

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def to_mapping(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "status": str(self.status),
            "reason": self.reason,
        }


def validate_holdout_map(
    *,
    expected_symbols: Collection[str],
    series_by_symbol: Mapping[str, BarSeries],
    spans: Sequence[HoldoutSpan],
) -> None:
    """Require every supplied series date to belong to exactly one named span."""

    if not spans:
        raise HoldoutMapError("a release requires a complete holdout map")

    by_symbol: dict[str, list[HoldoutSpan]] = {}
    for span in spans:
        by_symbol.setdefault(span.symbol, []).append(span)

    expected = set(expected_symbols)
    declared = set(by_symbol)
    if declared != expected:
        raise HoldoutMapError(
            "holdout map symbols differ from the certification request: "
            f"map-only={sorted(declared - expected)}, "
            f"certification-only={sorted(expected - declared)}"
        )

    for symbol in sorted(expected):
        ordered = sorted(by_symbol[symbol], key=lambda span: (span.start, span.end))
        names: set[str] = set()
        for span in ordered:
            if span.name in names:
                raise HoldoutMapError(f"{symbol}: duplicate holdout span name {span.name!r}")
            names.add(span.name)

        series = series_by_symbol.get(symbol)
        if series is None or len(series) == 0:
            continue
        start = series.bars[0].session_date
        end = series.bars[-1].session_date
        if ordered[0].start > start:
            raise HoldoutMapError(
                f"{symbol}: holdout map starts {ordered[0].start.isoformat()}, leaving "
                f"session {start.isoformat()} undeclared"
            )
        if ordered[-1].end < end:
            raise HoldoutMapError(
                f"{symbol}: holdout map ends {ordered[-1].end.isoformat()}, leaving "
                f"session {end.isoformat()} undeclared"
            )
        previous = ordered[0]
        for span in ordered[1:]:
            if span.start <= previous.end:
                raise HoldoutMapError(
                    f"{symbol}: spans {previous.name!r} and {span.name!r} overlap; "
                    "a date cannot hold two classifications"
                )
            first_unclaimed = previous.end + timedelta(days=1)
            if span.start != first_unclaimed:
                raise HoldoutMapError(
                    f"{symbol}: gap between {previous.name!r} and {span.name!r} leaves "
                    f"session {first_unclaimed.isoformat()} undeclared"
                )
            previous = span


__all__ = [
    "HoldoutMapError",
    "HoldoutSpan",
    "HoldoutStatus",
    "validate_holdout_map",
]
