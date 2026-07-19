"""Holdout declaration + embargo tests (AI Quant plan C1-e, ADR-0011 §7).

The embargo is default-masked: a declared window is removed from the read unless an
explicit unlock is passed. Covers symbol scoping, global windows, the no-window
no-op, the model's validation + round-trip, and the store-composed reader.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from chronos.histdata.holdout import (
    HoldoutWindow,
    embargoed_view,
    load_holdouts,
    read_embargoed_bars,
    write_holdouts,
)
from chronos.histdata.store import write_bars
from chronos.marketdata.bars import Bar, BarInterval, BarSeries

_CAPTURED = "2024-06-01T00:00:00+00:00"


def _series(symbol: str, days: list[int]) -> BarSeries:
    bars = tuple(
        Bar(
            symbol=symbol,
            source="ibkr",
            exchange="SMART",
            interval=BarInterval.DAY_1,
            session_date=date(2024, 1, day),
            timestamp_utc=datetime(2024, 1, day, 21, 0, tzinfo=UTC),
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1000,
        )
        for day in days
    )
    return BarSeries(symbol=symbol, interval=BarInterval.DAY_1, bars=bars)


def test_declared_window_is_masked_by_default_and_visible_when_unlocked() -> None:
    series = _series("SPY", [2, 3, 4, 5])
    window = HoldoutWindow("q1", date(2024, 1, 3), date(2024, 1, 4))
    masked = embargoed_view(series, [window], "SPY")
    assert [b.session_date.day for b in masked] == [2, 5]  # 3,4 embargoed
    unlocked = embargoed_view(series, [window], "SPY", unlocked=True)
    assert [b.session_date.day for b in unlocked] == [2, 3, 4, 5]


def test_symbol_scoped_window_only_masks_that_symbol() -> None:
    series = _series("QQQ", [2, 3, 4])
    window = HoldoutWindow("spy-only", date(2024, 1, 3), date(2024, 1, 3), symbols=("SPY",))
    assert [b.session_date.day for b in embargoed_view(series, [window], "QQQ")] == [2, 3, 4]
    assert [b.session_date.day for b in embargoed_view(series, [window], "SPY")] == [2, 4]


def test_global_window_masks_every_symbol() -> None:
    window = HoldoutWindow("global", date(2024, 1, 3), date(2024, 1, 3))
    for symbol in ("SPY", "QQQ"):
        series = _series(symbol, [2, 3, 4])
        assert [b.session_date.day for b in embargoed_view(series, [window], symbol)] == [2, 4]


def test_no_windows_is_a_no_op() -> None:
    series = _series("SPY", [2, 3, 4])
    assert embargoed_view(series, [], "SPY") is series


def test_window_rejects_inverted_range() -> None:
    with pytest.raises(ValueError):
        HoldoutWindow("bad", date(2024, 1, 5), date(2024, 1, 1))


def test_holdouts_round_trip(tmp_path: Path) -> None:
    windows = (
        HoldoutWindow("w2", date(2024, 6, 1), date(2024, 6, 30), reason="reserved"),
        HoldoutWindow("w1", date(2024, 1, 1), date(2024, 3, 31), symbols=("SPY", "QQQ")),
    )
    write_holdouts(tmp_path, windows)
    loaded = load_holdouts(tmp_path)
    assert [w.name for w in loaded] == ["w1", "w2"]  # sorted by start
    assert loaded[0].symbols == ("SPY", "QQQ")


def test_load_holdouts_absent_is_empty(tmp_path: Path) -> None:
    assert load_holdouts(tmp_path) == ()


def test_read_embargoed_bars_masks_stored_holdout(tmp_path: Path) -> None:
    write_bars(tmp_path, _series("SPY", [2, 3, 4, 5]), captured_at=_CAPTURED)
    write_holdouts(tmp_path, [HoldoutWindow("q", date(2024, 1, 4), date(2024, 1, 5))])
    masked = read_embargoed_bars(tmp_path, "SPY")
    assert [b.session_date.day for b in masked] == [2, 3]
    full = read_embargoed_bars(tmp_path, "SPY", unlocked=True)
    assert [b.session_date.day for b in full] == [2, 3, 4, 5]
