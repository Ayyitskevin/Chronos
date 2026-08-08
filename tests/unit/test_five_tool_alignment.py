from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.research.five_tool.alignment import SessionWindow, align_five_tool_inputs
from chronos.research.five_tool.models import FiveToolInputError


def _bars(
    symbol: str,
    timestamps: tuple[datetime, ...],
    interval: BarInterval,
) -> BarSeries:
    return BarSeries(
        symbol=symbol,
        interval=interval,
        bars=tuple(
            Bar(
                symbol=symbol,
                source="internal_spec",
                exchange="NYSE",
                interval=interval,
                session_date=timestamp.date(),
                timestamp_utc=timestamp,
                open=100.0 + index,
                high=101.0 + index,
                low=99.0 + index,
                close=100.5 + index,
                volume=1_000.0,
            )
            for index, timestamp in enumerate(timestamps)
        ),
    )


def test_higher_timeframe_excludes_equal_close_and_uses_prior_completed_value() -> None:
    start = datetime(2024, 1, 1, 21, tzinfo=UTC)
    primary_times = tuple(start + timedelta(days=index) for index in range(4))
    primary = _bars("AAA", primary_times, BarInterval.DAY_1)
    benchmark = _bars("SPY", primary_times, BarInterval.DAY_1)
    # HOUR_1 cannot be higher than DAY_1, so use a synthetic DAY primary's next
    # supported interval limitation explicitly below.  For the causal equality
    # test, primary is hourly and HTF is daily.
    hourly_times = (
        datetime(2024, 1, 2, 20, tzinfo=UTC),
        datetime(2024, 1, 2, 21, tzinfo=UTC),
        datetime(2024, 1, 3, 15, tzinfo=UTC),
    )
    hourly = _bars("AAA", hourly_times, BarInterval.HOUR_1)
    hourly_benchmark = _bars("SPY", hourly_times, BarInterval.HOUR_1)
    htf = _bars(
        "AAA",
        (
            datetime(2024, 1, 1, 21, tzinfo=UTC),
            datetime(2024, 1, 2, 21, tzinfo=UTC),
        ),
        BarInterval.DAY_1,
    )
    aligned = align_five_tool_inputs(
        hourly, hourly_benchmark, higher_timeframe=htf, htf_ema_length=1
    )
    assert aligned[0].htf_close is not None
    assert aligned[0].htf_close.source_timestamp_utc == htf[0].timestamp_utc
    # At exact equality, the 2024-01-02 daily close remains ineligible.
    assert aligned[1].htf_close is not None
    assert aligned[1].htf_close.source_timestamp_utc == htf[0].timestamp_utc
    assert aligned[2].htf_close is not None
    assert aligned[2].htf_close.source_timestamp_utc == htf[1].timestamp_utc
    assert primary.symbol == benchmark.symbol.replace("SPY", "AAA")


def test_session_window_handles_new_york_dst_in_utc() -> None:
    session = SessionWindow.parse("0935-1530:23456", "America/New_York")
    # 10:00 New York occurs at 15:00 UTC before spring DST and 14:00 UTC after it.
    assert session.contains_close(datetime(2024, 3, 8, 15, tzinfo=UTC))
    assert session.contains_close(datetime(2024, 3, 11, 14, tzinfo=UTC))
    assert not session.contains_close(datetime(2024, 3, 11, 13, tzinfo=UTC))


def test_benchmark_interval_mismatch_fails_closed() -> None:
    timestamp = datetime(2024, 1, 1, 21, tzinfo=UTC)
    primary = _bars("AAA", (timestamp,), BarInterval.DAY_1)
    benchmark = _bars("SPY", (timestamp,), BarInterval.HOUR_1)
    with pytest.raises(FiveToolInputError, match="chart interval"):
        align_five_tool_inputs(primary, benchmark)


def test_session_parser_rejects_unknown_timezone_and_bad_clock() -> None:
    with pytest.raises(FiveToolInputError, match="timezone"):
        SessionWindow.parse("0935-1530", "Mars/Olympus")
    with pytest.raises(FiveToolInputError, match="clock"):
        SessionWindow.parse("2560-1530", "UTC")
