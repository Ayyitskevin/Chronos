"""Backfill coordinator for the historical-data plane (ADR-0011 §3/§5).

Ties the fetch client, the pacing controller, and the file store together: for
each symbol it waits out any pacing delay, fetches unadjusted bars, and writes them
idempotently (quality-gated, fail-closed). Timing dependencies — the clock and the
sleep — are injected, so the coordinator is deterministic and testable without real
waiting; the process supplies the real ones.

It imports only the data-plane modules and ``marketdata``; no order/broker/
persistence code, and it never opens a database.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from chronos.histdata.client import HistoricalDataClient
from chronos.histdata.store import WriteResult, write_bars, write_hourly_bars
from chronos.marketdata.pacing import PacingController


@dataclass(frozen=True, slots=True)
class SymbolOutcome:
    symbol: str
    result: WriteResult | None
    error: str | None


def backfill_symbol(
    client: HistoricalDataClient,
    root: Path,
    symbol: str,
    *,
    end_date: date,
    duration_days: int,
    pacing: PacingController,
    now_fn: Callable[[], datetime],
    captured_at: str,
    sleep: Callable[[float], None] = time.sleep,
    source: str = "ibkr",
    exchange: str = "SMART",
    allow_correction: bool = False,
) -> WriteResult:
    """Pace, fetch, and idempotently store one symbol's unadjusted daily bars."""

    delay = pacing.delay_before(symbol, now_fn())
    if delay > 0:
        sleep(delay)
    pacing.record(symbol, now_fn())
    series = client.fetch_daily_bars(symbol, end_date=end_date, duration_days=duration_days)
    return write_bars(
        root,
        series,
        source=source,
        exchange=exchange,
        captured_at=captured_at,
        allow_correction=allow_correction,
    )


def backfill_symbols(
    client: HistoricalDataClient,
    root: Path,
    symbols: Iterable[str],
    *,
    end_date: date,
    duration_days: int,
    pacing: PacingController,
    now_fn: Callable[[], datetime],
    captured_at: str,
    sleep: Callable[[float], None] = time.sleep,
    source: str = "ibkr",
    exchange: str = "SMART",
) -> tuple[SymbolOutcome, ...]:
    """Backfill each symbol; a failure isolates to that symbol's outcome."""

    outcomes: list[SymbolOutcome] = []
    for symbol in symbols:
        try:
            result = backfill_symbol(
                client,
                root,
                symbol,
                end_date=end_date,
                duration_days=duration_days,
                pacing=pacing,
                now_fn=now_fn,
                captured_at=captured_at,
                sleep=sleep,
                source=source,
                exchange=exchange,
            )
            outcomes.append(SymbolOutcome(symbol, result, None))
        except Exception as error:
            outcomes.append(SymbolOutcome(symbol, None, f"{type(error).__name__}: {error}"))
    return tuple(outcomes)


#: One hourly request's span, chosen under every published reading of IBKR's
#: per-bar-size duration caps (the strictest is one month for 1-hour bars; the
#: repo records no verified table — ADR-0029 makes pinning it a first-run item).
#: Raising it after owner verification is a CLI knob, not a code change.
DEFAULT_HOURLY_CHUNK_DAYS = 30


def backfill_hourly_symbol(
    client: HistoricalDataClient,
    root: Path,
    symbol: str,
    *,
    end_date: date,
    duration_days: int,
    pacing: PacingController,
    now_fn: Callable[[], datetime],
    captured_at: str,
    sleep: Callable[[float], None] = time.sleep,
    source: str = "ibkr",
    exchange: str = "SMART",
    allow_correction: bool = False,
    chunk_days: int = DEFAULT_HOURLY_CHUNK_DAYS,
) -> WriteResult:
    """Pace, fetch, and store one symbol's hourly bars in cap-sized chunks.

    Chunking lives HERE, not in the client, so every gateway request passes the
    pacing controller — chunks issued inside a client would reach the gateway as
    an unpaced burst. Each chunk gets its own pacing key (symbol, bar size, and
    chunk end): distinct chunks are distinct requests, so the per-key cooldown —
    which exists for identical-request repetition — must not serialize them; the
    shared window budget is what bounds the true request rate. Chunks run oldest
    to newest so an interrupted backfill leaves a clean, resumable prefix.

    A chunk that returns zero rows is recorded and skipped, not an error: IBKR's
    intraday history horizon is undocumented in this repo, and chunks before a
    symbol's available depth (or its listing) legitimately come back empty. The
    certifier judges the resulting window; the backfill does not guess at it.
    """

    if chunk_days < 1:
        raise ValueError("chunk_days must be at least 1")
    if duration_days < 1:
        raise ValueError("duration_days must be at least 1")

    # Chunk boundaries, oldest first: each chunk covers (end - chunk_days, end].
    ends: list[tuple[date, int]] = []
    remaining = duration_days
    chunk_end = end_date
    while remaining > 0:
        span = min(chunk_days, remaining)
        ends.append((chunk_end, span))
        chunk_end = chunk_end - timedelta(days=span)
        remaining -= span
    ends.reverse()

    total_rows = 0
    total_added = 0
    corrections: list[str] = []
    for chunk_end, span in ends:
        key = f"{symbol}:1h:{chunk_end.isoformat()}"
        delay = pacing.delay_before(key, now_fn())
        if delay > 0:
            sleep(delay)
        pacing.record(key, now_fn())
        series = client.fetch_hourly_bars(symbol, end_date=chunk_end, duration_days=span)
        if len(series) == 0:
            continue
        result = write_hourly_bars(
            root,
            series,
            source=source,
            exchange=exchange,
            captured_at=captured_at,
            allow_correction=allow_correction,
        )
        total_rows = result.rows_written
        total_added += result.rows_added
        corrections.extend(result.corrections)
    return WriteResult(symbol, total_rows, total_added, tuple(corrections))


def backfill_hourly_symbols(
    client: HistoricalDataClient,
    root: Path,
    symbols: Iterable[str],
    *,
    end_date: date,
    duration_days: int,
    pacing: PacingController,
    now_fn: Callable[[], datetime],
    captured_at: str,
    sleep: Callable[[float], None] = time.sleep,
    source: str = "ibkr",
    exchange: str = "SMART",
    chunk_days: int = DEFAULT_HOURLY_CHUNK_DAYS,
) -> tuple[SymbolOutcome, ...]:
    """Hourly backfill for each symbol; a failure isolates to that symbol's outcome."""

    outcomes: list[SymbolOutcome] = []
    for symbol in symbols:
        try:
            result = backfill_hourly_symbol(
                client,
                root,
                symbol,
                end_date=end_date,
                duration_days=duration_days,
                pacing=pacing,
                now_fn=now_fn,
                captured_at=captured_at,
                sleep=sleep,
                source=source,
                exchange=exchange,
                chunk_days=chunk_days,
            )
            outcomes.append(SymbolOutcome(symbol, result, None))
        except Exception as error:
            outcomes.append(SymbolOutcome(symbol, None, f"{type(error).__name__}: {error}"))
    return tuple(outcomes)
