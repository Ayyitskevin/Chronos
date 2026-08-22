"""The hourly lane must fail every way the daily lane fails, at bar granularity.

The sweep that preceded this build found two silent-wrong paths in a naive hourly
extension: the date-keyed merge collapsing a session to its last bar under
``allow_correction``, and the date-keyed ``sequence_id`` branding all valid hourly
data as duplicated. The tests here pin the repairs from both directions — hourly
works, and daily behavior is byte-identical to what it was.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from tests.support.histdata_fakes import FakeHistoricalDataClient

from chronos.histdata.adjust import AdjustmentError, AdjustmentView, adjust_series
from chronos.histdata.backfill import backfill_hourly_symbol, backfill_hourly_symbols
from chronos.histdata.client import HistoricalDataClient
from chronos.histdata.holdout import HoldoutWindow, read_embargoed_hourly_bars, write_holdouts
from chronos.histdata.official_client import _hourly_bar_from_row
from chronos.histdata.store import (
    StoreConflictError,
    StoreError,
    hourly_bars_path,
    read_bars,
    read_hourly_bars,
    write_bars,
    write_hourly_bars,
)
from chronos.marketdata.bars import Bar, BarInterval, BarSeries, BarStatus
from chronos.marketdata.csv_provider import load_daily_csv_bytes, load_hourly_csv_bytes
from chronos.marketdata.pacing import PacingController
from chronos.marketdata.quality import validate_series

_ET = ZoneInfo("America/New_York")
_CAPTURED = "2026-08-21T12:00:00+00:00"


def _hourly_bar(day: date, close_et: str, price: float = 100.0) -> Bar:
    hour, minute = (int(part) for part in close_et.split(":"))
    close = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute, tzinfo=_ET)
    return Bar(
        symbol="SPY",
        interval=BarInterval.HOUR_1,
        source="ibkr",
        exchange="SMART",
        session_date=day,
        timestamp_utc=close.astimezone(UTC),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=10_000,
        status=BarStatus.CLOSED,
    )


def _session(day: date, price: float = 100.0) -> list[Bar]:
    closes = ("10:30", "11:30", "12:30", "13:30", "14:30", "15:30", "16:00")
    return [_hourly_bar(day, c, price) for c in closes]


def _series(*days: date, price: float = 100.0) -> BarSeries:
    bars: list[Bar] = []
    for day in days:
        bars.extend(_session(day, price))
    return BarSeries(symbol="SPY", interval=BarInterval.HOUR_1, bars=tuple(bars))


def _daily_bar(day: date, price: float = 100.0) -> Bar:
    return Bar(
        symbol="SPY",
        interval=BarInterval.DAY_1,
        source="ibkr",
        exchange="SMART",
        session_date=day,
        timestamp_utc=datetime(day.year, day.month, day.day, 21, tzinfo=UTC),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=10_000,
        status=BarStatus.CLOSED,
    )


_MON = date(2024, 5, 6)
_TUE = date(2024, 5, 7)


# ----------------------------------------------------------------- bar identity


def test_daily_sequence_ids_are_byte_identical_to_the_historical_form() -> None:
    """This string participates in execution intent identity; it must not move."""

    assert _daily_bar(_MON).sequence_id == "ibkr:SPY:1d:2024-05-06"


def test_hourly_bars_of_one_session_have_distinct_identities() -> None:
    ids = [bar.sequence_id for bar in _session(_MON)]
    assert len(set(ids)) == 7


def test_a_full_hourly_session_passes_quality_validation() -> None:
    """Before the identity fix, all seven bars shared one id → blocking DUPLICATE_BAR."""

    report = validate_series(_series(_MON, _TUE))
    assert report.blocking is False
    assert not [i for i in report.issues if i.kind.value == "DUPLICATE_BAR"]


# ------------------------------------------------------------------- csv round trip


def test_hourly_csv_round_trips_timestamps_exactly(tmp_path: Path) -> None:
    series = _series(_MON)
    write_hourly_bars(tmp_path, series, captured_at=_CAPTURED)
    read_back = read_hourly_bars(tmp_path, "SPY")
    assert read_back.interval is BarInterval.HOUR_1
    assert [b.timestamp_utc for b in read_back.bars] == [b.timestamp_utc for b in series.bars]
    assert [b.session_date for b in read_back.bars] == [b.session_date for b in series.bars]


def test_the_daily_loader_refuses_a_timestamped_file(tmp_path: Path) -> None:
    """The old behavior silently truncated 'date' cells to ten characters."""

    raw = b"date,open,high,low,close,volume\n2024-05-06T14:30:00+00:00,1,1,1,1,10\n"
    with pytest.raises(ValueError, match="load_hourly_csv"):
        load_daily_csv_bytes(raw, path=tmp_path / "x.csv", symbol="SPY", source="ibkr")


def test_the_hourly_loader_refuses_a_daily_shaped_file(tmp_path: Path) -> None:
    raw = b"date,open,high,low,close,volume\n2024-05-06,1,1,1,1,10\n"
    with pytest.raises(ValueError, match="missing required hourly columns"):
        load_hourly_csv_bytes(raw, path=tmp_path / "x.csv", symbol="SPY", source="ibkr")


def test_the_hourly_loader_refuses_a_naive_timestamp(tmp_path: Path) -> None:
    raw = (
        b"timestamp_utc,session_date,open,high,low,close,volume\n"
        b"2024-05-06T14:30:00,2024-05-06,1,1,1,1,10\n"
    )
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        load_hourly_csv_bytes(raw, path=tmp_path / "x.csv", symbol="SPY", source="ibkr")


# ------------------------------------------------------------------------ the store


def test_hourly_rewrite_of_identical_data_is_a_no_op(tmp_path: Path) -> None:
    series = _series(_MON)
    first = write_hourly_bars(tmp_path, series, captured_at=_CAPTURED)
    again = write_hourly_bars(tmp_path, series, captured_at=_CAPTURED)
    assert first.rows_added == 7
    assert again.rows_added == 0 and again.corrections == ()


def test_a_changed_hourly_row_fails_closed_and_names_the_timestamp(tmp_path: Path) -> None:
    write_hourly_bars(tmp_path, _series(_MON), captured_at=_CAPTURED)
    changed = BarSeries(
        symbol="SPY",
        interval=BarInterval.HOUR_1,
        bars=tuple(
            _hourly_bar(_MON, "14:30", 55.0) if b.timestamp_utc.astimezone(_ET).hour == 14 else b
            for b in _session(_MON)
        ),
    )
    # The merge key is the UTC close timestamp; 14:30 EDT prints as 18:30 UTC.
    with pytest.raises(StoreConflictError, match="18:30"):
        write_hourly_bars(tmp_path, changed, captured_at=_CAPTURED)


def test_a_second_hourly_session_merges_without_conflict(tmp_path: Path) -> None:
    """The date-keyed merge treated bar 2 of a day as a conflict with bar 1.

    Worse, with ``allow_correction=True`` it silently kept only the LAST bar of
    each session and recorded the loss as legitimate corrections. Fourteen rows
    surviving proves the timestamp key; empty corrections proves no silent
    supersede happened.
    """

    write_hourly_bars(tmp_path, _series(_MON), captured_at=_CAPTURED)
    result = write_hourly_bars(
        tmp_path, _series(_MON, _TUE), captured_at=_CAPTURED, allow_correction=True
    )
    assert result.rows_written == 14
    assert result.rows_added == 7
    assert result.corrections == ()


def test_a_deliberate_hourly_supersede_records_the_exact_bar(tmp_path: Path) -> None:
    write_hourly_bars(tmp_path, _series(_MON), captured_at=_CAPTURED)
    changed = BarSeries(
        symbol="SPY",
        interval=BarInterval.HOUR_1,
        bars=tuple(
            _hourly_bar(_MON, "14:30", 55.0) if b.timestamp_utc.astimezone(_ET).hour == 14 else b
            for b in _session(_MON)
        ),
    )
    result = write_hourly_bars(tmp_path, changed, captured_at=_CAPTURED, allow_correction=True)
    assert len(result.corrections) == 1
    assert "T18:30" in result.corrections[0]  # 14:30 EDT == 18:30 UTC


def test_daily_and_hourly_lanes_coexist_per_symbol(tmp_path: Path) -> None:
    """Separate files, separate manifest entries — neither clobbers the other."""

    daily = BarSeries("SPY", BarInterval.DAY_1, (_daily_bar(_MON), _daily_bar(_TUE)))
    write_bars(tmp_path, daily, captured_at=_CAPTURED)
    write_hourly_bars(tmp_path, _series(_MON), captured_at=_CAPTURED)

    import json

    manifest = json.loads((tmp_path / "MANIFEST.json").read_text())
    entry = manifest["symbols"]["SPY"]
    assert entry["bars"]["rows"] == 2
    assert entry["bars_1h"]["rows"] == 7
    assert entry["bars_1h"]["interval"] == "1h"
    assert entry["bars_1h"]["start"].startswith("2024-05-06T")
    assert read_bars(tmp_path, "SPY").interval is BarInterval.DAY_1
    assert read_hourly_bars(tmp_path, "SPY").interval is BarInterval.HOUR_1


def test_write_hourly_bars_refuses_a_daily_series(tmp_path: Path) -> None:
    daily = BarSeries("SPY", BarInterval.DAY_1, (_daily_bar(_MON),))
    with pytest.raises(StoreError, match="HOUR_1 series only"):
        write_hourly_bars(tmp_path, daily, captured_at=_CAPTURED)


def test_hourly_lane_lives_in_its_own_tree(tmp_path: Path) -> None:
    write_hourly_bars(tmp_path, _series(_MON), captured_at=_CAPTURED)
    assert hourly_bars_path(tmp_path, "SPY") == tmp_path / "bars_1h" / "SPY.csv"
    assert hourly_bars_path(tmp_path, "SPY").exists()
    assert not (tmp_path / "bars" / "SPY.csv").exists()


# ----------------------------------------------------------------- the IBKR parser


def _ib_row(et_start: str, price: float = 100.0) -> SimpleNamespace:
    start = datetime.fromisoformat(et_start).replace(tzinfo=_ET)
    return SimpleNamespace(
        date=str(int(start.timestamp())),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1000,
    )


def test_parser_stamps_full_hours_and_caps_the_final_bar() -> None:
    first = _hourly_bar_from_row("SPY", "SMART", _ib_row("2024-05-06T09:30:00"))
    final = _hourly_bar_from_row("SPY", "SMART", _ib_row("2024-05-06T15:30:00"))
    assert first.timestamp_utc.astimezone(_ET).strftime("%H:%M") == "10:30"
    assert final.timestamp_utc.astimezone(_ET).strftime("%H:%M") == "16:00"
    assert first.session_date == final.session_date == _MON


def test_parser_caps_a_half_day_at_one_oclock() -> None:
    final = _hourly_bar_from_row("SPY", "SMART", _ib_row("2024-07-03T12:30:00"))
    assert final.timestamp_utc.astimezone(_ET).strftime("%H:%M") == "13:00"


def test_parser_handles_both_dst_regimes() -> None:
    summer = _hourly_bar_from_row("SPY", "SMART", _ib_row("2024-05-06T09:30:00"))
    winter = _hourly_bar_from_row("SPY", "SMART", _ib_row("2024-01-16T09:30:00"))
    assert summer.timestamp_utc == datetime(2024, 5, 6, 14, 30, tzinfo=UTC)
    assert winter.timestamp_utc == datetime(2024, 1, 16, 15, 30, tzinfo=UTC)


def test_parser_refuses_a_non_epoch_row_instead_of_guessing() -> None:
    """formatDate ambiguity is sidestepped by requesting epoch; a datetime string
    arriving anyway means the request and the response disagree — refuse."""

    from chronos.histdata.client import HistoricalDataError

    row = SimpleNamespace(date="20240506 09:30:00", open=1, high=1, low=1, close=1, volume=1)
    with pytest.raises(HistoricalDataError, match="refuses to guess"):
        _hourly_bar_from_row("SPY", "SMART", row)


def test_parser_dates_a_non_session_bar_without_inventing_a_close() -> None:
    """A Saturday bar is stored honestly (nominal span) for certification to flag."""

    bar = _hourly_bar_from_row("SPY", "SMART", _ib_row("2024-05-04T10:30:00"))
    assert bar.session_date == date(2024, 5, 4)
    assert bar.timestamp_utc.astimezone(_ET).strftime("%H:%M") == "11:30"


# ------------------------------------------------------------------- the coordinator


def _fake_with_hourly(*days: date) -> FakeHistoricalDataClient:
    fake = FakeHistoricalDataClient(hourly_by_symbol={"SPY": _series(*days)})
    fake.connect()
    return fake


def _now() -> datetime:
    return datetime(2024, 5, 8, 12, tzinfo=UTC)


def test_hourly_backfill_chunks_and_paces_every_request(tmp_path: Path) -> None:
    fake = _fake_with_hourly(_MON, _TUE)
    pacing = PacingController()
    result = backfill_hourly_symbol(
        fake,
        tmp_path,
        "SPY",
        end_date=date(2024, 5, 7),
        duration_days=90,
        chunk_days=30,
        pacing=pacing,
        now_fn=_now,
        captured_at=_CAPTURED,
        sleep=lambda _: None,
    )
    # 90 days in 30-day chunks = 3 requests, oldest first, distinct pacing keys.
    assert len(fake.hourly_fetch_calls) == 3
    ends = [call[1] for call in fake.hourly_fetch_calls]
    assert ends == sorted(ends)
    assert result.rows_written == 14
    assert result.rows_added == 14


def test_empty_chunks_before_available_depth_are_not_errors(tmp_path: Path) -> None:
    """Chunks before IBKR's intraday horizon come back empty; the run continues."""

    fake = _fake_with_hourly(_MON, _TUE)
    result = backfill_hourly_symbol(
        fake,
        tmp_path,
        "SPY",
        end_date=date(2024, 5, 7),
        duration_days=365,
        chunk_days=30,
        pacing=PacingController(),
        now_fn=_now,
        captured_at=_CAPTURED,
        sleep=lambda _: None,
    )
    assert len(fake.hourly_fetch_calls) == 13  # 12 full chunks + the 5-day remainder
    assert result.rows_added == 14  # only the two real sessions produced rows


def test_a_failing_symbol_isolates_in_the_hourly_coordinator(tmp_path: Path) -> None:
    fake = _fake_with_hourly(_MON)
    outcomes = backfill_hourly_symbols(
        fake,
        tmp_path,
        ["SPY", "QQQ"],  # QQQ has no canned hourly series
        end_date=date(2024, 5, 7),
        duration_days=30,
        pacing=PacingController(),
        now_fn=_now,
        captured_at=_CAPTURED,
        sleep=lambda _: None,
    )
    by_symbol = {o.symbol: o for o in outcomes}
    assert by_symbol["SPY"].error is None
    assert by_symbol["QQQ"].result is None
    assert "no canned hourly series" in (by_symbol["QQQ"].error or "")


def test_the_fake_still_satisfies_the_extended_protocol() -> None:
    assert isinstance(FakeHistoricalDataClient(), HistoricalDataClient)


def test_chunk_parameters_are_validated(tmp_path: Path) -> None:
    fake = _fake_with_hourly(_MON)
    with pytest.raises(ValueError, match="chunk_days"):
        backfill_hourly_symbol(
            fake,
            tmp_path,
            "SPY",
            end_date=_TUE,
            duration_days=30,
            chunk_days=0,
            pacing=PacingController(),
            now_fn=_now,
            captured_at=_CAPTURED,
            sleep=lambda _: None,
        )


# ------------------------------------------------------------- embargo and adjustment


def test_a_date_window_masks_every_intraday_bar_of_the_session(tmp_path: Path) -> None:
    write_hourly_bars(tmp_path, _series(_MON, _TUE), captured_at=_CAPTURED)
    write_holdouts(tmp_path, [HoldoutWindow(name="w", start=_MON, end=_MON)])
    masked = read_embargoed_hourly_bars(tmp_path, "SPY")
    assert len(masked) == 7
    assert all(bar.session_date == _TUE for bar in masked.bars)
    unlocked = read_embargoed_hourly_bars(tmp_path, "SPY", unlocked=True)
    assert len(unlocked) == 14


def test_the_embargo_holds_for_a_bar_whose_utc_date_differs(tmp_path: Path) -> None:
    """A 19:59 ET close on a masked Friday is already Saturday in UTC; the mask
    keys on session_date, so it must still drop."""

    friday = date(2024, 5, 3)
    late = Bar(
        symbol="SPY",
        interval=BarInterval.HOUR_1,
        source="ibkr",
        exchange="SMART",
        session_date=friday,
        timestamp_utc=datetime(2024, 5, 3, 23, 59, tzinfo=UTC) + timedelta(minutes=2),
        open=1,
        high=1,
        low=1,
        close=1,
        volume=1,
        status=BarStatus.CLOSED,
    )
    assert late.timestamp_utc.date() != friday  # the trap this test exists for
    series = BarSeries("SPY", BarInterval.HOUR_1, (*_session(friday), late))
    write_hourly_bars(tmp_path, series, captured_at=_CAPTURED)
    write_holdouts(tmp_path, [HoldoutWindow(name="w", start=friday, end=friday)])
    assert len(read_embargoed_hourly_bars(tmp_path, "SPY")) == 0


def test_adjusted_views_over_hourly_series_refuse(tmp_path: Path) -> None:
    """C_ref is an official daily closing print; an hourly series has none."""

    with pytest.raises(AdjustmentError, match="daily series only"):
        adjust_series(_series(_MON), (), AdjustmentView.SPLIT_ADJUSTED)


def test_raw_view_over_hourly_series_is_allowed() -> None:
    result = adjust_series(_series(_MON), (), AdjustmentView.RAW)
    assert result.series.interval is BarInterval.HOUR_1
