"""Causal cross-series alignment for Five-Tool research traces."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from chronos.marketdata.bars import Bar, BarInterval, BarSeries, BarStatus
from chronos.research.five_tool.indicators import pine_ema
from chronos.research.five_tool.models import (
    AccountSnapshot,
    CompanionValue,
    FiveToolBarInput,
    FiveToolInputError,
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
    """Timestamp-qualified identity; ``Bar.sequence_id`` is only daily-safe today."""

    return f"{bar.sequence_id}:{bar.timestamp_utc.isoformat()}"


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """A Pine-style local exchange session evaluated at the bar close timestamp.

    Pine's session gate uses the bar opening time.  Chronos bars currently expose
    only close time, so callers must treat this helper as a documented close-time
    approximation or supply membership from a richer calendar feed.
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

    def contains_close(self, timestamp_utc: datetime) -> bool:
        if timestamp_utc.tzinfo is None or timestamp_utc.utcoffset() is None:
            raise FiveToolInputError("session timestamp must be timezone-aware")
        local = timestamp_utc.astimezone(ZoneInfo(self.timezone))
        local_time = local.timetz().replace(tzinfo=None)
        if self.start <= self.end:
            return local.weekday() in self.weekdays and self.start <= local_time < self.end
        # Overnight session: after start belongs to current day; before end belongs
        # to the day whose session began yesterday.
        if local_time >= self.start:
            return local.weekday() in self.weekdays
        previous_weekday = (local.weekday() - 1) % 7
        return local_time < self.end and previous_weekday in self.weekdays


AccountProvider = Callable[[Bar, int], AccountSnapshot]


def align_five_tool_inputs(
    primary: BarSeries,
    benchmark: BarSeries,
    *,
    higher_timeframe: BarSeries | None = None,
    htf_ema_length: int = 100,
    exchange_timezone: str = "America/New_York",
    long_session: str = "0935-1530",
    short_session: str = "0935-1530",
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
    if any(bar.status is not BarStatus.CLOSED for bar in (*primary.bars, *benchmark.bars)):
        raise FiveToolInputError("alignment accepts closed bars only")
    htf_valid = higher_timeframe is not None and interval_seconds(
        higher_timeframe.interval
    ) > interval_seconds(primary.interval)
    if higher_timeframe is not None and any(
        bar.status is not BarStatus.CLOSED for bar in higher_timeframe
    ):
        raise FiveToolInputError("HTF alignment accepts closed bars only")

    htf_bars = higher_timeframe.bars if htf_valid and higher_timeframe is not None else ()
    htf_ema = pine_ema(tuple(bar.close for bar in htf_bars), htf_ema_length)
    long_window = SessionWindow.parse(long_session, exchange_timezone)
    short_window = SessionWindow.parse(short_session, exchange_timezone)
    benchmark_index = -1
    htf_index = -1
    result: list[FiveToolBarInput] = []
    for index, bar in enumerate(primary):
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
                long_plus_in_session=long_window.contains_close(bar.timestamp_utc),
                short_plus_in_session=short_window.contains_close(bar.timestamp_utc),
                account=(account_provider or (lambda _bar, _index: AccountSnapshot()))(bar, index),
            )
        )
    return tuple(result)
