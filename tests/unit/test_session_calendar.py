"""The session calendar is checked against dated facts, not against itself.

A calendar tested by re-deriving its own rules proves only that the rules agree with
themselves. Every case below names a specific published NYSE date — the observance
boundaries the rules exist to get right, the ad-hoc closures no rule can produce, and
the coverage refusals that keep a wrong answer from being invented.
"""

from __future__ import annotations

from datetime import date, time

import pytest

from chronos.research.session_calendar import (
    EARLY_CLOSE,
    FIRST_COVERED_DATE,
    LAST_COVERED_DATE,
    REGULAR_CLOSE,
    CalendarCoverageError,
    SessionCalendar,
    SessionKind,
    easter_sunday,
)


@pytest.fixture
def calendar() -> SessionCalendar:
    return SessionCalendar()


# ------------------------------------------------------------------ the fixed rules


@pytest.mark.parametrize(
    ("day", "name"),
    [
        (date(2024, 1, 1), "New Year's Day"),
        (date(2024, 1, 15), "Martin Luther King Jr. Day"),
        (date(2024, 2, 19), "Washington's Birthday"),
        (date(2024, 3, 29), "Good Friday"),
        (date(2024, 5, 27), "Memorial Day"),
        (date(2024, 6, 19), "Juneteenth National Independence Day"),
        (date(2024, 7, 4), "Independence Day"),
        (date(2024, 9, 2), "Labor Day"),
        (date(2024, 11, 28), "Thanksgiving Day"),
        (date(2024, 12, 25), "Christmas Day"),
    ],
)
def test_every_2024_holiday_is_closed_and_named(
    calendar: SessionCalendar, day: date, name: str
) -> None:
    assert calendar.is_session(day) is False
    assert calendar.closure_reason(day) == name


def test_a_normal_wednesday_is_a_regular_session(calendar: SessionCalendar) -> None:
    session = calendar.session(date(2024, 5, 1))
    assert session.kind is SessionKind.REGULAR
    assert session.close_time == REGULAR_CLOSE
    assert calendar.closure_reason(date(2024, 5, 1)) is None


def test_weekends_are_closed_without_being_holidays(calendar: SessionCalendar) -> None:
    assert calendar.closure_reason(date(2024, 5, 4)) == "Weekend"
    assert calendar.closure_reason(date(2024, 5, 5)) == "Weekend"


# ------------------------------------------------------- weekend observance boundaries


def test_sunday_holiday_is_observed_the_following_monday(calendar: SessionCalendar) -> None:
    """January 1, 2023 fell on a Sunday; the exchange closed Monday January 2."""

    assert calendar.is_session(date(2023, 1, 2)) is False
    assert calendar.closure_reason(date(2023, 1, 2)) == "New Year's Day"


def test_saturday_holiday_is_observed_the_preceding_friday(calendar: SessionCalendar) -> None:
    """Christmas 2021 fell on a Saturday; the exchange closed Friday December 24."""

    assert calendar.is_session(date(2021, 12, 24)) is False
    assert calendar.closure_reason(date(2021, 12, 24)) == "Christmas Day"


def test_a_saturday_new_year_is_not_observed_at_all(calendar: SessionCalendar) -> None:
    """Rule 7.2's exception: the last business day of the year is never a holiday.

    January 1, 2022 fell on a Saturday. December 31, 2021 was a full trading day, and
    reading the generic Saturday rule literally would have deleted a real session from
    every 2021 coverage calculation.
    """

    assert calendar.is_session(date(2021, 12, 31)) is True
    assert calendar.closure_reason(date(2021, 12, 31)) is None
    # And no New Year closure is invented in 2022 either: Monday January 3 opened
    # normally, so the holiday simply does not exist that year.
    assert calendar.expected_session_count(date(2022, 1, 1), date(2022, 1, 3)) == 1
    assert calendar.is_session(date(2022, 1, 3)) is True


def test_juneteenth_does_not_exist_before_2022(calendar: SessionCalendar) -> None:
    assert calendar.is_session(date(2021, 6, 18)) is True
    assert calendar.closure_reason(date(2022, 6, 20)) == "Juneteenth National Independence Day"


def test_good_friday_tracks_easter(calendar: SessionCalendar) -> None:
    assert easter_sunday(2024) == date(2024, 3, 31)
    assert easter_sunday(2021) == date(2021, 4, 4)
    assert easter_sunday(2038) == date(2038, 4, 25)
    assert calendar.closure_reason(date(2021, 4, 2)) == "Good Friday"


# ------------------------------------------------------------------- ad-hoc closures


@pytest.mark.parametrize(
    ("day", "fragment"),
    [
        (date(2001, 9, 11), "September 11"),
        (date(2001, 9, 14), "September 11"),
        (date(2004, 6, 11), "Reagan"),
        (date(2007, 1, 2), "Ford"),
        (date(2012, 10, 29), "Hurricane Sandy"),
        (date(2012, 10, 30), "Hurricane Sandy"),
        (date(2018, 12, 5), "George H. W. Bush"),
        (date(2025, 1, 9), "Jimmy Carter"),
    ],
)
def test_unscheduled_closures_are_pinned_facts_with_reasons(
    calendar: SessionCalendar, day: date, fragment: str
) -> None:
    assert calendar.is_session(day) is False
    reason = calendar.closure_reason(day)
    assert reason is not None and fragment in reason


def test_september_2001_shortens_the_trading_year(calendar: SessionCalendar) -> None:
    """2001 had 248 sessions, not 252 — the four-day closure is the difference."""

    assert calendar.expected_session_count(date(2001, 1, 1), date(2001, 12, 31)) == 248
    assert calendar.expected_session_count(date(2024, 1, 1), date(2024, 12, 31)) == 252


def test_the_session_after_september_11_reopened(calendar: SessionCalendar) -> None:
    assert calendar.is_session(date(2001, 9, 17)) is True


# ----------------------------------------------------------------------- half-days


@pytest.mark.parametrize(
    "day",
    [
        date(2024, 7, 3),  # Independence Day fell on a Thursday
        date(2024, 11, 29),  # Friday after Thanksgiving
        date(2024, 12, 24),  # Christmas Eve on a Tuesday
        date(2019, 7, 3),
        date(2025, 12, 24),
    ],
)
def test_pinned_half_days_close_at_one(calendar: SessionCalendar, day: date) -> None:
    assert calendar.is_early_close(day) is True
    session = calendar.session(day)
    assert session.kind is SessionKind.EARLY_CLOSE
    assert session.close_time == EARLY_CLOSE
    assert session.is_early_close is True


@pytest.mark.parametrize(
    "day",
    [
        date(2023, 12, 22),  # Christmas Eve fell on a Sunday: no half-day at all
        date(2022, 7, 1),  # July 3 fell on a Sunday: no half-day
        date(2024, 5, 1),  # an ordinary session
    ],
)
def test_ordinary_sessions_are_not_half_days(calendar: SessionCalendar, day: date) -> None:
    assert calendar.is_early_close(day) is False
    assert calendar.session(day).close_time == REGULAR_CLOSE


def test_a_holiday_is_not_reported_as_a_half_day(calendar: SessionCalendar) -> None:
    """December 24, 2021 was the observed Christmas closure, not a 13:00 close."""

    assert calendar.is_early_close(date(2021, 12, 24)) is False
    assert calendar.session(date(2021, 12, 24)).kind is SessionKind.CLOSED


# ------------------------------------------------------------------- bar expectations


def test_hourly_expectation_counts_the_partial_final_bar(calendar: SessionCalendar) -> None:
    """09:30-16:00 is 6.5 hours: seven hourly bars, the last one half length."""

    assert calendar.expected_bar_count(date(2024, 5, 1)) == 7


def test_hourly_expectation_shrinks_on_a_half_day(calendar: SessionCalendar) -> None:
    """09:30-13:00 is 3.5 hours: four bars. Counting seven would invent three gaps."""

    assert calendar.expected_bar_count(date(2024, 7, 3)) == 4


def test_a_closed_day_expects_no_bars(calendar: SessionCalendar) -> None:
    assert calendar.expected_bar_count(date(2024, 7, 4)) == 0
    assert calendar.expected_bar_count(date(2012, 10, 29)) == 0


def test_finer_resolutions_scale(calendar: SessionCalendar) -> None:
    assert calendar.expected_bar_count(date(2024, 5, 1), bars_per_hour=2) == 13
    assert calendar.expected_bar_count(date(2024, 7, 3), bars_per_hour=2) == 7


def test_bars_per_hour_must_be_positive(calendar: SessionCalendar) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        calendar.expected_bar_count(date(2024, 5, 1), bars_per_hour=0)


# --------------------------------------------------------------- fail-closed coverage


def test_dates_before_the_pinned_range_are_refused(calendar: SessionCalendar) -> None:
    with pytest.raises(CalendarCoverageError, match="outside the pinned calendar range"):
        calendar.is_session(date(1997, 12, 31))


def test_dates_after_the_pinned_range_are_refused(calendar: SessionCalendar) -> None:
    """An unknown future ad-hoc closure must refuse, never resolve to 'open'."""

    with pytest.raises(CalendarCoverageError, match="extend _AD_HOC_CLOSURES"):
        calendar.is_session(date(2027, 3, 1))


def test_half_day_knowledge_is_pinned_more_narrowly_than_sessions() -> None:
    """1999 is a covered session year but not a covered half-day year."""

    calendar = SessionCalendar(first_covered=date(1998, 1, 1), last_covered=LAST_COVERED_DATE)
    assert calendar.is_session(date(1999, 7, 6)) is True
    with pytest.raises(CalendarCoverageError, match="pinned early-close range"):
        calendar.is_early_close(date(1999, 7, 6))
    with pytest.raises(CalendarCoverageError, match="pinned early-close range"):
        calendar.expected_bar_count(date(1999, 7, 6))


def test_a_range_query_refuses_if_either_endpoint_is_uncovered(
    calendar: SessionCalendar,
) -> None:
    with pytest.raises(CalendarCoverageError):
        calendar.sessions(date(2026, 12, 1), date(2027, 1, 5))


def test_reversed_ranges_are_rejected(calendar: SessionCalendar) -> None:
    with pytest.raises(ValueError, match="start must not follow end"):
        calendar.sessions(date(2024, 5, 2), date(2024, 5, 1))


def test_a_narrowed_calendar_refuses_outside_its_own_bounds() -> None:
    calendar = SessionCalendar(first_covered=date(2020, 1, 1), last_covered=date(2020, 12, 31))
    assert calendar.first_covered == date(2020, 1, 1)
    assert calendar.last_covered == date(2020, 12, 31)
    assert calendar.expected_session_count(date(2020, 1, 1), date(2020, 12, 31)) == 253
    with pytest.raises(CalendarCoverageError):
        calendar.is_session(date(2021, 1, 4))


def test_construction_rejects_an_inverted_range() -> None:
    with pytest.raises(ValueError, match="must not follow"):
        SessionCalendar(first_covered=date(2021, 1, 1), last_covered=date(2020, 1, 1))


def test_the_declared_bounds_are_the_defaults(calendar: SessionCalendar) -> None:
    assert calendar.first_covered == FIRST_COVERED_DATE
    assert calendar.last_covered == LAST_COVERED_DATE
    assert calendar.session(date(2024, 5, 1)).open_time == time(9, 30)
