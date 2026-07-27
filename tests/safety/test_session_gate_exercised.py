"""The equity session gate, finally exercised end to end (M9, closes R-26).

R-26's sharpest sentence was not that the gate was wrong — it was that the gate
had **never been exercised**. ``_broker_confirms_open`` hard-returned ``None``
from Milestone 5 onward, so every equity and option instant inside regular
trading hours resolved to ``AMBIGUOUS`` and blocked. Fail-closed, so never a
money hazard; but it also meant no live equity or option order could pass the
risk engine at all, and no test had ever seen the gate say ``OPEN``.

So this file drives the whole path — a qualified contract carrying IBKR's own
``liquidHours``, through the provider method that reads it, into the session
decision that gates submission — and asserts all three outcomes, including the
one that had never happened.

The holiday case is the reason any of this exists. 2026-07-03 is a Friday at
11:00 New York: a weekday, inside regular trading hours, and shut. No calendar
can know that. Only the venue can say it, and it says it with ``CLOSED``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from chronos.domain.enums import OrderIntent, ProductFamily
from chronos.domain.models import UnderlyingContract
from chronos.orders.evidence import BrokerRiskEvidenceProvider
from chronos.orders.intent import build_stock_intent
from chronos.services.trading_hours import MarketSession, session_for

_NY = "US/Eastern"
_OPEN_DAY = "20260702"  # Thursday, a normal session
_HOLIDAY = "20260703"  # Friday, observed holiday — the case only the venue knows

_LIQUID_HOURS = f"{_OPEN_DAY}:0930-1600,{_HOLIDAY}:CLOSED"


def _ny(day: str, hour: int, minute: int = 0) -> datetime:
    local = datetime(
        int(day[0:4]), int(day[4:6]), int(day[6:8]), hour, minute, tzinfo=ZoneInfo(_NY)
    )
    return local.astimezone(UTC)


def _contract(*, hours: str = _LIQUID_HOURS, zone: str = _NY) -> UnderlyingContract:
    return UnderlyingContract(
        con_id=756733,
        symbol="SPY",
        liquid_hours=hours,
        time_zone_id=zone,
    )


def _intent(contract: UnderlyingContract) -> Any:
    return build_stock_intent(
        account_id="DU1234567",
        intent=OrderIntent.OPEN_LONG_STOCK,
        contract=contract,
        quantity=1,
        limit_price=Decimal("400.00"),
    )


def _provider() -> BrokerRiskEvidenceProvider:
    """A provider built for one method.

    ``_broker_confirms_open`` reads the intent's contract and nothing else — no
    connection, no settings, no gateway. That is the property worth having: the
    session answer costs no broker round-trip, because the evidence arrived with
    the qualification that already happened.
    """

    return BrokerRiskEvidenceProvider(connection=None, settings=None)  # type: ignore[arg-type]


def _session(contract: UnderlyingContract, moment: datetime) -> MarketSession:
    confirms = _provider()._broker_confirms_open(_intent(contract), moment)
    return session_for(
        ProductFamily.STOCK,
        now=moment,
        market_timezone="America/New_York",
        broker_confirms_open=confirms,
    ).session


def test_an_in_hours_instant_with_broker_evidence_is_finally_open() -> None:
    """The assertion that could not have been written before M9.

    Everything needed for this was already arriving on every qualification; it
    was being dropped one attribute short of the code that needed it.
    """

    decision = session_for(
        ProductFamily.STOCK,
        now=_ny(_OPEN_DAY, 11),
        market_timezone="America/New_York",
        broker_confirms_open=_provider()._broker_confirms_open(
            _intent(_contract()), _ny(_OPEN_DAY, 11)
        ),
    )
    assert decision.session is MarketSession.OPEN
    assert decision.may_submit is True


def test_a_holiday_inside_trading_hours_is_closed_not_ambiguous() -> None:
    """The case the calendar cannot reach.

    A Friday at 11:00 New York passes every local check — weekday, inside RTH —
    and the exchange is shut. Before M9 this was AMBIGUOUS, which blocked for the
    right outcome but the wrong reason: not "the venue says closed", merely "we
    have no idea".
    """

    decision = session_for(
        ProductFamily.STOCK,
        now=_ny(_HOLIDAY, 11),
        market_timezone="America/New_York",
        broker_confirms_open=_provider()._broker_confirms_open(
            _intent(_contract()), _ny(_HOLIDAY, 11)
        ),
    )
    assert decision.session is MarketSession.CLOSED
    assert decision.may_submit is False
    assert "closed" in decision.reason


def test_without_evidence_the_gate_still_blocks_exactly_as_before() -> None:
    """M9 must not have quietly turned "unknown" into "go".

    A contract with no hours — the demo adapter, an older qualification path, a
    gateway that answered without details — lands back on the pre-M9 behaviour,
    which is ambiguous and blocking.
    """

    bare = UnderlyingContract(con_id=756733, symbol="SPY")
    assert _session(bare, _ny(_OPEN_DAY, 11)) is MarketSession.AMBIGUOUS


@pytest.mark.parametrize(
    "hours, zone",
    [
        ("", _NY),
        (_LIQUID_HOURS, ""),
        ("total garbage", _NY),
        (_LIQUID_HOURS, "Mars/Olympus"),
    ],
)
def test_unusable_evidence_never_opens_the_gate(hours: str, zone: str) -> None:
    """Every way the evidence can be unusable degrades to blocking.

    This is the direction that matters. A spurious ``OPEN`` here is the only
    output in the whole chain that can let an order through a gate that should
    have held it.
    """

    assert _session(_contract(hours=hours, zone=zone), _ny(_OPEN_DAY, 11)) is not (
        MarketSession.OPEN
    )


def test_a_day_the_venue_never_mentioned_is_ambiguous_not_open() -> None:
    """Stale hours must not authorise a day they never covered.

    ``liquidHours`` is a short rolling window. A week later it says nothing about
    today, and "the schedule I was handed does not mention now" has to read as
    unknown — otherwise an old contract object silently becomes a standing
    permission.
    """

    assert _session(_contract(), _ny("20260710", 11)) is MarketSession.AMBIGUOUS


def test_outside_the_venues_window_on_a_covered_day_is_closed() -> None:
    assert _session(_contract(), _ny(_OPEN_DAY, 3)) is MarketSession.CLOSED
    assert _session(_contract(), _ny(_OPEN_DAY, 20)) is MarketSession.CLOSED
