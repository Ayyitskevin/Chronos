"""Historical-data client port (ADR-0011 §2).

The one boundary the data process fetches through. Implementations return
**unadjusted** as-traded bars (``marketdata.BarSeries``); adjustment is a
read-time concern handled elsewhere (``adjust.py``) and never crosses this port.
Daily and hourly are separate methods rather than an interval parameter, because
their request semantics differ at the gateway (IBKR caps request duration per
bar size — ADR-0029), and a caller must confront that instead of passing a flag.

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
    """A source of unadjusted historical bars for one symbol at a time."""

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

    def fetch_hourly_bars(self, symbol: str, *, end_date: date, duration_days: int) -> BarSeries:
        """Return unadjusted HOUR_1 bars for one bounded chunk ending on ``end_date``.

        One call is ONE gateway request: chunking a long backfill into cap-sized
        requests belongs to the coordinator (``backfill.py``), never in here —
        chunks issued inside a client would bypass the pacing controller entirely.
        ``duration_days`` must respect IBKR's per-bar-size duration cap; the
        conservative chunk default lives with the coordinator. Raises
        :class:`HistoricalDataError` on failure.
        """
