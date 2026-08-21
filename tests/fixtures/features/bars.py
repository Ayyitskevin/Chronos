"""Deterministic synthetic bars for pairing-feature tests.  Not market data."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from chronos.marketdata.bars import Bar, BarInterval, BarSeries

START = datetime(2020, 1, 2, 21, tzinfo=UTC)


def daily_bars(
    symbol: str,
    *,
    count: int,
    start: datetime = START,
    close: float = 100.0,
    step: float = 0.0,
    volume: float = 2_000_000.0,
    shock_index: int | None = None,
    shock_return: float = -0.2,
    exchange: str = "NYSE",
) -> BarSeries:
    bars: list[Bar] = []
    price = close
    for index in range(count):
        timestamp = start + timedelta(days=index)
        if shock_index is not None and index == shock_index:
            price = price * (1.0 + shock_return)
        else:
            price = price + step
        bars.append(
            Bar(
                symbol=symbol,
                source="feature_fixture",
                exchange="AMEX" if symbol == "SPY" else exchange,
                interval=BarInterval.DAY_1,
                session_date=timestamp.date(),
                timestamp_utc=timestamp,
                open=price - 0.1,
                high=price + 0.4,
                low=price - 0.4,
                close=price,
                volume=volume,
            )
        )
    return BarSeries(symbol=symbol, interval=BarInterval.DAY_1, bars=tuple(bars))


def intraday_bars(
    symbol: str,
    *,
    days: int,
    bars_per_day: int,
    start: datetime = START,
    volume: float = 10_000.0,
    elevated_day: int | None = None,
) -> BarSeries:
    bars: list[Bar] = []
    price = 100.0
    for day in range(days):
        session = (start + timedelta(days=day)).date()
        for slot in range(bars_per_day):
            timestamp = start + timedelta(days=day, minutes=5 * (slot + 1))
            slot_volume = volume * (8.0 if day == elevated_day else 1.0)
            price += 0.01
            bars.append(
                Bar(
                    symbol=symbol,
                    source="feature_fixture",
                    exchange="NYSE",
                    interval=BarInterval.MIN_5,
                    session_date=session,
                    timestamp_utc=timestamp,
                    open=price - 0.02,
                    high=price + 0.03,
                    low=price - 0.03,
                    close=price,
                    volume=slot_volume,
                )
            )
    return BarSeries(symbol=symbol, interval=BarInterval.MIN_5, bars=tuple(bars))


def closes(series: BarSeries) -> Sequence[float]:
    return series.closes()
