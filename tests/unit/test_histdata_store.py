"""Historical-data store + backfill tests (AI Quant plan C1-d, ADR-0011 §3).

Covers idempotent append, fail-closed conflict + logged correction, the quality
gate on write, provenance manifest (bars + actions hashes), corporate-action
round-trip, and the backfill coordinator over the fake client (no real waiting).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.support.histdata_fakes import FakeHistoricalDataClient

from chronos.histdata.backfill import backfill_symbol, backfill_symbols
from chronos.histdata.corporate_actions import ActionKind, CorporateAction
from chronos.histdata.pacing import PacingController
from chronos.histdata.store import (
    StoreConflictError,
    StoreError,
    manifest_path,
    read_actions,
    read_bars,
    write_actions,
    write_bars,
)
from chronos.marketdata.bars import Bar, BarInterval, BarSeries

_CAPTURED = "2024-06-01T00:00:00+00:00"


def _bar(day: int, close: float, *, high: float | None = None, low: float | None = None) -> Bar:
    return Bar(
        symbol="SPY",
        source="ibkr",
        exchange="SMART",
        interval=BarInterval.DAY_1,
        session_date=date(2024, 1, day),
        timestamp_utc=datetime(2024, 1, day, 21, 0, tzinfo=UTC),
        open=close,
        high=high if high is not None else close + 1,
        low=low if low is not None else close - 1,
        close=close,
        volume=1000,
    )


def _series(*bars: Bar) -> BarSeries:
    return BarSeries(symbol="SPY", interval=BarInterval.DAY_1, bars=bars)


def _manifest(root: Path) -> dict:
    return json.loads(manifest_path(root).read_text(encoding="utf-8"))


# --- bars: write / read / idempotency / conflict ----------------------------


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    result = write_bars(tmp_path, _series(_bar(2, 100), _bar(3, 101)), captured_at=_CAPTURED)
    assert result.rows_written == 2
    assert result.rows_added == 2
    out = read_bars(tmp_path, "SPY")
    assert [(b.session_date.day, b.close) for b in out] == [(2, 100.0), (3, 101.0)]


def test_idempotent_rewrite_of_identical_rows_is_a_no_op(tmp_path: Path) -> None:
    write_bars(tmp_path, _series(_bar(2, 100), _bar(3, 101)), captured_at=_CAPTURED)
    result = write_bars(tmp_path, _series(_bar(2, 100), _bar(3, 101)), captured_at=_CAPTURED)
    assert result.rows_added == 0
    assert result.corrections == ()
    assert len(read_bars(tmp_path, "SPY")) == 2


def test_new_dates_are_appended(tmp_path: Path) -> None:
    write_bars(tmp_path, _series(_bar(2, 100)), captured_at=_CAPTURED)
    result = write_bars(tmp_path, _series(_bar(3, 101), _bar(4, 102)), captured_at=_CAPTURED)
    assert result.rows_added == 2
    assert [b.session_date.day for b in read_bars(tmp_path, "SPY")] == [2, 3, 4]


def test_conflicting_row_fails_closed(tmp_path: Path) -> None:
    write_bars(tmp_path, _series(_bar(2, 100)), captured_at=_CAPTURED)
    with pytest.raises(StoreConflictError):
        write_bars(tmp_path, _series(_bar(2, 999)), captured_at=_CAPTURED)
    # The stored row is untouched by the rejected write.
    assert read_bars(tmp_path, "SPY")[0].close == 100.0


def test_allow_correction_supersedes_and_records(tmp_path: Path) -> None:
    write_bars(tmp_path, _series(_bar(2, 100)), captured_at=_CAPTURED)
    result = write_bars(
        tmp_path, _series(_bar(2, 105)), captured_at=_CAPTURED, allow_correction=True
    )
    assert result.corrections == ("2024-01-02",)
    assert read_bars(tmp_path, "SPY")[0].close == 105.0
    assert _manifest(tmp_path)["symbols"]["SPY"]["bars"]["corrections"] == ["2024-01-02"]


def test_float_spelling_difference_is_not_a_conflict(tmp_path: Path) -> None:
    # 100.0 vs 100.0000004 canonicalize equal (< 1e-6) — no false conflict (D5).
    write_bars(tmp_path, _series(_bar(2, 100.0)), captured_at=_CAPTURED)
    result = write_bars(tmp_path, _series(_bar(2, 100.0000004)), captured_at=_CAPTURED)
    assert result.rows_added == 0


def test_blocking_quality_issue_aborts_the_write(tmp_path: Path) -> None:
    # high < low is an impossible OHLC — the quality gate blocks, nothing is written.
    bad = _bar(2, 100, high=5, low=8)
    with pytest.raises(StoreError):
        write_bars(tmp_path, _series(bad), captured_at=_CAPTURED)
    assert not (tmp_path / "bars" / "SPY.csv").exists()


def test_manifest_records_provenance(tmp_path: Path) -> None:
    write_bars(tmp_path, _series(_bar(2, 100), _bar(3, 101)), captured_at=_CAPTURED)
    entry = _manifest(tmp_path)["symbols"]["SPY"]["bars"]
    assert entry["adjusted"] is False
    assert entry["rows"] == 2
    assert entry["start"] == "2024-01-02"
    assert entry["end"] == "2024-01-03"
    assert entry["captured_at"] == _CAPTURED
    assert len(entry["sha256"]) == 64


# --- corporate actions ------------------------------------------------------


def test_actions_round_trip_and_hash(tmp_path: Path) -> None:
    actions = (
        CorporateAction(ActionKind.SPLIT, date(2024, 6, 7), 4.0, "ibkr"),
        CorporateAction(ActionKind.CASH_DIVIDEND, date(2024, 3, 15), 0.24, "ibkr"),
    )
    write_actions(tmp_path, "SPY", actions, captured_at=_CAPTURED)
    loaded = read_actions(tmp_path, "SPY")
    # Sorted by ex-date on write.
    assert [a.ex_date for a in loaded] == [date(2024, 3, 15), date(2024, 6, 7)]
    ca = _manifest(tmp_path)["symbols"]["SPY"]["corporate_actions"]
    assert ca["count"] == 2
    assert len(ca["sha256"]) == 64


def test_read_actions_absent_is_empty(tmp_path: Path) -> None:
    assert read_actions(tmp_path, "NONE") == ()


# --- backfill coordinator ---------------------------------------------------


def _canned(symbol: str, days: list[tuple[int, float]]) -> BarSeries:
    bars = tuple(
        Bar(
            symbol=symbol,
            source="ibkr",
            exchange="SMART",
            interval=BarInterval.DAY_1,
            session_date=date(2024, 1, day),
            timestamp_utc=datetime(2024, 1, day, 21, 0, tzinfo=UTC),
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=1000,
        )
        for day, close in days
    )
    return BarSeries(symbol=symbol, interval=BarInterval.DAY_1, bars=bars)


def test_backfill_symbol_fetches_paces_and_writes(tmp_path: Path) -> None:
    client = FakeHistoricalDataClient({"SPY": _canned("SPY", [(2, 100), (3, 101)])})
    client.connect()
    slept: list[float] = []
    result = backfill_symbol(
        client,
        tmp_path,
        "SPY",
        end_date=date(2024, 1, 31),
        duration_days=30,
        pacing=PacingController(),
        now_fn=lambda: datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        captured_at=_CAPTURED,
        sleep=slept.append,
    )
    assert result.rows_written == 2
    assert client.fetch_calls == [("SPY", date(2024, 1, 31), 30)]
    assert slept == []  # first request, no pacing delay
    assert [b.close for b in read_bars(tmp_path, "SPY")] == [100.0, 101.0]


def test_backfill_symbols_isolates_a_failure(tmp_path: Path) -> None:
    client = FakeHistoricalDataClient({"SPY": _canned("SPY", [(2, 100)])})
    client.connect()
    outcomes = backfill_symbols(
        client,
        tmp_path,
        ["SPY", "MISSING"],
        end_date=date(2024, 1, 31),
        duration_days=30,
        pacing=PacingController(),
        now_fn=lambda: datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        captured_at=_CAPTURED,
        sleep=lambda _: None,
    )
    by_symbol = {o.symbol: o for o in outcomes}
    assert by_symbol["SPY"].result is not None
    assert by_symbol["MISSING"].result is None
    assert by_symbol["MISSING"].error is not None
