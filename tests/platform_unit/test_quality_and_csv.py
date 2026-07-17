"""Data-quality validation and the strict CSV bar provider."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.marketdata.csv_provider import load_daily_csv
from chronos.marketdata.quality import IssueKind, validate_series


def make_bar(
    session: date,
    *,
    open_: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.5,
    volume: float = 1_000_000.0,
    hour: int = 21,
) -> Bar:
    return Bar(
        symbol="SPY",
        source="test",
        exchange="ARCA",
        interval=BarInterval.DAY_1,
        session_date=session,
        timestamp_utc=datetime.combine(session, time(hour, 0), tzinfo=UTC),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def series_of(*bars: Bar) -> BarSeries:
    return BarSeries(symbol="SPY", interval=BarInterval.DAY_1, bars=tuple(bars))


# Consecutive weekdays: Mon 2024-06-03 .. Fri 2024-06-07.
MON, TUE, WED, THU, FRI = (date(2024, 6, 3) + timedelta(days=i) for i in range(5))
SAT = date(2024, 6, 8)


class TestValidateSeries:
    def test_clean_weekday_series_is_ok(self) -> None:
        report = validate_series(series_of(make_bar(MON), make_bar(TUE), make_bar(WED)))
        assert report.ok
        assert not report.blocking
        assert report.bar_count == 3

    def test_impossible_ohlc_is_blocking(self) -> None:
        # close above high is impossible
        bad = make_bar(TUE, open_=100.0, high=100.5, low=99.0, close=101.0)
        report = validate_series(series_of(make_bar(MON), bad))
        kinds = [issue.kind for issue in report.issues]
        assert IssueKind.IMPOSSIBLE_OHLC in kinds
        assert report.blocking

    def test_negative_volume_is_blocking(self) -> None:
        report = validate_series(series_of(make_bar(MON, volume=-1.0)))
        kinds = [issue.kind for issue in report.issues]
        assert IssueKind.NEGATIVE_VOLUME in kinds
        assert report.blocking

    def test_duplicate_session_date_is_blocking(self) -> None:
        # Same session date at two timestamps: BarSeries accepts it (strictly
        # increasing timestamps) but the sequence_id collides.
        report = validate_series(series_of(make_bar(MON, hour=21), make_bar(MON, hour=22)))
        kinds = [issue.kind for issue in report.issues]
        assert IssueKind.DUPLICATE_BAR in kinds
        assert report.blocking

    def test_weekend_bar_flag_is_non_blocking(self) -> None:
        report = validate_series(series_of(make_bar(FRI), make_bar(SAT)))
        weekend = [i for i in report.issues if i.kind is IssueKind.WEEKEND_BAR]
        assert len(weekend) == 1
        assert not weekend[0].blocking
        assert not report.blocking  # only the weekend flag, so still tradeable

    def test_extreme_return_flag_is_non_blocking(self) -> None:
        # 100.5 -> 140.7 close-to-close is exactly +40% > 35% threshold
        jump = make_bar(TUE, open_=140.0, high=141.0, low=139.0, close=140.7)
        report = validate_series(series_of(make_bar(MON), jump))
        extreme = [i for i in report.issues if i.kind is IssueKind.EXTREME_RETURN]
        assert len(extreme) == 1
        assert not extreme[0].blocking
        assert not report.blocking

    def test_empty_series_is_blocking(self) -> None:
        report = validate_series(series_of())
        assert [issue.kind for issue in report.issues] == [IssueKind.EMPTY_SERIES]
        assert report.blocking
        assert report.bar_count == 0

    def test_non_positive_price_is_blocking(self) -> None:
        report = validate_series(series_of(make_bar(MON, low=0.0)))
        kinds = [issue.kind for issue in report.issues]
        assert IssueKind.NON_POSITIVE_PRICE in kinds
        assert report.blocking


class TestCsvProvider:
    def test_header_normalization_and_adj_close(self, tmp_path: Path) -> None:
        path = tmp_path / "spy.csv"
        path.write_text(
            " Date ,Open, High ,Low,Close,Volume,Adj Close\n"
            "2024-06-03,100,101,99,100.5,1000,99.5\n"
            "2024-06-04,100.5,102,100,101.5,1100,100.4\n",
            encoding="utf-8",
        )
        loaded = load_daily_csv(path, symbol="SPY", source="test")
        assert loaded.has_adjusted_close
        assert len(loaded.series) == 2
        first = loaded.series[0]
        assert first.session_date == date(2024, 6, 3)
        assert first.open == 100.0
        assert first.high == 101.0
        assert first.low == 99.0
        assert first.close == 100.5
        assert first.volume == 1000.0
        assert first.adjusted_close == 99.5

    def test_adjusted_close_is_optional(self, tmp_path: Path) -> None:
        path = tmp_path / "spy.csv"
        path.write_text(
            "date,open,high,low,close,volume\n2024-06-03,100,101,99,100.5,1000\n",
            encoding="utf-8",
        )
        loaded = load_daily_csv(path, symbol="SPY", source="test")
        assert not loaded.has_adjusted_close
        assert loaded.series[0].adjusted_close is None

    def test_missing_required_column_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "spy.csv"
        path.write_text("date,open,high,low,close\n2024-06-03,100,101,99,100.5\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"missing required columns.*volume"):
            load_daily_csv(path, symbol="SPY", source="test")

    def test_unparseable_row_raises_with_line_number(self, tmp_path: Path) -> None:
        path = tmp_path / "spy.csv"
        path.write_text(
            "date,open,high,low,close,volume\n"
            "2024-06-03,100,101,99,100.5,1000\n"
            "2024-06-04,oops,102,100,101.5,1100\n",
            encoding="utf-8",
        )
        # first data row is file line 2, so the bad second row is line 3
        with pytest.raises(ValueError, match=r":3: unparseable row"):
            load_daily_csv(path, symbol="SPY", source="test")

    def test_file_hash_is_stable_and_matches_bytes(self, tmp_path: Path) -> None:
        path = tmp_path / "spy.csv"
        path.write_text(
            "date,open,high,low,close,volume\n2024-06-03,100,101,99,100.5,1000\n",
            encoding="utf-8",
        )
        first = load_daily_csv(path, symbol="SPY", source="test")
        second = load_daily_csv(path, symbol="SPY", source="test")
        assert first.sha256 == second.sha256
        assert first.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()

    def test_out_of_order_rows_are_sorted(self, tmp_path: Path) -> None:
        path = tmp_path / "spy.csv"
        path.write_text(
            "date,open,high,low,close,volume\n"
            "2024-06-05,102,103,101,102.5,1200\n"
            "2024-06-03,100,101,99,100.5,1000\n"
            "2024-06-04,100.5,102,100,101.5,1100\n",
            encoding="utf-8",
        )
        loaded = load_daily_csv(path, symbol="SPY", source="test")
        sessions = [bar.session_date for bar in loaded.series]
        assert sessions == [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5)]
