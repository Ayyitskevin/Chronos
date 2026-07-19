"""Holdout declaration + default-masked embargo (ADR-0011 §7).

A holdout window is a date range (optionally symbol-scoped) declared **up front**,
before any strategy reads the data, in ``research/data/history/HOLDOUTS.json``.
``embargoed_view`` returns a series with the declared windows **removed by default**;
only an explicit ``unlocked=True`` returns the full series.

Scope honesty (ADR-0011 §11.6 / §8): this is a default-masked *accessor*, not yet a
structural guardian — a caller that reads ``bars/<SYMBOL>.csv`` directly bypasses it.
The once-only, owner-typed, logged unlock and registry-brokered reads are **C2's**
holdout guardian; C1 provides the declaration and the default-masked read so a window
can be embargoed before any strategy sees it, and claims no more than that.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from chronos.histdata.store import read_bars
from chronos.marketdata.bars import BarSeries


@dataclass(frozen=True, slots=True)
class HoldoutWindow:
    """An inclusive ``[start, end]`` date range, optionally scoped to some symbols."""

    name: str
    start: date
    end: date
    symbols: tuple[str, ...] = ()  # empty = applies to every symbol
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("HoldoutWindow.name must be non-empty")
        if self.end < self.start:
            raise ValueError(f"holdout {self.name!r}: end {self.end} precedes start {self.start}")
        # Normalize scope symbols so matching is case-insensitive from any construction
        # path — a scoped embargo must never fail OPEN on a lowercase query symbol.
        object.__setattr__(self, "symbols", tuple(s.upper() for s in self.symbols))

    def applies_to(self, symbol: str) -> bool:
        return not self.symbols or symbol.upper() in self.symbols

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> HoldoutWindow:
        try:
            return cls(
                name=str(data["name"]),
                start=date.fromisoformat(str(data["start"])),
                end=date.fromisoformat(str(data["end"])),
                symbols=tuple(str(s).upper() for s in data.get("symbols", ()) or ()),
                reason=str(data.get("reason", "")),
            )
        except (KeyError, ValueError) as error:
            raise ValueError(f"invalid holdout window {dict(data)!r}: {error}") from error

    def to_mapping(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "symbols": list(self.symbols),
            "reason": self.reason,
        }


def holdouts_path(root: Path) -> Path:
    return root / "HOLDOUTS.json"


def load_holdouts(root: Path) -> tuple[HoldoutWindow, ...]:
    path = holdouts_path(root)
    if not path.exists():
        return ()
    doc = json.loads(path.read_text(encoding="utf-8"))
    return tuple(HoldoutWindow.from_mapping(item) for item in doc.get("windows", []))


def write_holdouts(root: Path, windows: Iterable[HoldoutWindow]) -> None:
    ordered = sorted(windows, key=lambda w: (w.start, w.end, w.name))
    payload = {"schema_version": 1, "windows": [w.to_mapping() for w in ordered]}
    path = holdouts_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def embargoed_view(
    series: BarSeries,
    holdouts: Iterable[HoldoutWindow],
    symbol: str,
    *,
    unlocked: bool = False,
) -> BarSeries:
    """Return ``series`` with declared holdout windows removed (unless ``unlocked``)."""

    if unlocked:
        return series
    applicable = [w for w in holdouts if w.applies_to(symbol)]
    if not applicable:
        return series
    kept = tuple(
        bar for bar in series.bars if not any(w.contains(bar.session_date) for w in applicable)
    )
    return BarSeries(symbol=series.symbol, interval=series.interval, bars=kept)


def read_embargoed_bars(root: Path, symbol: str, *, unlocked: bool = False) -> BarSeries:
    """Load a symbol's stored bars with declared holdouts masked by default."""

    return embargoed_view(read_bars(root, symbol), load_holdouts(root), symbol, unlocked=unlocked)
