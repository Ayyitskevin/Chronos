"""Historical-data client port (ADR-0011 §2).

The one boundary the data process fetches through. Implementations return
**unadjusted** as-traded daily bars (``marketdata.BarSeries``); adjustment is a
read-time concern handled elsewhere (``adjust.py``) and never crosses this port.

Two implementations exist: ``FakeHistoricalDataClient`` (tests, deterministic,
contacts nothing) drives every CI/dev path, and ``OfficialIBKRHistoricalClient``
(``official_client.py``, lazy ``ibapi``) is the owner-run gateway path — present
but unexercised in this environment (invariant 8).
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from chronos.marketdata.bars import BarSeries


class HistoricalDataError(RuntimeError):
    """A fetch could not be satisfied (no data, gateway error, bad request)."""


@runtime_checkable
class HistoricalDataClient(Protocol):
    """A source of unadjusted historical daily bars for one symbol at a time."""

    def connect(self) -> None:
        """Open the underlying connection (idempotent)."""

    def disconnect(self) -> None:
        """Close the underlying connection (idempotent)."""

    def fetch_daily_bars(self, symbol: str, *, end_date: date, duration_days: int) -> BarSeries:
        """Return unadjusted daily bars for ``symbol`` ending on ``end_date``.

        ``duration_days`` bounds how far back to request. The returned series is
        as-traded (never adjusted); ordering/uniqueness are guaranteed by
        ``BarSeries``. Raises :class:`HistoricalDataError` on failure.
        """
