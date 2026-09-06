"""Deterministic synthetic six-symbol history store (Sprint 3, lane B1).

Writes the exact ``research/data/history/`` layout — ``bars/<SYM>.csv``,
``corporate_actions/<SYM>.json``, ``MANIFEST.json``, ``HOLDOUTS.json`` — for the
six-symbol campaign universe, from a seed and nothing else. **No network, no broker, no
market data**: every price here is generated arithmetic and is labelled as such in the
manifest's ``source``.

Why it exists: the owner capture writes a store, ``data verify`` reads a delivery, and until
real bars exist neither path can be exercised end to end. This produces a store-shaped
directory so lane A's ``data assemble`` has an input and Kevin can rehearse the runbook
before any capture runs.

Three properties are load-bearing.

**The schemas are the store's, not this module's.** Bars, the manifest and the action streams
are written through ``histdata.store.write_bars`` / ``write_actions`` and the holdout
declaration through ``histdata.holdout.write_holdouts``. Nothing here formats a manifest
field by hand, so the fixture cannot drift from the store it imitates — if the store's schema
changes, this generator's output changes with it or fails loudly.

**Sessions come from the certification calendar, not from a weekday rule.** A raw
weekday range emits bars on market holidays, which ``certify_export`` reports as
``UNEXPECTED_BAR`` — a fixture that cannot certify is not a rehearsal. ``SessionCalendar`` is
the same expectation the verifier measures against, so the two agree by construction.

**The split is exactly reconcilable.** On its ex-date the close is set to the prior close
divided by the ratio, so the observed close-to-close return equals
``(1 / ratio) - 1`` exactly — the value ``certification._split_implied_return`` computes.
That gives the verifier's material-move check a real discontinuity that resolves against a
declared action rather than an unexplained one. Dividends move the path too, but by far less
than the material threshold, so they change the prices without manufacturing findings.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path

from chronos.histdata.corporate_actions import ActionKind, CorporateAction
from chronos.histdata.holdout import HoldoutWindow, write_holdouts
from chronos.histdata.store import write_actions, write_bars
from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.research.data_intake import CAMPAIGN_SYMBOLS
from chronos.research.session_calendar import SessionCalendar

#: Synthetic prices are not market data and must never be mistaken for it. This lands in
#: every manifest entry's ``source`` field and in each action's ``source``.
SYNTHETIC_SOURCE = "synthetic"

DEFAULT_START = date(2016, 1, 4)
DEFAULT_END = date(2026, 6, 30)

#: The one declared split: 2-for-1 on QQQ. One is enough — the check being rehearsed is
#: "a material move resolves against a declared action", and a second split would test the
#: same predicate twice.
_SPLIT_SYMBOL = "QQQ"
_SPLIT_RATIO = 2.0

_DAILY_CLOSE_UTC = time(hour=21, minute=0)


#: Per-symbol starting level, daily drift and volatility, and the quarterly dividend paid
#: per share. GLD pays nothing, which is true of the real instrument and gives the fixture a
#: symbol whose action stream is legitimately empty.
@dataclass(frozen=True, slots=True)
class _Profile:
    base: float
    drift: float
    vol: float
    dividend: float


_PROFILES = {
    "QQQ": _Profile(base=100.0, drift=0.00035, vol=0.011, dividend=0.16),
    "SPY": _Profile(base=190.0, drift=0.00028, vol=0.009, dividend=1.05),
    "IWM": _Profile(base=105.0, drift=0.00020, vol=0.012, dividend=0.42),
    "DIA": _Profile(base=170.0, drift=0.00026, vol=0.009, dividend=0.55),
    "GLD": _Profile(base=102.0, drift=0.00018, vol=0.008, dividend=0.0),
    "TLT": _Profile(base=120.0, drift=0.00005, vol=0.007, dividend=0.25),
}


def _unit(seed: int, symbol: str, index: int) -> float:
    """A stable value in [0, 1) from (seed, symbol, index).

    A hash rather than ``random`` on purpose: the sequence must not depend on how many
    values were drawn before it, so adding a symbol or changing a date range cannot shift
    another symbol's path and silently invalidate a pinned digest.
    """

    material = f"{seed}:{symbol}:{index}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") / 2**64


def _sessions(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError(f"end {end} precedes start {start}")
    return list(SessionCalendar().sessions(start, end))


def _dividend_dates(sessions: list[date]) -> set[date]:
    """One ex-dividend session per calendar quarter — the first session of each."""

    seen: dict[tuple[int, int], date] = {}
    for day in sessions:
        seen.setdefault((day.year, (day.month - 1) // 3), day)
    return set(seen.values())


def _split_date(sessions: list[date]) -> date:
    """Mid-range, so the split has real price history either side of it."""

    return sessions[len(sessions) // 2]


def actions_for(symbol: str, sessions: list[date]) -> tuple[CorporateAction, ...]:
    """The declared action stream for one symbol, in native as-of-ex-date basis."""

    profile = _PROFILES[symbol]
    actions: list[CorporateAction] = []
    if profile.dividend > 0:
        for day in sorted(_dividend_dates(sessions)):
            actions.append(
                CorporateAction(
                    kind=ActionKind.CASH_DIVIDEND,
                    ex_date=day,
                    value=profile.dividend,
                    source=SYNTHETIC_SOURCE,
                    note="synthetic quarterly cash dividend",
                )
            )
    if symbol == _SPLIT_SYMBOL:
        actions.append(
            CorporateAction(
                kind=ActionKind.SPLIT,
                ex_date=_split_date(sessions),
                value=_SPLIT_RATIO,
                source=SYNTHETIC_SOURCE,
                note="synthetic 2-for-1 split",
            )
        )
    return tuple(sorted(actions, key=lambda a: (a.ex_date, a.kind.value, a.value)))


def bars_for(symbol: str, sessions: list[date], seed: int) -> BarSeries:
    """One unadjusted as-traded daily series, with the actions reflected in the path."""

    profile = _PROFILES[symbol]
    dividends = _dividend_dates(sessions) if profile.dividend > 0 else set()
    split_on = _split_date(sessions) if symbol == _SPLIT_SYMBOL else None

    bars: list[Bar] = []
    close = profile.base
    for index, day in enumerate(sessions):
        previous_close = close
        if split_on is not None and day == split_on:
            # Exact, so the observed return equals (1 / ratio) - 1 and reconciles.
            close = round(previous_close / _SPLIT_RATIO, 4)
            open_ = close
        else:
            shock = (_unit(seed, symbol, index) - 0.5) * 2.0 * profile.vol
            close = round(previous_close * (1.0 + profile.drift + shock), 4)
            if day in dividends:
                close = round(max(close - profile.dividend, 0.01), 4)
            open_ = round(
                previous_close * (1.0 + (_unit(seed, symbol, index + 10**6) - 0.5) * profile.vol), 4
            )
        span = abs(close - open_) + round(profile.vol * close * 0.5, 4)
        high = round(max(open_, close) + span * _unit(seed, symbol, index + 2 * 10**6), 4)
        low = round(min(open_, close) - span * _unit(seed, symbol, index + 3 * 10**6), 4)
        low = round(max(low, 0.01), 4)
        volume = float(int(1_000_000 + _unit(seed, symbol, index + 4 * 10**6) * 9_000_000))
        bars.append(
            Bar(
                symbol=symbol,
                source=SYNTHETIC_SOURCE,
                exchange="SMART",
                interval=BarInterval.DAY_1,
                session_date=day,
                timestamp_utc=datetime.combine(day, _DAILY_CLOSE_UTC, tzinfo=UTC),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
    return BarSeries(symbol=symbol, interval=BarInterval.DAY_1, bars=tuple(bars))


def holdout_windows(sessions: list[date]) -> tuple[HoldoutWindow, ...]:
    """One declared, default-embargoed clean window over the tail of the range.

    Scoped to every campaign symbol explicitly rather than left symbol-less, so a reader —
    and lane A's holdout_map derivation — can see which symbols it covers without inferring
    that an empty list means "all".
    """

    tail_start = sessions[int(len(sessions) * 0.9)]
    return (
        HoldoutWindow(
            name="synthetic-clean-tail",
            start=tail_start,
            end=sessions[-1],
            symbols=tuple(CAMPAIGN_SYMBOLS),
            reason="synthetic fixture: untouched tail reserved as the clean holdout",
        ),
    )


def generate_store(
    out: Path,
    *,
    seed: int,
    start: date = DEFAULT_START,
    end: date = DEFAULT_END,
) -> dict[str, int]:
    """Write the whole store under ``out`` and return per-symbol bar counts.

    ``captured_at`` is derived from the requested range rather than from the clock, because
    it lands in ``MANIFEST.json`` and a wall-clock value would make the store's bytes differ
    on every run — which is the one property a fixture cannot afford.
    """

    sessions = _sessions(start, end)
    if not sessions:
        raise ValueError(f"no sessions between {start} and {end}")
    captured_at = datetime.combine(end, _DAILY_CLOSE_UTC, tzinfo=UTC).isoformat()

    written: dict[str, int] = {}
    for symbol in CAMPAIGN_SYMBOLS:
        series = bars_for(symbol, sessions, seed)
        result = write_bars(
            out, series, source=SYNTHETIC_SOURCE, exchange="SMART", captured_at=captured_at
        )
        write_actions(out, symbol, actions_for(symbol, sessions), captured_at=captured_at)
        written[symbol] = result.rows_written
    write_holdouts(out, holdout_windows(sessions))
    return written


__all__ = [
    "CAMPAIGN_SYMBOLS",
    "DEFAULT_END",
    "DEFAULT_START",
    "SYNTHETIC_SOURCE",
    "actions_for",
    "bars_for",
    "generate_store",
    "holdout_windows",
]
