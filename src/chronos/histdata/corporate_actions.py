"""Corporate-action event stream for the historical-data plane (ADR-0011 §4).

A ``CorporateAction`` is a split or a cash dividend with its ex-date and its
*native, as-of-ex-date* value — the split ratio ``r`` (4-for-1 ⇒ 4; reverse
1-for-10 ⇒ 0.1) or the cash amount per share ``d``. The value is stored in the
basis of its own ex-date and **never restated to a later split's terms**: the
read-time adjustment (``adjust.py``) pairs a dividend's ``d`` with the *unadjusted*
close before its ex-date, so a split-restated ``d`` would silently under-count the
dividend by the split factor (ADR-0011 §11.11 / D2).

The models are frozen dataclasses matching the ``marketdata`` bar vocabulary they
sit beside; ``from_mapping`` / ``to_mapping`` are the JSON round-trip the store
(C1-d) reads and writes. This module imports nothing outside the standard library.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any


class ActionKind(StrEnum):
    SPLIT = "split"
    CASH_DIVIDEND = "cash_dividend"


@dataclass(frozen=True, slots=True)
class CorporateAction:
    """One split or cash dividend, in native as-of-ex-date basis.

    ``value`` is the split **ratio** (``> 0``; a reverse split is ``< 1``) for
    ``SPLIT``, or the cash **amount per share** (``> 0``) for ``CASH_DIVIDEND``.
    """

    kind: ActionKind
    ex_date: date
    value: float
    source: str
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.ex_date, date):
            raise ValueError("CorporateAction.ex_date must be a date")
        if self.kind is ActionKind.SPLIT and not self.value > 0:
            raise ValueError(f"split ratio must be > 0, got {self.value!r}")
        if self.kind is ActionKind.CASH_DIVIDEND and not self.value > 0:
            raise ValueError(f"cash dividend amount must be > 0, got {self.value!r}")
        if not self.source:
            raise ValueError("CorporateAction.source must be non-empty (provenance)")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> CorporateAction:
        """Parse one action from a JSON mapping; fails closed on bad input."""

        try:
            kind = ActionKind(str(data["kind"]))
            ex_date = date.fromisoformat(str(data["ex_date"]))
            value = float(data["value"])
        except (KeyError, ValueError) as error:
            raise ValueError(f"invalid corporate action {dict(data)!r}: {error}") from error
        return cls(
            kind=kind,
            ex_date=ex_date,
            value=value,
            source=str(data.get("source", "")),
            note=str(data.get("note", "")),
        )

    def to_mapping(self) -> dict[str, Any]:
        """Serialize to a JSON-ready mapping (deterministic key order)."""

        return {
            "kind": self.kind.value,
            "ex_date": self.ex_date.isoformat(),
            "value": self.value,
            "source": self.source,
            "note": self.note,
        }
