"""Timezone-aware timestamp helpers."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

MARKET_TIMEZONE = ZoneInfo("America/New_York")


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Chronos timestamps must be timezone-aware")
    return value


def as_utc(value: datetime) -> datetime:
    return require_aware(value).astimezone(UTC)


def as_market_time(value: datetime) -> datetime:
    return require_aware(value).astimezone(MARKET_TIMEZONE)
