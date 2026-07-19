"""Deterministic fake historical-data client for tests (ADR-0011 §2).

Records calls and returns canned unadjusted bars; contacts no gateway. Every
CI/dev path uses this — the ``FakeMarketDataBroker`` / ``FakeBroker`` pattern
applied to ``reqHistoricalData``. No real ``ibapi`` and no network, ever.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from chronos.histdata.client import HistoricalDataError
from chronos.marketdata.bars import BarSeries


class FakeHistoricalDataClient:
    """A ``HistoricalDataClient`` backed by in-memory canned series."""

    def __init__(self, series_by_symbol: Mapping[str, BarSeries] | None = None) -> None:
        self._series: dict[str, BarSeries] = dict(series_by_symbol or {})
        self.connected = False
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.fetch_calls: list[tuple[str, date, int]] = []

    def set_series(self, symbol: str, series: BarSeries) -> None:
        self._series[symbol] = series

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
