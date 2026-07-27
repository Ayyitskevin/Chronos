"""Session evidence must never manufacture an "open" (M9, R-26).

This parser is the only production supplier of ``broker_confirms_open``, and that
flag is the only thing that can turn an in-hours equity instant from AMBIGUOUS
(blocked) into OPEN (submittable). Every other output it can produce fails
closed. So the asymmetry is the whole safety argument, and it is what this file
asserts hardest:

- a spurious ``True`` opens a gate that should have held — the one unrecoverable
  direction;
- a spurious ``False`` or ``None`` blocks an order that could have gone — visible,
  annoying, and safe.

Which is why the malformed-input cases below outnumber the happy path, and why
the holiday case gets its own section: ``CLOSED`` in ``liquidHours`` is the venue
telling you a normal-looking Friday is not a trading day, and it is the single
fact a calendar can never derive.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from chronos.services.liquid_hours import parse_liquid_hours, resolve_zone

_NY = "US/Eastern"

# 2026-07-02 is a Thursday; 2026-07-03 a Friday the US market observes as a
# holiday. Both are inside regular trading hours by the clock, which is exactly
# when broker evidence is the only thing that can tell them apart.
_OPEN_DAY = "20260702"
_HOLIDAY = "20260703"

_TWO_DAYS = f"{_OPEN_DAY}:0930-1600,{_HOLIDAY}:CLOSED"


def _at(day: str, hour: int, minute: int = 0) -> datetime:
    """A New York wall-clock instant, as UTC.

    Built in the venue's own zone and converted, rather than by adding the EDT
    offset by hand — an offset-arithmetic helper overflows past midnight on the
    late-evening cases, which are exactly the ones the date-boundary tests below
    depend on.
    """

    local = datetime(
        int(day[0:4]), int(day[4:6]), int(day[6:8]), hour, minute, tzinfo=ZoneInfo(_NY)
    )
    return local.astimezone(UTC)


# ------------------------------------------------------------------- the happy path


def test_an_instant_inside_a_liquid_window_confirms_open() -> None:
    hours = parse_liquid_hours(_TWO_DAYS, time_zone_id=_NY)
    assert hours is not None
    assert hours.confirms_open(_at(_OPEN_DAY, 11)) is True


def test_the_current_format_with_a_date_on_both_ends_parses() -> None:
    raw = f"{_OPEN_DAY}:0930-{_OPEN_DAY}:1600"
    hours = parse_liquid_hours(raw, time_zone_id=_NY)
    assert hours is not None
    assert hours.confirms_open(_at(_OPEN_DAY, 11)) is True
    assert hours.confirms_open(_at(_OPEN_DAY, 17)) is False


def test_semicolons_and_commas_are_both_accepted() -> None:
    """IBKR has shipped both separators; a vintage difference must not read as
    a schedule we could not parse."""

    commas = parse_liquid_hours(_TWO_DAYS, time_zone_id=_NY)
    semis = parse_liquid_hours(_TWO_DAYS.replace(",", ";"), time_zone_id=_NY)
    assert commas is not None and semis is not None
    assert commas.windows == semis.windows
    assert commas.closed_dates == semis.closed_dates


def test_an_overnight_window_spans_the_date_boundary() -> None:
    """A close on the next date is how IBKR expresses a session that runs past
    midnight; reading it as a same-day window would silently truncate it."""

    raw = f"{_OPEN_DAY}:1800-20260703:0200"
    hours = parse_liquid_hours(raw, time_zone_id=_NY)
    assert hours is not None
    assert hours.confirms_open(_at(_OPEN_DAY, 23)) is True


def test_a_2400_close_is_midnight_not_an_invalid_hour() -> None:
    raw = f"{_OPEN_DAY}:0930-2400"
    hours = parse_liquid_hours(raw, time_zone_id=_NY)
    assert hours is not None
    assert hours.confirms_open(_at(_OPEN_DAY, 23, 59)) is True


# ------------------------------------------------------------------- the holiday


def test_a_closed_day_reports_closed_not_unknown() -> None:
    """The reason this module exists.

    2026-07-03 is a Friday at 11:00 New York — a weekday, inside regular trading
    hours, and shut. The calendar in `trading_hours` cannot know that; only the
    venue can say it, and it says it with CLOSED.
    """

    hours = parse_liquid_hours(_TWO_DAYS, time_zone_id=_NY)
    assert hours is not None
    assert hours.confirms_open(_at(_HOLIDAY, 11)) is False


def test_a_day_outside_the_data_is_unknown_not_closed() -> None:
    """Not having been told is not the same as having been told no.

    Answering "closed" for a day the string never mentioned would be inventing
    evidence — and it would look identical to a real holiday in the audit trail.
    """

    hours = parse_liquid_hours(_TWO_DAYS, time_zone_id=_NY)
    assert hours is not None
    assert hours.confirms_open(_at("20260710", 11)) is None


def test_an_out_of_window_instant_on_a_covered_day_is_closed() -> None:
    hours = parse_liquid_hours(_TWO_DAYS, time_zone_id=_NY)
    assert hours is not None
    assert hours.confirms_open(_at(_OPEN_DAY, 3)) is False
    assert hours.confirms_open(_at(_OPEN_DAY, 20)) is False


def test_the_local_day_is_the_venues_day_not_utcs() -> None:
    """An 20:30 New York instant is already the next date in UTC. Asking UTC's
    calendar would look up the wrong day and mis-report a holiday."""

    hours = parse_liquid_hours(_TWO_DAYS, time_zone_id=_NY)
    assert hours is not None
    # 20:30 EDT on the open day == 00:30 UTC on the holiday date.
    late = _at(_OPEN_DAY, 20, 30)
    assert late.astimezone(UTC).strftime("%Y%m%d") == _HOLIDAY
    # Still resolved against the venue's own day, which is covered and closed.
    assert hours.confirms_open(late) is False


# ------------------------------------------------- everything that must not open


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "garbage",
        "20260702",
        "20260702:",
        "20260702:0930",
        "2026:0930-1600",
        "20260702:9999-1600",
        "20260702:0930-9999",
        "20260702:093-1600",
        "20260702:0930-0930",
        "20260702:1600-0930",
        "notadate:0930-1600",
    ],
)
def test_unparseable_input_is_never_an_open(raw: str) -> None:
    """Every shape of malformed input degrades to unknown, which blocks.

    The parametrization is the point: a parser that answered True for any one of
    these would open a gate on a string nobody could read.
    """

    hours = parse_liquid_hours(raw, time_zone_id=_NY)
    if hours is None:
        return
    assert hours.confirms_open(_at(_OPEN_DAY, 11)) is not True


def test_an_unresolvable_timezone_yields_no_evidence() -> None:
    """Guessing a zone would shift every window by hours and could place a
    closed instant inside one."""

    assert parse_liquid_hours(_TWO_DAYS, time_zone_id="Mars/Olympus") is None
    assert parse_liquid_hours(_TWO_DAYS, time_zone_id="") is None


def test_one_bad_segment_does_not_discard_the_good_ones() -> None:
    """A single unreadable day should not throw away a week of real evidence —
    and the skipped day degrades to unknown, never to open."""

    raw = f"{_OPEN_DAY}:0930-1600,BROKEN,{_HOLIDAY}:CLOSED"
    hours = parse_liquid_hours(raw, time_zone_id=_NY)
    assert hours is not None
    assert hours.confirms_open(_at(_OPEN_DAY, 11)) is True
    assert hours.confirms_open(_at(_HOLIDAY, 11)) is False


def test_a_string_of_only_bad_segments_is_no_evidence() -> None:
    assert parse_liquid_hours("BROKEN,ALSOBROKEN", time_zone_id=_NY) is None


# ----------------------------------------------------------------- the zone map


@pytest.mark.parametrize(
    "ibkr_name, expected",
    [("US/Eastern", "US/Eastern"), ("EST5EDT", "America/New_York"), ("GMT", "Etc/GMT")],
)
def test_ibkr_zone_names_resolve(ibkr_name: str, expected: str) -> None:
    assert resolve_zone(ibkr_name) == expected


def test_an_unknown_zone_resolves_to_nothing() -> None:
    assert resolve_zone("NOT_A_ZONE") is None
