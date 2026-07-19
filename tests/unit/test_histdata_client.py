"""Historical-data client + pacing tests (AI Quant plan C1-c, ADR-0011 §2/§5).

Covers the fake client's contract (connect/fetch/records, fail-closed) and the
pacing controller's rolling-window + per-key-cooldown budget against an injected
clock (no real sleeping). The official gateway client is owner-run and unexercised
here; a separate isolation test (C1-d) proves its ``ibapi`` import stays lazy.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from tests.support.histdata_fakes import FakeHistoricalDataClient

from chronos.histdata import HistoricalDataError, PacingController
from chronos.histdata.client import HistoricalDataClient
from chronos.marketdata.bars import Bar, BarInterval, BarSeries


def _series(symbol: str, days: list[tuple[int, float]]) -> BarSeries:
    bars = tuple(
        Bar(
            symbol=symbol,
            source="ibkr",
            exchange="SMART",
            interval=BarInterval.DAY_1,
            session_date=date(2024, 1, day),
            timestamp_utc=datetime(2024, 1, day, 21, 0, tzinfo=UTC),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1000,
        )
        for day, close in days
    )
    return BarSeries(symbol=symbol, interval=BarInterval.DAY_1, bars=bars)


# --- fake client ------------------------------------------------------------


def test_fake_client_satisfies_the_protocol() -> None:
    assert isinstance(FakeHistoricalDataClient(), HistoricalDataClient)


def test_fake_client_records_calls_and_returns_bars_up_to_end_date() -> None:
    client = FakeHistoricalDataClient({"SPY": _series("SPY", [(2, 100), (3, 101), (4, 102)])})
    client.connect()
    out = client.fetch_daily_bars("SPY", end_date=date(2024, 1, 3), duration_days=30)
    assert [b.session_date.day for b in out] == [2, 3]  # 4th is after end_date
    assert client.connect_calls == 1
    assert client.fetch_calls == [("SPY", date(2024, 1, 3), 30)]


def test_fake_client_fails_closed_when_not_connected() -> None:
    client = FakeHistoricalDataClient({"SPY": _series("SPY", [(2, 100)])})
    with pytest.raises(HistoricalDataError):
        client.fetch_daily_bars("SPY", end_date=date(2024, 1, 2), duration_days=5)


def test_fake_client_fails_closed_on_unknown_symbol() -> None:
    client = FakeHistoricalDataClient()
    client.connect()
    with pytest.raises(HistoricalDataError):
        client.fetch_daily_bars("NOPE", end_date=date(2024, 1, 2), duration_days=5)


# --- pacing controller ------------------------------------------------------


def _t(second: int) -> datetime:
    return datetime(2024, 1, 1, 12, 0, second, tzinfo=UTC)


def test_pacing_allows_up_to_the_window_budget_then_blocks() -> None:
    pacing = PacingController(max_per_window=3, window=timedelta(seconds=60))
    # Three distinct keys so the per-key cooldown never binds; only the window cap does.
    for i in range(3):
        key = f"k{i}"
        assert pacing.delay_before(key, _t(i)) == 0.0
        pacing.record(key, _t(i))
    # A 4th request is blocked until the oldest (t=0) leaves the 60s window.
    wait = pacing.delay_before("k3", _t(10))
    assert wait == pytest.approx(50.0)  # 0 + 60 - 10


def test_pacing_window_frees_as_time_passes() -> None:
    pacing = PacingController(max_per_window=1, window=timedelta(seconds=30))
    pacing.record("a", _t(0))
    assert pacing.delay_before("b", _t(10)) == pytest.approx(20.0)  # 0 + 30 - 10
    assert pacing.delay_before("b", _t(30)) == 0.0  # window elapsed


def test_pacing_per_key_cooldown_binds_independently() -> None:
    pacing = PacingController(max_per_window=100, cooldown=timedelta(seconds=15))
    pacing.record("SPY", _t(0))
    assert pacing.delay_before("SPY", _t(5)) == pytest.approx(10.0)  # 0 + 15 - 5
    assert pacing.delay_before("QQQ", _t(5)) == 0.0  # different key, no cooldown
    assert pacing.delay_before("SPY", _t(15)) == 0.0  # cooldown elapsed


def test_pacing_takes_the_max_of_window_and_cooldown_waits() -> None:
    pacing = PacingController(
        max_per_window=1, window=timedelta(seconds=60), cooldown=timedelta(seconds=15)
    )
    pacing.record("SPY", _t(0))
    # Window says wait 50s (@t=10), cooldown says 5s; the larger governs.
    assert pacing.delay_before("SPY", _t(10)) == pytest.approx(50.0)


def test_pacing_rejects_non_positive_budget() -> None:
    with pytest.raises(ValueError):
        PacingController(max_per_window=0)
