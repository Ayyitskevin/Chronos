"""The bars plane must not fabricate, and must not block (M8c, ADR-0019).

Every other terminal panel reads a database row: cheap, repeatable, and nobody's
problem if it is asked twice. Bars are the exception, and the exception is what
this file is about. A miss is a ``reqHistoricalData`` on the one broker
connection this process shares with the order pipeline and the autonomy tick, and
IBKR paces historical requests harder than anything else it serves.

Two properties therefore have to hold, and neither is visible by reading the
happy path:

1. **Pacing degrades, it never waits.** The histdata backfill process answers a
   pacing delay by sleeping. Doing that here would put the terminal's chart in
   front of an order submission in the same event loop, so a paced-out request
   must serve what is cached — labelled stale — or refuse outright.
2. **Nothing is invented to fill a gap.** No cache means a refusal carrying the
   broker's own words, not an empty series that reads as "this instrument did
   nothing".

The cache tests are here rather than in a unit file for the same reason: a cache
that quietly missed would turn a 2-minute chart poll into a broker request every
2 minutes per open panel, which is a safety property wearing a performance
costume.
"""

from __future__ import annotations

from collections.abc import Coroutine
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from chronos.api.bars import MAX_LOOKBACK_DAYS, BarProvider
from chronos.broker.base import BrokerDataError
from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.marketdata.pacing import PacingController

_NOW = datetime(2026, 7, 26, 15, 0, tzinfo=UTC)


class _FakeConnection:
    """Runs the adapter coroutine inline; the real one hands it to a loop thread."""

    def run(self, coroutine: Coroutine[Any, Any, Any], *, timeout: float | None = None) -> Any:
        del timeout
        try:
            coroutine.send(None)
        except StopIteration as stop:
            return stop.value
        coroutine.close()
        raise AssertionError("the fake broker must not await anything")


class _FakeBroker:
    """Counts calls, so a test can prove a cache hit issued no request."""

    def __init__(self, *, bars: int = 10, fail: Exception | None = None) -> None:
        self.qualified: list[str] = []
        self.fetches: list[tuple[str, int]] = []
        self._bars = bars
        self._fail = fail

    async def qualify_underlying(self, symbol: str) -> Any:
        self.qualified.append(symbol)
        return _contract(symbol)

    async def historical_bars(
        self,
        contract: Any,
        *,
        interval: BarInterval = BarInterval.DAY_1,
        lookback_days: int = 180,
    ) -> BarSeries:
        self.fetches.append((contract.symbol, lookback_days))
        if self._fail is not None:
            raise self._fail
        return _series(contract.symbol, count=min(self._bars, lookback_days))


class _Contract:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.exchange = "SMART"
        self.con_id = 1


def _contract(symbol: str) -> _Contract:
    return _Contract(symbol)


def _series(symbol: str, *, count: int) -> BarSeries:
    bars = tuple(
        Bar(
            symbol=symbol,
            source="ibkr",
            exchange="SMART",
            interval=BarInterval.DAY_1,
            session_date=date(2026, 7, 26) - timedelta(days=count - index),
            timestamp_utc=datetime(2026, 7, 26, 21, 0, tzinfo=UTC) - timedelta(days=count - index),
            open=100.0 + index,
            high=101.0 + index,
            low=99.0 + index,
            close=100.5 + index,
            volume=1_000.0,
        )
        for index in range(count)
    )
    return BarSeries(symbol=symbol, interval=BarInterval.DAY_1, bars=bars)


class _FakeRuntime:
    def __init__(self, broker: _FakeBroker) -> None:
        self.broker = broker
        self.connection = _FakeConnection()


def _provider(broker: _FakeBroker, **kwargs: Any) -> BarProvider:
    return BarProvider(_FakeRuntime(broker), **kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------------ the cache


def test_a_second_request_inside_the_ttl_does_not_reach_the_broker() -> None:
    """The whole point. A chart polling every two minutes must not be a request
    every two minutes on the connection the order pipeline shares."""

    broker = _FakeBroker(bars=200)
    provider = _provider(broker)

    first = provider.bars("SPY", lookback_days=180, now=_NOW)
    second = provider.bars("SPY", lookback_days=180, now=_NOW + timedelta(minutes=5))

    assert first.ok and second.ok
    assert len(broker.fetches) == 1
    assert second.stale is False
    assert len(second.series) == len(first.series)


def test_a_narrower_window_is_sliced_from_what_is_already_held() -> None:
    """Asking for 30 days after 180 is a question the cache can already answer.

    Re-fetching would spend a paced request to obtain a subset of bytes already
    in memory.
    """

    broker = _FakeBroker(bars=200)
    provider = _provider(broker)
    provider.bars("SPY", lookback_days=180, now=_NOW)

    narrow = provider.bars("SPY", lookback_days=30, now=_NOW + timedelta(minutes=1))

    assert len(broker.fetches) == 1
    assert narrow.ok
    assert len(narrow.series) < 200
    assert all(bar.session_date >= (_NOW - timedelta(days=30)).date() for bar in narrow.series)


def test_a_wider_window_than_is_held_refetches() -> None:
    broker = _FakeBroker(bars=400)
    provider = _provider(broker)
    provider.bars("SPY", lookback_days=30, now=_NOW)

    provider.bars("SPY", lookback_days=365, now=_NOW + timedelta(minutes=1))

    assert [lookback for _, lookback in broker.fetches] == [30, 365]


def test_the_cache_is_bounded() -> None:
    """An unbounded dict here is a slow leak in the broker-holding process."""

    broker = _FakeBroker(bars=5)
    # No cooldown: this test is about eviction, and the default per-key cooldown
    # would refuse the re-request for a pacing reason and hide what is being
    # asserted.
    provider = _provider(broker, max_cached=2, pacing=PacingController(cooldown=timedelta(0)))
    for symbol in ("AAA", "BBB", "CCC"):
        provider.bars(symbol, lookback_days=10, now=_NOW)

    # AAA was evicted, so asking again is a fetch rather than a hit.
    provider.bars("AAA", lookback_days=10, now=_NOW + timedelta(seconds=1))
    assert [symbol for symbol, _ in broker.fetches] == ["AAA", "BBB", "CCC", "AAA"]


# ----------------------------------------------------------------- the pacing


def test_a_paced_out_request_serves_the_cache_and_says_it_is_stale() -> None:
    """Never a sleep. The request handler returns immediately with a labelled answer."""

    broker = _FakeBroker(bars=50)
    # A cooldown longer than the TTL guarantees the refresh is refused while the
    # entry is expired — the exact window where a naive implementation blocks.
    provider = _provider(
        broker,
        ttl=timedelta(seconds=1),
        pacing=PacingController(cooldown=timedelta(minutes=30)),
    )
    provider.bars("SPY", lookback_days=30, now=_NOW)

    later = provider.bars("SPY", lookback_days=30, now=_NOW + timedelta(minutes=5))

    assert len(broker.fetches) == 1, "a paced-out refresh must not reach the broker"
    assert later.ok
    assert later.stale is True
    assert len(later.series) > 0
    assert later.fetched_at == _NOW


def test_a_paced_out_request_with_nothing_cached_refuses_rather_than_waiting() -> None:
    broker = _FakeBroker(bars=50)
    provider = _provider(broker, pacing=PacingController(cooldown=timedelta(minutes=30)))
    provider.bars("SPY", lookback_days=30, now=_NOW)

    other = provider.bars("SPY", lookback_days=90, now=_NOW + timedelta(seconds=5))

    assert other.ok is False
    assert "paced" in other.refusal
    assert len(other.series) == 0
    assert len(broker.fetches) == 1


def test_a_cache_hit_does_not_spend_pacing_budget() -> None:
    """Budget is for requests the gateway saw. Charging a hit would throttle the
    terminal against a limit it never approached."""

    broker = _FakeBroker(bars=50)
    pacing = PacingController(max_per_window=2, window=timedelta(minutes=10))
    provider = _provider(broker, pacing=pacing)

    provider.bars("SPY", lookback_days=30, now=_NOW)
    for offset in range(10):
        provider.bars("SPY", lookback_days=30, now=_NOW + timedelta(seconds=offset))

    # One symbol still leaves room for a second under a two-per-window budget.
    fresh = provider.bars("QQQ", lookback_days=30, now=_NOW + timedelta(seconds=30))
    assert fresh.ok
    assert [symbol for symbol, _ in broker.fetches] == ["SPY", "QQQ"]


# ------------------------------------------------------------------ refusals


def test_a_broker_failure_with_nothing_cached_refuses_in_the_brokers_words() -> None:
    """ "No security definition" tells an operator what to do. "Unavailable" sends
    them looking in the wrong place."""

    broker = _FakeBroker(fail=BrokerDataError("no security definition for WAT"))
    provider = _provider(broker)

    answer = provider.bars("WAT", lookback_days=30, now=_NOW)

    assert answer.ok is False
    assert "no security definition" in answer.refusal
    assert len(answer.series) == 0


def test_a_broker_failure_serves_the_cache_as_stale_rather_than_going_blank() -> None:
    broker = _FakeBroker(bars=50)
    provider = _provider(broker, ttl=timedelta(seconds=1))
    provider.bars("SPY", lookback_days=30, now=_NOW)
    broker._fail = BrokerDataError("gateway went away")

    answer = provider.bars("SPY", lookback_days=30, now=_NOW + timedelta(minutes=5))

    assert answer.ok
    assert answer.stale is True
    assert len(answer.series) > 0


@pytest.mark.parametrize(
    "symbol, lookback",
    [("", 30), ("  ", 30), ("SP Y", 30), ("SPY", 0), ("SPY", MAX_LOOKBACK_DAYS + 1)],
)
def test_a_bad_request_is_refused_without_touching_the_broker(symbol: str, lookback: int) -> None:
    """Validation before the connection, so a typo cannot spend paced budget."""

    broker = _FakeBroker()
    provider = _provider(broker)

    answer = provider.bars(symbol, lookback_days=lookback, now=_NOW)

    assert answer.ok is False
    assert broker.fetches == []
    assert broker.qualified == []


def test_a_failed_fetch_still_spends_no_budget_it_did_not_use() -> None:
    """A request the gateway refused before answering still counted at the
    gateway, so the budget must move — otherwise a broken symbol becomes an
    unthrottled retry loop against the connection that submits orders."""

    broker = _FakeBroker(fail=BrokerDataError("down"))
    pacing = PacingController(cooldown=timedelta(minutes=30))
    provider = _provider(broker, pacing=pacing)

    provider.bars("SPY", lookback_days=30, now=_NOW)
    second = provider.bars("SPY", lookback_days=30, now=_NOW + timedelta(seconds=1))

    assert len(broker.fetches) == 1, "a failed fetch must not be retried without pacing"
    assert second.ok is False
