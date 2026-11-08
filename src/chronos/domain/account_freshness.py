"""Require bounded local-read age and verified adapter response provenance for money use."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from chronos.domain.enums import AccountSummaryProvenance, BrokerMode

MAX_ACCOUNT_SUMMARY_AGE_SECONDS = 30


class AccountSummaryFreshnessReason(StrEnum):
    MISSING_TIMESTAMP = "MISSING_TIMESTAMP"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    INVALID_CLOCK = "INVALID_CLOCK"
    INVALID_MAX_AGE = "INVALID_MAX_AGE"
    FUTURE_DATED = "FUTURE_DATED"
    STALE = "STALE"
    UNVERIFIED_PROVENANCE = "UNVERIFIED_PROVENANCE"


class AccountSummaryFreshnessError(ValueError):
    """An account summary cannot safely support a risk or submission decision."""

    def __init__(self, reason: AccountSummaryFreshnessReason) -> None:
        self.reason = reason
        super().__init__(f"account summary freshness refused: {reason.value.lower()}")


def account_summary_check_time(*, broker_mode: BrokerMode, demo_now: datetime) -> datetime:
    """Sample wall time after a live read, or retain the deterministic DEMO clock."""

    if broker_mode is BrokerMode.DEMO:
        return demo_now
    return datetime.now(UTC)


def account_summary_provenance(summary: object) -> AccountSummaryProvenance:
    """Preserve only a typed verified label; missing/broken reads stay UNKNOWN."""

    try:
        value = getattr(summary, "provenance", AccountSummaryProvenance.UNKNOWN)
    except Exception:
        return AccountSummaryProvenance.UNKNOWN
    if value is AccountSummaryProvenance.VERIFIED_RESPONSE:
        return AccountSummaryProvenance.VERIFIED_RESPONSE
    return AccountSummaryProvenance.UNKNOWN


def require_account_summary_freshness(
    as_of: datetime | None,
    *,
    now: datetime,
    max_age_seconds: int,
    provenance: AccountSummaryProvenance = AccountSummaryProvenance.UNKNOWN,
) -> None:
    """Refuse unverified provenance and invalid or over-age local read stamps.

    The configured age is defense-in-depth bounded here as well as in Settings,
    so a non-Settings caller cannot widen the maximum observation window. The
    stamp does not prove when the broker valued or last updated cached balances.
    Provenance is a trusted adapter assertion of a direct response, including
    controlled local/simulated replies, not broker valuation-time attestation.
    Missing or malformed proof never becomes trusted because a stamp is new.
    """

    if (
        type(max_age_seconds) is not int
        or not 1 <= max_age_seconds <= MAX_ACCOUNT_SUMMARY_AGE_SECONDS
    ):
        raise AccountSummaryFreshnessError(AccountSummaryFreshnessReason.INVALID_MAX_AGE)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise AccountSummaryFreshnessError(AccountSummaryFreshnessReason.INVALID_CLOCK)
    if as_of is None:
        raise AccountSummaryFreshnessError(AccountSummaryFreshnessReason.MISSING_TIMESTAMP)
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise AccountSummaryFreshnessError(AccountSummaryFreshnessReason.INVALID_TIMESTAMP)

    observed_at = as_of.astimezone(UTC)
    checked_at = now.astimezone(UTC)
    age = checked_at - observed_at
    if age < timedelta(0):
        raise AccountSummaryFreshnessError(AccountSummaryFreshnessReason.FUTURE_DATED)
    if age > timedelta(seconds=max_age_seconds):
        raise AccountSummaryFreshnessError(AccountSummaryFreshnessReason.STALE)
    if provenance is not AccountSummaryProvenance.VERIFIED_RESPONSE:
        raise AccountSummaryFreshnessError(AccountSummaryFreshnessReason.UNVERIFIED_PROVENANCE)
