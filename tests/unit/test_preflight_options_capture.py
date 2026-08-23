"""The capture preflight's session-date logic (ADR-0012, docs/histdata_runbook.md).

The only part of the preflight with real logic is which US trading date a capture belongs
to. It matters more than its size suggests: option snapshots are labeled by session, a
mislabeled session is indistinguishable from a real one after the fact, and the data
cannot be re-fetched to correct it. These pin the timezone reasoning so a later
"simplification" to the machine's local date, or to the bare UTC date, fails here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from scripts.preflight_options_capture import (
    eastern_session_date,
    utc_default_mislabels_session,
)

EASTERN = ZoneInfo("America/New_York")


def test_a_run_after_the_close_belongs_to_that_days_session() -> None:
    """21:15 UTC is 17:15 EDT — after the 16:00 close, still the same trading date."""

    now = datetime(2026, 8, 20, 21, 15, tzinfo=UTC)
    assert eastern_session_date(now) == date(2026, 8, 20)
    assert not utc_default_mislabels_session(now)


def test_the_same_wall_clock_in_winter_still_lands_on_the_session() -> None:
    """21:15 UTC is 16:15 EST under standard time — after the close, same date.

    Pins that the UTC-pinned schedule survives the DST transition rather than working
    only for half the year.
    """

    now = datetime(2026, 1, 15, 21, 15, tzinfo=UTC)
    assert eastern_session_date(now) == date(2026, 1, 15)
    assert not utc_default_mislabels_session(now)


def test_a_locally_scheduled_evening_run_mislabels_the_session() -> None:
    """The hazard the UTC pin exists to prevent.

    ``OnCalendar=Mon..Fri 21:15`` (unpinned) fires at 21:15 *local*. In New York that is
    01:15 UTC the next day, so ``--session``'s UTC-date default files Thursday's chain
    under Friday — and Friday's under Saturday, a date with no session at all.
    """

    local_evening = datetime(2026, 8, 20, 21, 15, tzinfo=EASTERN)
    assert local_evening.astimezone(UTC).date() == date(2026, 8, 21)
    assert eastern_session_date(local_evening) == date(2026, 8, 20)
    assert utc_default_mislabels_session(local_evening)


def test_a_friday_evening_local_run_would_file_under_a_non_session_saturday() -> None:
    """The most legible form of the bug: a weekend date that cannot be a session."""

    friday_evening = datetime(2026, 8, 21, 21, 15, tzinfo=EASTERN)
    mislabeled = friday_evening.astimezone(UTC).date()
    assert mislabeled.weekday() == 5  # Saturday
    assert eastern_session_date(friday_evening).weekday() == 4  # the real session, Friday
    assert utc_default_mislabels_session(friday_evening)


def test_before_utc_midnight_the_default_is_safe() -> None:
    """23:59 UTC still shares the date with the ET session; 00:01 UTC no longer does."""

    assert not utc_default_mislabels_session(datetime(2026, 8, 20, 23, 59, tzinfo=UTC))
    assert utc_default_mislabels_session(datetime(2026, 8, 21, 0, 1, tzinfo=UTC))
