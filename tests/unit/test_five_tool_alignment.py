from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.research.five_tool.alignment import SessionWindow, align_five_tool_inputs
from chronos.research.five_tool.models import FiveToolInputError, FiveToolSettings


def _bars(
    symbol: str,
    timestamps: tuple[datetime, ...],
    interval: BarInterval,
    *,
    source: str = "internal_spec",
    exchange: str | None = None,
) -> BarSeries:
    resolved_exchange = exchange or ("AMEX" if symbol == "SPY" else "NYSE")
    return BarSeries(
        symbol=symbol,
        interval=interval,
        bars=tuple(
            Bar(
                symbol=symbol,
                source=source,
                exchange=resolved_exchange,
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


def _settings(start: datetime) -> FiveToolSettings:
    return FiveToolSettings.defaults(history_start_utc=start)


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
        _settings(hourly_times[0]),
        hourly,
        hourly_benchmark,
        higher_timeframe=htf,
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
        align_five_tool_inputs(_settings(timestamp), primary, benchmark)


def test_session_parser_rejects_unknown_timezone_and_bad_clock() -> None:
    with pytest.raises(FiveToolInputError, match="timezone"):
        SessionWindow.parse("0935-1530", "Mars/Olympus")
    with pytest.raises(FiveToolInputError, match="clock"):
        SessionWindow.parse("2560-1530", "UTC")


def test_midnight_to_midnight_session_is_open_for_the_full_selected_day() -> None:
    session = SessionWindow.parse("0000-0000:2", "UTC")
    assert session.contains_open(datetime(2024, 1, 1, 0, 0, tzinfo=UTC))
    assert session.contains_open(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    assert session.contains_open(datetime(2024, 1, 1, 23, 59, tzinfo=UTC))
    assert not session.contains_open(datetime(2024, 1, 2, 12, 0, tzinfo=UTC))


def test_alignment_uses_intraday_bar_open_for_session_boundary() -> None:
    closes = (
        datetime(2024, 1, 2, 14, 35, tzinfo=UTC),
        datetime(2024, 1, 2, 14, 40, tzinfo=UTC),
    )
    primary = _bars("AAA", closes, BarInterval.MIN_5)
    benchmark = _bars("SPY", closes, BarInterval.MIN_5)
    settings = FiveToolSettings.defaults(
        history_start_utc=closes[0],
        overrides={"htf_tf": "5"},
    )
    aligned = align_five_tool_inputs(settings, primary, benchmark)
    # 09:30-09:35 opens before the 09:35 Pine session; the next bar opens at it.
    assert aligned[0].long_plus_in_session is False
    assert aligned[1].long_plus_in_session is True


def test_alignment_requires_representable_higher_timeframe_evidence() -> None:
    intraday_start = datetime(2024, 1, 2, 15, tzinfo=UTC)
    intraday = _bars("AAA", (intraday_start,), BarInterval.MIN_5)
    intraday_benchmark = _bars("SPY", (intraday_start,), BarInterval.MIN_5)
    with pytest.raises(FiveToolInputError, match="requires an explicit"):
        align_five_tool_inputs(_settings(intraday_start), intraday, intraday_benchmark)

    daily_start = datetime(2024, 1, 2, 21, tzinfo=UTC)
    daily = _bars("AAA", (daily_start,), BarInterval.DAY_1)
    daily_benchmark = _bars("SPY", (daily_start,), BarInterval.DAY_1)
    multi_day = FiveToolSettings.defaults(
        history_start_utc=daily_start,
        overrides={"htf_tf": "2D"},
    )
    with pytest.raises(FiveToolInputError, match="cannot be represented"):
        align_five_tool_inputs(multi_day, daily, daily_benchmark)


def test_alignment_rejects_benchmark_and_htf_identity_drift() -> None:
    start = datetime(2024, 1, 2, 15, tzinfo=UTC)
    primary = _bars("AAA", (start,), BarInterval.MIN_5)
    wrong_benchmark = _bars("QQQ", (start,), BarInterval.MIN_5)
    with pytest.raises(FiveToolInputError, match="bench_sym"):
        align_five_tool_inputs(_settings(start), primary, wrong_benchmark)

    benchmark = _bars("SPY", (start,), BarInterval.MIN_5)
    wrong_benchmark_exchange = _bars("SPY", (start,), BarInterval.MIN_5, exchange="NASDAQ")
    with pytest.raises(FiveToolInputError, match="benchmark exchange"):
        align_five_tool_inputs(_settings(start), primary, wrong_benchmark_exchange)

    wrong_benchmark_source = _bars("SPY", (start,), BarInterval.MIN_5, source="other-feed")
    with pytest.raises(FiveToolInputError, match="benchmark feed source"):
        align_five_tool_inputs(_settings(start), primary, wrong_benchmark_source)

    wrong_htf = _bars("AAA", (start - timedelta(hours=1),), BarInterval.HOUR_1)
    with pytest.raises(FiveToolInputError, match="does not match settings htf_tf"):
        align_five_tool_inputs(_settings(start), primary, benchmark, higher_timeframe=wrong_htf)

    wrong_htf_identity = _bars(
        "AAA",
        (start - timedelta(days=1),),
        BarInterval.DAY_1,
        source="other-feed",
        exchange="NASDAQ",
    )
    with pytest.raises(FiveToolInputError, match="HTF source/exchange identity"):
        align_five_tool_inputs(
            _settings(start), primary, benchmark, higher_timeframe=wrong_htf_identity
        )


def test_alignment_rejects_feed_drift_within_a_companion_series() -> None:
    start = datetime(2024, 1, 2, 15, tzinfo=UTC)
    primary = _bars("AAA", (start, start + timedelta(minutes=5)), BarInterval.MIN_5)
    benchmark = _bars("SPY", (start, start + timedelta(minutes=5)), BarInterval.MIN_5)
    drifted = BarSeries(
        symbol=benchmark.symbol,
        interval=benchmark.interval,
        bars=(benchmark[0], replace(benchmark[1], source="other")),
    )
    with pytest.raises(FiveToolInputError, match="changed within the series"):
        align_five_tool_inputs(
            FiveToolSettings.defaults(
                history_start_utc=start,
                overrides={"htf_tf": "5"},
            ),
            primary,
            drifted,
        )
