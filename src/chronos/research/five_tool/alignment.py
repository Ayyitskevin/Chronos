"""Causal cross-series alignment for Five-Tool research traces."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from chronos.marketdata.bars import Bar, BarInterval, BarSeries, BarStatus
from chronos.research.five_tool.indicators import pine_ema
from chronos.research.five_tool.models import (
    AccountSnapshot,
    CompanionValue,
    FiveToolBarInput,
    FiveToolInputError,
    FiveToolSettings,
    pine_timeframe_seconds,
)

_INTERVAL_SECONDS = {
    BarInterval.MIN_1: 60,
    BarInterval.MIN_5: 5 * 60,
    BarInterval.HOUR_1: 60 * 60,
    BarInterval.DAY_1: 24 * 60 * 60,
}


def interval_seconds(interval: BarInterval) -> int:
    return _INTERVAL_SECONDS[interval]


def source_bar_id(bar: Bar) -> str:
    """Full venue/feed identity qualified by the exact source timestamp."""

    identity = (
        f"{bar.source}:{bar.exchange}:{bar.symbol}:{bar.interval}:{bar.session_date.isoformat()}"
    )
    return f"{identity}:{bar.timestamp_utc.isoformat()}"


def _series_feed_identity(series: BarSeries, label: str) -> tuple[str, str] | None:
    """Return one stable ``(source, exchange)`` identity or fail on drift."""

    if not series.bars:
        return None
    expected = (series.bars[0].source, series.bars[0].exchange)
    if any((bar.source, bar.exchange) != expected for bar in series.bars[1:]):
        raise FiveToolInputError(f"{label} source/exchange identity changed within the series")
    return expected


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """A Pine-style local exchange session evaluated at a supplied bar-open time.

    Chronos bars expose only close time, so the alignment helper derives open time
    by subtracting the fixed interval.  This is exact for regular intraday bars and
    a documented approximation for daily bars and exchange-calendar discontinuities.
    """

    start: time
    end: time
    weekdays: frozenset[int]
    timezone: str

    @classmethod
    def parse(cls, expression: str, timezone: str) -> SessionWindow:
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise FiveToolInputError(f"unknown exchange timezone: {timezone}") from exc
        clock_part, separator, day_part = expression.partition(":")
        start_text, dash, end_text = clock_part.partition("-")
        if not dash or len(start_text) != 4 or len(end_text) != 4:
            raise FiveToolInputError(f"invalid Pine session: {expression!r}")

        def parse_clock(value: str) -> time:
            if not value.isdigit():
                raise FiveToolInputError(f"invalid Pine session clock: {value!r}")
            hour = int(value[:2])
            minute = int(value[2:])
            if hour > 23 or minute > 59:
                raise FiveToolInputError(f"invalid Pine session clock: {value!r}")
            return time(hour=hour, minute=minute)

        # Pine day digits are Sunday=1 through Saturday=7.  Python Monday=0.
        pine_days = day_part if separator else "1234567"
        if not pine_days or any(character not in "1234567" for character in pine_days):
            raise FiveToolInputError(f"invalid Pine session weekdays: {day_part!r}")
        weekdays = frozenset((int(character) + 5) % 7 for character in pine_days)
        return cls(
            start=parse_clock(start_text),
            end=parse_clock(end_text),
            weekdays=weekdays,
            timezone=timezone,
        )

    def contains_open(self, timestamp_utc: datetime) -> bool:
        if timestamp_utc.tzinfo is None or timestamp_utc.utcoffset() is None:
            raise FiveToolInputError("session timestamp must be timezone-aware")
        local = timestamp_utc.astimezone(ZoneInfo(self.timezone))
        local_time = local.timetz().replace(tzinfo=None)
        if self.start == self.end:
            session_weekday = (
                local.weekday() if local_time >= self.start else (local.weekday() - 1) % 7
            )
            return session_weekday in self.weekdays
        if self.start < self.end:
            return local.weekday() in self.weekdays and self.start <= local_time < self.end
        # Overnight session: after start belongs to current day; before end belongs
        # to the day whose session began yesterday.
        if local_time >= self.start:
            return local.weekday() in self.weekdays
        previous_weekday = (local.weekday() - 1) % 7
        return local_time < self.end and previous_weekday in self.weekdays

    def contains_close(self, timestamp_utc: datetime) -> bool:
        """Backward-compatible alias; the argument is interpreted as an instant."""

        return self.contains_open(timestamp_utc)


AccountProvider = Callable[[Bar, int], AccountSnapshot]


def align_five_tool_inputs(
    settings: FiveToolSettings,
    primary: BarSeries,
    benchmark: BarSeries,
    *,
    higher_timeframe: BarSeries | None = None,
    account_provider: AccountProvider | None = None,
    external_regime: Mapping[datetime, float | None] | None = None,
    external_strength: Mapping[datetime, float | None] | None = None,
) -> tuple[FiveToolBarInput, ...]:
    """Align companion data without backfill or future access.

    Benchmark values emulate ``gaps_off`` by carrying the latest source bar with
    ``timestamp <= primary``.  HTF values require ``source timestamp < primary``;
    equality is excluded so the just-closing HTF bar is never consumed early.
    """

    if benchmark.interval is not primary.interval:
        raise FiveToolInputError("benchmark must use the primary chart interval")
    configured_benchmark = settings.text("bench_sym")
    expected_benchmark_exchange, separator, expected_benchmark_symbol = (
        configured_benchmark.rpartition(":")
    )
    if not separator or not expected_benchmark_exchange or not expected_benchmark_symbol:
        raise FiveToolInputError("settings bench_sym must be an exchange-qualified ticker ID")
    if benchmark.symbol.upper() != expected_benchmark_symbol.upper():
        raise FiveToolInputError(
            "benchmark series does not match the settings bench_sym contract: "
            f"expected {expected_benchmark_symbol!r}, got {benchmark.symbol!r}"
        )
    primary_identity = _series_feed_identity(primary, "primary")
    benchmark_identity = _series_feed_identity(benchmark, "benchmark")
    if benchmark_identity is not None:
        benchmark_source, benchmark_exchange = benchmark_identity
        if benchmark_exchange.upper() != expected_benchmark_exchange.upper():
            raise FiveToolInputError(
                "benchmark exchange does not match settings bench_sym: "
                f"expected {expected_benchmark_exchange!r}, got {benchmark_exchange!r}"
            )
        if primary_identity is not None and benchmark_source != primary_identity[0]:
            raise FiveToolInputError(
                "benchmark feed source does not match the primary series source"
            )
    if any(bar.status is not BarStatus.CLOSED for bar in (*primary.bars, *benchmark.bars)):
        raise FiveToolInputError("alignment accepts closed bars only")
    requested_seconds = pine_timeframe_seconds(settings.text("htf_tf"))
    requested_htf_valid = requested_seconds > interval_seconds(primary.interval)
    if requested_htf_valid and requested_seconds not in set(_INTERVAL_SECONDS.values()):
        raise FiveToolInputError(
            "settings htf_tf cannot be represented by the supported BarInterval vocabulary"
        )
    if requested_htf_valid and higher_timeframe is None:
        raise FiveToolInputError(
            "a valid higher settings htf_tf requires an explicit higher_timeframe series"
        )
    if higher_timeframe is not None and higher_timeframe.symbol != primary.symbol:
        raise FiveToolInputError("HTF series must use the primary chart symbol")
    htf_identity = (
        _series_feed_identity(higher_timeframe, "HTF") if higher_timeframe is not None else None
    )
    if (
        primary_identity is not None
        and htf_identity is not None
        and htf_identity != primary_identity
    ):
        raise FiveToolInputError("HTF source/exchange identity must match the primary series")
    if (
        higher_timeframe is not None
        and requested_htf_valid
        and interval_seconds(higher_timeframe.interval) != requested_seconds
    ):
        raise FiveToolInputError("HTF series interval does not match settings htf_tf")
    htf_valid = (
        higher_timeframe is not None
        and interval_seconds(higher_timeframe.interval) > interval_seconds(primary.interval)
        and requested_htf_valid
    )
    if higher_timeframe is not None and any(
        bar.status is not BarStatus.CLOSED for bar in higher_timeframe
    ):
        raise FiveToolInputError("HTF alignment accepts closed bars only")

    htf_bars = higher_timeframe.bars if htf_valid and higher_timeframe is not None else ()
    htf_ema = pine_ema(tuple(bar.close for bar in htf_bars), settings.integer("htf_ema_len"))
    long_window = SessionWindow.parse(
        settings.text("long_plus_session"), settings.exchange_timezone
    )
    short_window = SessionWindow.parse(
        settings.text("short_plus_session"), settings.exchange_timezone
    )
    benchmark_index = -1
    htf_index = -1
    result: list[FiveToolBarInput] = []
    for index, bar in enumerate(primary):
        approximated_open = bar.timestamp_utc - timedelta(seconds=interval_seconds(bar.interval))
        while (
            benchmark_index + 1 < len(benchmark)
            and benchmark[benchmark_index + 1].timestamp_utc <= bar.timestamp_utc
        ):
            benchmark_index += 1
        while (
            htf_index + 1 < len(htf_bars)
            and htf_bars[htf_index + 1].timestamp_utc < bar.timestamp_utc
        ):
            htf_index += 1
        benchmark_value = None
        if benchmark_index >= 0:
            source = benchmark[benchmark_index]
            benchmark_value = CompanionValue(
                value=source.close,
                source_timestamp_utc=source.timestamp_utc,
                source_sequence_id=source_bar_id(source),
            )
        htf_close_value = None
        htf_ema_value = None
        if htf_index >= 0:
            source = htf_bars[htf_index]
            htf_close_value = CompanionValue(
                value=source.close,
                source_timestamp_utc=source.timestamp_utc,
                source_sequence_id=source_bar_id(source),
            )
            ema_value = htf_ema[htf_index]
            if ema_value is not None:
                htf_ema_value = CompanionValue(
                    value=ema_value,
                    source_timestamp_utc=source.timestamp_utc,
                    source_sequence_id=source_bar_id(source),
                )
        result.append(
            FiveToolBarInput(
                primary=bar,
                benchmark=benchmark_value,
                htf_close=htf_close_value,
                htf_ema=htf_ema_value,
                external_regime=(external_regime or {}).get(bar.timestamp_utc),
                external_strength=(external_strength or {}).get(bar.timestamp_utc),
                long_plus_in_session=long_window.contains_open(approximated_open),
                short_plus_in_session=short_window.contains_open(approximated_open),
                account=(account_provider or (lambda _bar, _index: AccountSnapshot()))(bar, index),
            )
        )
    return tuple(result)
