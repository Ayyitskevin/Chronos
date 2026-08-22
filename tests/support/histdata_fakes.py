"""Deterministic fake historical-data client for tests (ADR-0011 §2).

Records calls and returns canned unadjusted bars; contacts no gateway. Every
CI/dev path uses this — the ``FakeMarketDataBroker`` / ``FakeBroker`` pattern
applied to ``reqHistoricalData``. No real ``ibapi`` and no network, ever.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta

from chronos.histdata.client import HistoricalDataError
from chronos.marketdata.bars import BarSeries


class FakeHistoricalDataClient:
    """A ``HistoricalDataClient`` backed by in-memory canned series."""

    def __init__(
        self,
        series_by_symbol: Mapping[str, BarSeries] | None = None,
        hourly_by_symbol: Mapping[str, BarSeries] | None = None,
    ) -> None:
        self._series: dict[str, BarSeries] = dict(series_by_symbol or {})
        self._hourly: dict[str, BarSeries] = dict(hourly_by_symbol or {})
        self.connected = False
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.fetch_calls: list[tuple[str, date, int]] = []
        #: hourly calls carry the interval tag so tests can pin exactly what was
        #: requested; the daily list keeps its historical shape untouched.
        self.hourly_fetch_calls: list[tuple[str, date, int]] = []

    def set_series(self, symbol: str, series: BarSeries) -> None:
        self._series[symbol] = series

    def set_hourly_series(self, symbol: str, series: BarSeries) -> None:
        self._hourly[symbol] = series

    def connect(self) -> None:
        self.connect_calls += 1
        self.connected = True

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    def fetch_daily_bars(self, symbol: str, *, end_date: date, duration_days: int) -> BarSeries:
        self.fetch_calls.append((symbol, end_date, duration_days))
        if not self.connected:
            raise HistoricalDataError("fake client not connected")
        if symbol not in self._series:
            raise HistoricalDataError(f"no canned series for {symbol!r}")
        source = self._series[symbol]
        bars = tuple(bar for bar in source.bars if bar.session_date <= end_date)
        return BarSeries(symbol=source.symbol, interval=source.interval, bars=bars)

    def fetch_hourly_bars(self, symbol: str, *, end_date: date, duration_days: int) -> BarSeries:
        """One chunk of the canned hourly series.

        Filtering is timestamp-aware over the chunk's date span: a session_date
        cutoff alone would return the whole history for every chunk and hide
        chunking bugs the coordinator tests exist to catch.
        """

        self.hourly_fetch_calls.append((symbol, end_date, duration_days))
        if not self.connected:
            raise HistoricalDataError("fake client not connected")
        if symbol not in self._hourly:
            raise HistoricalDataError(f"no canned hourly series for {symbol!r}")
        source = self._hourly[symbol]
        window_start = end_date - timedelta(days=duration_days)
        bars = tuple(bar for bar in source.bars if window_start < bar.session_date <= end_date)
        return BarSeries(symbol=source.symbol, interval=source.interval, bars=bars)
