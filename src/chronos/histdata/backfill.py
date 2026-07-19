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
from datetime import date, datetime
from pathlib import Path

from chronos.histdata.client import HistoricalDataClient
from chronos.histdata.pacing import PacingController
from chronos.histdata.store import WriteResult, write_bars


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
