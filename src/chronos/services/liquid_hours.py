"""IBKR ``liquidHours`` as broker session evidence (M9, closes R-26).

:mod:`chronos.services.trading_hours` has been able to answer "is this family
tradable right now" since Milestone 5, and has been unable to *actually* answer
it for equities and options that whole time. Its contract is tri-state —
``True``/``False``/``None`` — and the only production supplier,
``BrokerRiskEvidenceProvider._broker_confirms_open``, hard-returned ``None``. So
every equity and option instant inside regular trading hours resolved to
``AMBIGUOUS``, which blocks. Fail-closed, so never a money hazard; but it also
means **no live equity or option order could pass the risk engine at all**, and
the session gate had never once been exercised (RISK_REGISTER R-26).

This module is the missing supplier. It parses what IBKR already returns
alongside every qualified contract and turns it into that tri-state.

## Why a calendar could never have done this

The weekday-and-clock rule in ``trading_hours`` rules out weekends and
out-of-hours instants on its own. What it cannot rule out is a **holiday**:
2026-07-03 is a Friday at 11:00 New York and the exchange is shut. No amount of
local reasoning fixes that, which is why the gate stays ambiguous rather than
guessing — and why ``CLOSED`` in ``liquidHours`` is the single most important
token this parser reads. It is the venue telling you a normal-looking weekday is
not a trading day.

## The formats

IBKR has shipped two shapes for this field and both are still seen:

- ``20090507:0700-1830,20090508:0700-1830`` — legacy, times only on the range.
- ``20180323:0400-20180323:2000;20180326:0400-20180326:2000`` — current, with a
  date on both ends, which is what makes an overnight session expressible.

Separators are ``;`` or ``,`` depending on vintage. A closed day is
``20090507:CLOSED``. Times are local to the contract's ``timeZoneId``, which is
an IBKR zone name (``US/Eastern``) rather than always an IANA one.

## What it refuses to do

Anything it cannot parse becomes ``None``, and ``None`` means *ambiguous*, which
blocks. That direction is deliberate and it is the whole safety argument: a
parser bug, an unknown timezone, or a format IBKR changes tomorrow all degrade to
"we do not know", never to "open". The one thing this module must never do is
manufacture a ``True``, because a spurious ``True`` is the only output here that
can let an order through a gate that should have held it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_logger = logging.getLogger("chronos.services.liquid_hours")

#: IBKR zone names that are not IANA identifiers. Mapped rather than guessed;
#: an unmapped unknown stays unknown and the session goes ambiguous.
_ZONE_ALIASES = {
    "GMT": "Etc/GMT",
    "UTC": "Etc/UTC",
    "MET": "Europe/Paris",
    "EST": "America/New_York",
    "EST5EDT": "America/New_York",
    "CST": "America/Chicago",
    "CST6CDT": "America/Chicago",
    "MST": "America/Denver",
    "MST7MDT": "America/Denver",
    "PST": "America/Los_Angeles",
    "PST8PDT": "America/Los_Angeles",
    "JST": "Asia/Tokyo",
    "AET": "Australia/Sydney",
}

_CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """One liquid window, in UTC."""

    opens_at: datetime
    closes_at: datetime

    def contains(self, moment: datetime) -> bool:
        return self.opens_at <= moment < self.closes_at


@dataclass(frozen=True, slots=True)
class LiquidHours:
    """Parsed ``liquidHours`` for one contract.

    ``closed_dates`` is carried separately from the absence of a window, because
    the two are different claims. A date IBKR explicitly marked ``CLOSED`` is the
    venue saying "not a trading day"; a date the string never mentioned is simply
    outside what we were told, and answering "closed" for it would be inventing
    evidence.
    """

    windows: tuple[SessionWindow, ...]
    closed_dates: frozenset[str]
    covered_dates: frozenset[str]
    timezone: str

    def confirms_open(self, moment: datetime) -> bool | None:
        """Tri-state session evidence for ``moment``.

        ``True`` inside a liquid window. ``False`` on a day this data covers but
        whose windows exclude the instant — including an explicit ``CLOSED``,
        which is the holiday case. ``None`` when the data says nothing about that
        day, because not having been told is not the same as having been told no.
        """

        instant = moment.astimezone(UTC)
        for window in self.windows:
            if window.contains(instant):
                return True

        # Which local day the instant falls on, in the venue's own zone: a
        # 20:30 New York instant is still 2026-07-02 there while already
        # 2026-07-03 in UTC, and the venue's calendar is the one being asked.
        try:
            local_day = instant.astimezone(ZoneInfo(self.timezone)).strftime("%Y%m%d")
        except (ZoneInfoNotFoundError, ValueError):
            return None

        if local_day in self.closed_dates:
            return False
        if local_day in self.covered_dates:
            return False
        return None


def resolve_zone(time_zone_id: str) -> str | None:
    """An IBKR zone name as an IANA one, or ``None`` when it cannot be resolved."""

    name = (time_zone_id or "").strip()
    if not name:
        return None
    candidate = _ZONE_ALIASES.get(name.upper(), name)
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return candidate


def parse_liquid_hours(raw: str, *, time_zone_id: str) -> LiquidHours | None:
    """Parse one ``liquidHours`` string, or ``None`` if it cannot be trusted.

    Returns ``None`` — never a partial result — when the timezone is unresolvable
    or when nothing in the string parsed. A half-read schedule is worse than no
    schedule: it would answer ``False`` for days it simply failed to read, and a
    confident wrong "closed" is how a working system looks broken.

    Individual malformed segments inside an otherwise good string *are* skipped,
    with a warning. That asymmetry is deliberate: one unreadable day should not
    discard a week of good evidence, and a skipped day degrades to ``None``
    (ambiguous) rather than to anything permissive.
    """

    zone_name = resolve_zone(time_zone_id)
    if zone_name is None:
        _logger.warning(
            "Unresolvable contract timezone %r; session evidence stays unavailable",
            time_zone_id,
            extra={"event": "liquid_hours_zone_unknown"},
        )
        return None

    zone = ZoneInfo(zone_name)
    text = (raw or "").strip()
    if not text:
        return None

    windows: list[SessionWindow] = []
    closed: set[str] = set()
    covered: set[str] = set()
    parsed_any = False

    for segment in text.replace(";", ",").split(","):
        piece = segment.strip()
        if not piece:
            continue
        outcome = _parse_segment(piece, zone)
        if outcome is None:
            _logger.warning(
                "Skipping unparseable liquidHours segment %r",
                piece,
                extra={"event": "liquid_hours_segment_unparseable"},
            )
            continue
        parsed_any = True
        day, window = outcome
        covered.add(day)
        if window is None:
            closed.add(day)
        else:
            windows.append(window)

    if not parsed_any:
        return None
    return LiquidHours(
        windows=tuple(windows),
        closed_dates=frozenset(closed),
        covered_dates=frozenset(covered),
        timezone=zone_name,
    )


def _parse_segment(piece: str, zone: ZoneInfo) -> tuple[str, SessionWindow | None] | None:
    """One ``YYYYMMDD:...`` segment as ``(local_day, window_or_None)``."""

    head, separator, tail = piece.partition(":")
    if not separator or not _is_day(head):
        return None
    body = tail.strip()

    if body.upper() == _CLOSED:
        return head, None

    start_text, dash, end_text = body.partition("-")
    if not dash:
        return None

    start = _moment(head, start_text.strip(), zone)
    end = _moment(head, end_text.strip(), zone, default_day=head)
    if start is None or end is None:
        return None
    if end <= start:
        # A window that does not advance is not a window. IBKR expresses an
        # overnight session by naming the next date on the closing side, so a
        # non-advancing pair means the segment was misread rather than that the
        # venue opened and shut in the same instant.
        return None
    return head, SessionWindow(opens_at=start, closes_at=end)


def _moment(
    day: str, text: str, zone: ZoneInfo, *, default_day: str | None = None
) -> datetime | None:
    """``HHMM`` or ``YYYYMMDD:HHMM`` in the venue's zone, returned as UTC."""

    value = text.strip()
    if ":" in value:
        day_part, _, clock = value.partition(":")
        if not _is_day(day_part):
            return None
        day, value = day_part, clock.strip()
    elif default_day is not None:
        day = default_day

    if len(value) != 4 or not value.isdigit():
        return None
    hour, minute = int(value[:2]), int(value[2:])
    # IBKR uses 2400 for a midnight close; Python's time does not.
    if hour == 24 and minute == 0:
        try:
            base = datetime.strptime(day, "%Y%m%d").replace(tzinfo=zone)
        except ValueError:
            return None
        from datetime import timedelta

        return (base + timedelta(days=1)).astimezone(UTC)
    if hour > 23 or minute > 59:
        return None
    try:
        local = datetime.strptime(day, "%Y%m%d").replace(hour=hour, minute=minute, tzinfo=zone)
    except ValueError:
        return None
    return local.astimezone(UTC)


def _is_day(value: str) -> bool:
    return len(value) == 8 and value.isdigit()
