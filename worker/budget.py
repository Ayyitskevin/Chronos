"""A per-UTC-day token ceiling for the worker's model spend (R-47 residual (e)).

Until now cost was logged, never capped — cadence was the only throttle. This
module is the cap: an in-memory accumulator the loop consults before every
think and charges after every priced response. At the ceiling the cycle logs
``COST_CEILING`` and skips thinking until the UTC day rolls; nothing here can
make the worker think more, only less.

Deliberately in memory and per process: a restart forgets the day's spend.
That residual is disclosed in the risk register rather than solved with a
state file, because a worker that persists counters grows a write path this
process otherwise does not have.
"""

from __future__ import annotations

from datetime import UTC, date, datetime


def _today() -> date:
    return datetime.now(UTC).date()


class DailyTokenBudget:
    """Accumulates model tokens per UTC day against an optional ceiling.

    ``ceiling`` is the validated ``CHRONOS_WORKER_MAX_DAILY_TOKENS`` value;
    ``None`` means uncapped — spend is still tracked, ``exhausted`` is never
    true, and the unchanged posture is disclosed at startup.

    ``today`` parameters exist so tests can drive the day roll with real
    dates instead of monkeypatching the clock.
    """

    def __init__(self, ceiling: int | None) -> None:
        self._ceiling = ceiling
        self._day: date | None = None
        self._spent = 0

    @property
    def ceiling(self) -> int | None:
        return self._ceiling

    @property
    def spent_today(self) -> int:
        return self._spent

    def spend(self, tokens: int, *, today: date | None = None) -> None:
        """Charge ``tokens`` against today's total, rolling the day first."""

        self._roll(today if today is not None else _today())
        self._spent += tokens

    def exhausted(self, *, today: date | None = None) -> bool:
        """True when today's spend has met the ceiling. Never true uncapped."""

        self._roll(today if today is not None else _today())
        return self._ceiling is not None and self._spent >= self._ceiling

    def _roll(self, today: date) -> None:
        if today != self._day:
            self._day = today
            self._spent = 0
