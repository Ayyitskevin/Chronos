"""Deterministic NYSE/NASDAQ session calendar — **research evidence only**.

Why this exists, and the boundary it must never cross
-----------------------------------------------------
Chronos deliberately refuses to invent a holiday calendar for the *trading* plane.
R-26 records the reason exactly: the load-bearing token is IBKR's own ``CLOSED``,
"that is the venue telling you a normal-looking Friday is not a trading day, which
is precisely the fact a weekday-and-clock calendar can never derive." R-34 discloses
the same residual for session counters. Nothing here changes that, and this module is
structurally barred from the authority plane by
``tests/safety/test_session_calendar_isolation.py``.

What a research-plane calendar is for is a different question with a different answer.
Phase 3's first data-quality gate is "at least 99.5% expected-session coverage"
(``docs/VISION_COMPLETION_PLAN.md`` §8). *Expected* is not derivable from the delivered
bars — a vendor that silently drops 40 sessions produces a file that is 100% consistent
with itself. Certification needs an independent expectation to measure the export
against, and that is the only thing this module supplies.

The error direction matters and is deliberate. This calendar is a **classifier of
discrepancies, never an oracle that suppresses them**: a session with no bar is an
unexplained gap that blocks certification, and a bar on a non-session day is an
unexpected bar that also blocks it. So a wrong entry here fails **loudly** at
certification time rather than silently certifying a hole. That property is why a
pinned table is acceptable evidence infrastructure and would not be acceptable as a
trading gate.

Coverage is bounded and fail-closed
-----------------------------------
Recurring holidays are rule-derived and hold for any year. Ad-hoc closures (funerals,
9/11, Hurricane Sandy) are unknowable in advance, so they are a pinned table and the
calendar refuses any date outside the range that table is complete for. Extending the
horizon means editing ``_AD_HOC_CLOSURES`` and ``LAST_COVERED_DATE`` together, in one
reviewed change.

Half-days are pinned separately and more narrowly than sessions, because they only
matter for hourly-bar expectations and their historical pattern is less regular than
the closure pattern. ``expected_bar_count`` refuses an hourly request outside the
early-close coverage range rather than assuming a full session.

Verification basis (stated because a calendar is exactly the artifact whose
correctness is easy to assert and hard to see): the recurring rules and the ad-hoc
table below are pinned knowledge, checked in ``tests/unit/test_session_calendar.py``
against dated, individually named cases — not against a second implementation of the
same rules, which would only prove the rules agree with themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- coverage bounds

#: First date this calendar answers for. 1998 is the first year NYSE observed
#: Martin Luther King Jr. Day, which removes a pre-1998 rule branch that no Chronos
#: dataset needs — the earliest in-repo history starts 1999-11-01.
FIRST_COVERED_DATE = date(1998, 1, 1)

#: Last date this calendar answers for. Bounded because ad-hoc closures cannot be
#: derived; extend with ``_AD_HOC_CLOSURES`` in the same reviewed change.
LAST_COVERED_DATE = date(2026, 12, 31)

#: Early closes are pinned over a narrower, later window than sessions. Before 2000
#: the half-day pattern was less regular than the rules below describe, and asserting
#: it would be inventing evidence.
FIRST_EARLY_CLOSE_COVERED_DATE = date(2000, 1, 1)
LAST_EARLY_CLOSE_COVERED_DATE = LAST_COVERED_DATE

#: Regular session hours, US/Eastern wall clock.
_EASTERN = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
#: Every NYSE half-day in the covered range closes at 13:00 ET.
EARLY_CLOSE = time(13, 0)


class SessionCalendarError(RuntimeError):
    """A calendar question could not be answered under the declared coverage."""


class CalendarCoverageError(SessionCalendarError):
    """The requested date lies outside the range this calendar is pinned for."""


class SessionKind(StrEnum):
    CLOSED = "CLOSED"
    REGULAR = "REGULAR"
    EARLY_CLOSE = "EARLY_CLOSE"


@dataclass(frozen=True, slots=True)
class Session:
    """One expected trading session, in exchange-local wall clock."""

    session_date: date
    kind: SessionKind
    open_time: time
    close_time: time

    @property
    def is_early_close(self) -> bool:
        return self.kind is SessionKind.EARLY_CLOSE


# ---------------------------------------------------------------- pinned closures

#: Unscheduled full-day closures. Each entry is a fact about a specific date that no
#: rule can produce; the reason is carried so a certification report can name it.
_AD_HOC_CLOSURES: dict[date, str] = {
    date(2001, 9, 11): "September 11 attacks — market closed",
    date(2001, 9, 12): "September 11 attacks — market closed",
    date(2001, 9, 13): "September 11 attacks — market closed",
    date(2001, 9, 14): "September 11 attacks — market closed",
    date(2004, 6, 11): "National day of mourning — President Ronald Reagan",
    date(2007, 1, 2): "National day of mourning — President Gerald Ford",
    date(2012, 10, 29): "Hurricane Sandy",
    date(2012, 10, 30): "Hurricane Sandy",
    date(2018, 12, 5): "National day of mourning — President George H. W. Bush",
    date(2025, 1, 9): "National day of mourning — President Jimmy Carter",
}

#: Half-days that the recurring rules below do not produce. Empty today: every
#: 13:00 close in the covered range is rule-derived. Kept as the declared seam so a
#: future exception is a data edit rather than a rule rewrite.
_AD_HOC_EARLY_CLOSES: dict[date, str] = {}


# ---------------------------------------------------------------- recurring rules


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (Mon=0) of a month."""

    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last ``weekday`` (Mon=0) of a month."""

    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = following - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def easter_sunday(year: int) -> date:
    """Gregorian Easter (Meeus/Jones/Butcher). Good Friday is two days earlier."""

    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    label = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * label) // 451
    month, day_offset = divmod(h + label - 7 * m + 114, 31)
    return date(year, month, day_offset + 1)


def _observed(day: date) -> date | None:
    """Apply NYSE Rule 7.2 weekend observance.

    Saturday holidays move to the preceding Friday, Sunday holidays to the following
    Monday — **except** a Saturday holiday whose Friday is the last business day of
    the year, which the exchange does not observe at all. That exception is why a
    Saturday January 1 leaves December 31 a full trading day.
    """

    weekday = day.weekday()
    if weekday == 5:  # Saturday
        preceding = day - timedelta(days=1)
        if preceding.month == 12 and preceding.day == 31:
            return None
        return preceding
    if weekday == 6:  # Sunday
        return day + timedelta(days=1)
    return day


def _recurring_holidays(year: int) -> dict[date, str]:
    """Every scheduled full-day closure observed in ``year``.

    Keyed by the *observed* date, so a holiday shifted out of the year (a Sunday
    December 31 boundary case) lands in the year it is actually observed.
    """

    candidates: list[tuple[date, str]] = [
        (date(year, 1, 1), "New Year's Day"),
        (_nth_weekday(year, 1, 0, 3), "Martin Luther King Jr. Day"),
        (_nth_weekday(year, 2, 0, 3), "Washington's Birthday"),
        (easter_sunday(year) - timedelta(days=2), "Good Friday"),
        (_last_weekday(year, 5, 0), "Memorial Day"),
        (date(year, 7, 4), "Independence Day"),
        (_nth_weekday(year, 9, 0, 1), "Labor Day"),
        (_nth_weekday(year, 11, 3, 4), "Thanksgiving Day"),
        (date(year, 12, 25), "Christmas Day"),
    ]
    if year >= 2022:
        # First observed 2022-06-20; June 19, 2022 fell on a Sunday.
        candidates.append((date(year, 6, 19), "Juneteenth National Independence Day"))

    holidays: dict[date, str] = {}
    for day, name in candidates:
        observed = _observed(day)
        if observed is not None:
            holidays[observed] = name
    return holidays


def _is_rule_early_close(day: date) -> bool:
    """The three recurring 13:00 closes, expressed as conditions on ``day`` itself.

    Each is written as "this date is the half-day" rather than "the holiday is
    tomorrow", so the weekend cases fall out instead of needing exceptions: a
    July 3 that is a Sunday is never reached, and a December 24 that is a Friday is
    already the observed Christmas closure.
    """

    if day.weekday() >= 5:
        return False
    # Friday after Thanksgiving.
    thanksgiving = _nth_weekday(day.year, 11, 3, 4)
    if day == thanksgiving + timedelta(days=1):
        return True
    # Christmas Eve, when it falls Monday-Thursday.
    if day.month == 12 and day.day == 24 and day.weekday() <= 3:
        return True
    # July 3, when Independence Day itself is a weekday.
    return day.month == 7 and day.day == 3 and date(day.year, 7, 4).weekday() <= 4


# ---------------------------------------------------------------- public calendar


class SessionCalendar:
    """Expected NYSE/NASDAQ equity sessions over a bounded, pinned date range."""

    def __init__(
        self,
        *,
        first_covered: date = FIRST_COVERED_DATE,
        last_covered: date = LAST_COVERED_DATE,
    ) -> None:
        if first_covered > last_covered:
            raise ValueError("first_covered must not follow last_covered")
        self._first_covered = first_covered
        self._last_covered = last_covered
        self._holidays: dict[int, dict[date, str]] = {}

    @property
    def first_covered(self) -> date:
        return self._first_covered

    @property
    def last_covered(self) -> date:
        return self._last_covered

    def _require_covered(self, day: date) -> None:
        if not (self._first_covered <= day <= self._last_covered):
            raise CalendarCoverageError(
                f"{day.isoformat()} is outside the pinned calendar range "
                f"{self._first_covered.isoformat()}..{self._last_covered.isoformat()}; "
                "extend _AD_HOC_CLOSURES and LAST_COVERED_DATE together"
            )

    def _year_holidays(self, year: int) -> dict[date, str]:
        cached = self._holidays.get(year)
        if cached is None:
            cached = _recurring_holidays(year)
            self._holidays[year] = cached
        return cached

    def closure_reason(self, day: date) -> str | None:
        """Why ``day`` is not a session, or ``None`` if it is one."""

        self._require_covered(day)
        if day.weekday() >= 5:
            return "Weekend"
        ad_hoc = _AD_HOC_CLOSURES.get(day)
        if ad_hoc is not None:
            return ad_hoc
        # Weekend observance shifts by at most one day and no candidate sits on a
        # year boundary, so an observed date always lands in its own rule year.
        return self._year_holidays(day.year).get(day)

    def is_session(self, day: date) -> bool:
        return self.closure_reason(day) is None

    def session(self, day: date) -> Session:
        """The expected session for ``day``, or ``CLOSED`` with zero-length hours."""

        reason = self.closure_reason(day)
        if reason is not None:
            return Session(
                session_date=day,
                kind=SessionKind.CLOSED,
                open_time=REGULAR_OPEN,
                close_time=REGULAR_OPEN,
            )
        if self._is_early_close(day):
            return Session(
                session_date=day,
                kind=SessionKind.EARLY_CLOSE,
                open_time=REGULAR_OPEN,
                close_time=EARLY_CLOSE,
            )
        return Session(
            session_date=day,
            kind=SessionKind.REGULAR,
            open_time=REGULAR_OPEN,
            close_time=REGULAR_CLOSE,
        )

    def _is_early_close(self, day: date) -> bool:
        if day in _AD_HOC_EARLY_CLOSES:
            return True
        return _is_rule_early_close(day)

    def is_early_close(self, day: date) -> bool:
        """Whether ``day`` is a pinned half-day. Refuses outside early-close coverage."""

        self._require_covered(day)
        if not (FIRST_EARLY_CLOSE_COVERED_DATE <= day <= LAST_EARLY_CLOSE_COVERED_DATE):
            raise CalendarCoverageError(
                f"{day.isoformat()} is outside the pinned early-close range "
                f"{FIRST_EARLY_CLOSE_COVERED_DATE.isoformat()}.."
                f"{LAST_EARLY_CLOSE_COVERED_DATE.isoformat()}; half-day knowledge is "
                "pinned more narrowly than session knowledge and is not assumed"
            )
        if not self.is_session(day):
            return False
        return self._is_early_close(day)

    def sessions(self, start: date, end: date) -> tuple[date, ...]:
        """Every expected session date in the inclusive range ``[start, end]``."""

        if start > end:
            raise ValueError("start must not follow end")
        self._require_covered(start)
        self._require_covered(end)
        found: list[date] = []
        day = start
        while day <= end:
            if self.closure_reason(day) is None:
                found.append(day)
            day += timedelta(days=1)
        return tuple(found)

    def expected_session_count(self, start: date, end: date) -> int:
        return len(self.sessions(start, end))

    def expected_bar_count(self, day: date, *, bars_per_hour: int = 1) -> int:
        """Expected intraday bars for ``day`` at ``bars_per_hour`` resolution.

        Refuses rather than assuming a full session when half-day knowledge is not
        pinned for the date, because silently counting a 13:00 close as 16:00 turns
        three missing bars into an unexplained gap on every half-day in the export.
        """

        if bars_per_hour < 1:
            raise ValueError("bars_per_hour must be at least 1")
        self._require_covered(day)
        if not self.is_session(day):
            return 0
        # is_early_close carries the narrower coverage refusal.
        early = self.is_early_close(day)
        close = EARLY_CLOSE if early else REGULAR_CLOSE
        open_minutes = REGULAR_OPEN.hour * 60 + REGULAR_OPEN.minute
        close_minutes = close.hour * 60 + close.minute
        span_minutes = close_minutes - open_minutes
        minutes_per_bar = 60 // bars_per_hour
        # Partial trailing bars count: 09:30-16:00 is 6.5 hours, which is seven
        # hourly bars with a half-length final bar, and IBKR delivers exactly that.
        return -(-span_minutes // minutes_per_bar)

    def expected_close_timestamps_utc(
        self, day: date, *, bars_per_hour: int = 1
    ) -> tuple[datetime, ...]:
        """The exact UTC close timestamp of every expected intraday bar of ``day``.

        This is ``expected_bar_count`` sharpened from *how many* to *which*: bar-level
        certification needs to name the missing bar, not merely count a shortfall.
        Closes fall on whole bar boundaries after the open, and the final partial
        bar closes at the session close itself — 16:00, or 13:00 on a half-day —
        matching how the ingestion parser stamps IBKR's RTH bars. Same coverage
        refusals as ``expected_bar_count``; conversion runs through US/Eastern so
        both DST regimes produce the timestamps the data actually carries.
        """

        if bars_per_hour < 1:
            raise ValueError("bars_per_hour must be at least 1")
        self._require_covered(day)
        if not self.is_session(day):
            return ()
        early = self.is_early_close(day)  # carries the narrower coverage refusal
        close = EARLY_CLOSE if early else REGULAR_CLOSE
        open_dt = datetime.combine(day, REGULAR_OPEN, tzinfo=_EASTERN)
        close_dt = datetime.combine(day, close, tzinfo=_EASTERN)
        step = timedelta(minutes=60 // bars_per_hour)
        closes: list[datetime] = []
        cursor = open_dt + step
        while cursor < close_dt:
            closes.append(cursor.astimezone(UTC))
            cursor += step
        closes.append(close_dt.astimezone(UTC))
        return tuple(closes)


__all__ = [
    "EARLY_CLOSE",
    "FIRST_COVERED_DATE",
    "FIRST_EARLY_CLOSE_COVERED_DATE",
    "LAST_COVERED_DATE",
    "LAST_EARLY_CLOSE_COVERED_DATE",
    "REGULAR_CLOSE",
    "REGULAR_OPEN",
    "CalendarCoverageError",
    "Session",
    "SessionCalendar",
    "SessionCalendarError",
    "SessionKind",
    "easter_sunday",
]
