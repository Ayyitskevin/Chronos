"""Family-aware trading-session decisions, fail-closed on ambiguity.

Owner spec: never assume every weekday is tradable; block when hours are
ambiguous. This module encodes what can be known *without* broker evidence
and requires broker session confirmation for the rest:

- Equities/options: outside regular trading hours (09:30-16:00
  America/New_York, Monday-Friday) the session is CLOSED. *Inside* RTH the
  calendar alone cannot rule out a holiday, so the decision is AMBIGUOUS
  unless the caller supplies broker-confirmed session evidence (from
  qualified contract ``liquidHours``, wired in Milestone 5). Ambiguity
  blocks submission — by design.
- Crypto (Paxos/Zero Hash): trades continuously; OPEN at any hour, with the
  caveat recorded that venue maintenance windows exist and the broker
  remains the authority at order time.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

from chronos.domain.enums import ProductFamily
from chronos.domain.models import ChronosModel

_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)


class MarketSession(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    AMBIGUOUS = "AMBIGUOUS"


class SessionDecision(ChronosModel):
    family: ProductFamily
    session: MarketSession
    may_submit: bool
    reason: str


def session_for(
    family: ProductFamily,
    *,
    now: datetime,
    market_timezone: str = "America/New_York",
    broker_confirms_open: bool | None = None,
) -> SessionDecision:
    """Decide the session for one family at one instant.

    ``broker_confirms_open`` is tri-state: True/False when broker session
    evidence (liquidHours) is available, None when it is not. Only True can
    turn an in-RTH equity/option instant into OPEN.
    """

    if now.tzinfo is None:
        raise ValueError("session decisions require a timezone-aware timestamp")

    if family is ProductFamily.CRYPTO:
        return SessionDecision(
            family=family,
            session=MarketSession.OPEN,
            may_submit=True,
            reason=(
                "crypto trades ~24/7; venue maintenance windows remain the broker's "
                "authority at order time"
            ),
        )

    local = now.astimezone(ZoneInfo(market_timezone))
    if local.weekday() >= 5:
        return SessionDecision(
            family=family,
            session=MarketSession.CLOSED,
            may_submit=False,
            reason=f"weekend in {market_timezone}",
        )
    in_rth = _RTH_OPEN <= local.time() < _RTH_CLOSE
    if not in_rth:
        return SessionDecision(
            family=family,
            session=MarketSession.CLOSED,
            may_submit=False,
            reason=(
                f"outside regular trading hours ({_RTH_OPEN:%H:%M}-{_RTH_CLOSE:%H:%M} "
                f"{market_timezone})"
            ),
        )
    if broker_confirms_open is True:
        return SessionDecision(
            family=family,
            session=MarketSession.OPEN,
            may_submit=True,
            reason="inside regular trading hours and broker-confirmed open",
        )
    if broker_confirms_open is False:
        return SessionDecision(
            family=family,
            session=MarketSession.CLOSED,
            may_submit=False,
            reason="broker session evidence reports the market closed (holiday or halt)",
        )
    return SessionDecision(
        family=family,
        session=MarketSession.AMBIGUOUS,
        may_submit=False,
        reason=(
            "inside regular trading hours but no broker session evidence; a holiday "
            "cannot be ruled out from the calendar alone, so submission is blocked"
        ),
    )


def utc_now() -> datetime:  # pragma: no cover - convenience for callers
    return datetime.now(tz=UTC)
