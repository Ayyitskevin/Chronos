"""Historical bars for the operator terminal, cached and paced (M8c, ADR-0019).

The terminal's other panels read durable state: a database row is free to fetch
and answering twice costs nothing. Bars are not like that. Every miss here is a
``reqHistoricalData`` on the **one** broker connection this process also uses for
the order pipeline and the autonomy tick, and IBKR paces historical requests far
more tightly than quotes. A chart panel polling on the ordinary five-second panel
interval would be an unbounded request source pointed at the most rate-limited
surface the gateway has.

So this module sits between the route and the broker and enforces three things:

## 1. Completed bars are immutable, so the cache is the normal path

A daily bar for a past session never changes. Once fetched it is correct until
the next session closes, which makes a long TTL correct rather than merely
convenient. The cache is keyed by ``(symbol, interval)`` and remembers **how much
history it holds**, so a request for 90 days is served by slicing a cached 180-day
series instead of issuing a second request for a subset of what is already here.

## 2. Pacing never blocks; it degrades

:class:`~chronos.marketdata.pacing.PacingController` answers "how long until this
request is allowed". The histdata backfill process responds to that by sleeping.
This module must not: it runs inside a request handler on the event loop of the
process holding the broker connection, and sleeping there would convert a pacing
limit into latency for the order pipeline and the autonomy tick.

Instead a paced-out request **serves what is cached and says it is stale**, and
refuses outright when there is nothing cached. That is the same rule the rest of
the terminal follows — an operator is better served by a chart labelled "as of
14:02" than by a spinner, and better served by an explicit refusal than by an
unlabelled old one.

## 3. What it will not do

It will not fabricate. A gap in the returned series stays a gap, a failed fetch
with no cache is a refusal carrying the broker's own reason, and nothing here
synthesizes a bar to make a chart look continuous. It also holds **no lock across
a broker call**: two concurrent requests for the same cold symbol will both issue
a fetch, which wastes one request and is strictly better than one request handler
holding a lock while the other waits on the network.

## Disclosed residual

The pacing budget is this process's own. The histdata backfill process paces
itself separately under a different client id, and IBKR's real limits may be
shared across them — nobody can verify that from here without a live gateway.
This is the same residual ADR-0011 §5 has always carried, now with a second
self-pacing caller; :mod:`chronos.marketdata.pacing` says so at the source.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta

from chronos.broker.base import BrokerError
from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.marketdata.pacing import PacingController
from chronos.runtime import AppRuntime
from chronos.utils.time import utc_now

_logger = logging.getLogger("chronos.api.bars")

#: How long a fetched series is served without re-asking. Daily bars change once
#: a session, so fifteen minutes is far below the rate at which the underlying
#: data can move while being high enough that a panel left open does not become a
#: request source. Intraday intervals are not exempted: they are not tradable
#: capabilities today, and a shorter TTL for them would be tuning against a
#: workload nobody has run.
DEFAULT_TTL = timedelta(minutes=15)

#: Symbols kept in the cache. A bound rather than a guess about working-set size:
#: the terminal is one operator's window, and an unbounded dict here would be a
#: slow leak in the process that holds the broker connection.
MAX_CACHED_SERIES = 64

#: The most history one request may ask for. IBKR's duration limits vary by bar
#: size and a request beyond them is an error rather than a truncation, so the
#: ceiling is enforced here where it can be explained.
MAX_LOOKBACK_DAYS = 3650


@dataclass(frozen=True, slots=True)
class BarsAnswer:
    """A series and the honest provenance of it.

    ``refusal`` non-empty means ``series`` is empty and nothing could be served.
    ``stale`` means the series is real but older than its TTL — served because
    re-fetching was paced out, and labelled so the panel can say so.
    """

    series: BarSeries
    fetched_at: datetime | None
    source: str
    stale: bool = False
    refusal: str = ""

    @property
    def ok(self) -> bool:
        return not self.refusal


@dataclass(slots=True)
class _Entry:
    series: BarSeries
    fetched_at: datetime
    lookback_days: int
    source: str


class BarProvider:
    """Cache + pacing in front of ``Broker.historical_bars``."""

    def __init__(
        self,
        runtime: AppRuntime,
        *,
        ttl: timedelta = DEFAULT_TTL,
        pacing: PacingController | None = None,
        max_cached: int = MAX_CACHED_SERIES,
    ) -> None:
        self._runtime = runtime
        self._ttl = ttl
        self._pacing = pacing if pacing is not None else PacingController()
        self._max_cached = max_cached
        self._cache: OrderedDict[tuple[str, BarInterval], _Entry] = OrderedDict()

    def bars(
        self,
        symbol: str,
        *,
        interval: BarInterval = BarInterval.DAY_1,
        lookback_days: int = 180,
        now: datetime | None = None,
    ) -> BarsAnswer:
        """The requested series, from cache when it can be, from the broker when it must."""

        moment = now if now is not None else utc_now()
        name = symbol.strip().upper()
        if not name or not name.isalnum():
            return BarsAnswer(
                series=BarSeries(symbol=name or "?", interval=interval, bars=()),
                fetched_at=None,
                source="",
                refusal="a symbol must be alphanumeric",
            )
        if lookback_days < 1 or lookback_days > MAX_LOOKBACK_DAYS:
            return BarsAnswer(
                series=BarSeries(symbol=name, interval=interval, bars=()),
                fetched_at=None,
                source="",
                refusal=f"lookback_days must be between 1 and {MAX_LOOKBACK_DAYS}",
            )

        key = (name, interval)
        cached = self._cache.get(key)
        covers = cached is not None and cached.lookback_days >= lookback_days
        fresh = cached is not None and moment - cached.fetched_at < self._ttl
        if cached is not None and covers and fresh:
            self._cache.move_to_end(key)
            return BarsAnswer(
                series=_tail(cached.series, lookback_days, moment),
                fetched_at=cached.fetched_at,
                source=cached.source,
            )

        # Ask for at least what is cached, so a 30-day request after a 180-day one
        # does not quietly shrink the cache and make the next wide request a miss.
        want = max(lookback_days, cached.lookback_days if cached is not None else 0)
        pacing_key = f"{name}:{interval.value}"
        delay = self._pacing.delay_before(pacing_key, moment)
        if delay > 0:
            if cached is not None and covers:
                return BarsAnswer(
                    series=_tail(cached.series, lookback_days, moment),
                    fetched_at=cached.fetched_at,
                    source=cached.source,
                    stale=True,
                )
            return BarsAnswer(
                series=BarSeries(symbol=name, interval=interval, bars=()),
                fetched_at=cached.fetched_at if cached is not None else None,
                source=cached.source if cached is not None else "",
                refusal=(
                    f"historical requests are paced; {name} is available in "
                    f"{delay:.0f}s and nothing is cached to show meanwhile"
                ),
            )

        # Recorded BEFORE the call, not after a successful one. The gateway sees
        # the request either way, so a failure that did not consume budget would
        # let a bad symbol retry on every poll with nothing throttling it —
        # against the same connection that submits orders. Found by
        # `test_a_failed_fetch_still_spends_no_budget_it_did_not_use`.
        self._pacing.record(pacing_key, moment)
        try:
            series, source = self._fetch(name, interval=interval, lookback_days=want)
        except BrokerError as error:
            # The broker's own words: an operator who sees "no security definition"
            # can act on it, where "unavailable" sends them looking in the wrong place.
            reason = str(error) or error.__class__.__name__
            if cached is not None and covers:
                _logger.warning(
                    "Serving cached bars for %s after a failed refresh",
                    name,
                    extra={"event": "bars_refresh_failed"},
                )
                return BarsAnswer(
                    series=_tail(cached.series, lookback_days, moment),
                    fetched_at=cached.fetched_at,
                    source=cached.source,
                    stale=True,
                )
            return BarsAnswer(
                series=BarSeries(symbol=name, interval=interval, bars=()),
                fetched_at=None,
                source="",
                refusal=reason,
            )

        self._store(
            key, _Entry(series=series, fetched_at=moment, lookback_days=want, source=source)
        )
        return BarsAnswer(
            series=_tail(series, lookback_days, moment),
            fetched_at=moment,
            source=source,
        )

    def _fetch(
        self, symbol: str, *, interval: BarInterval, lookback_days: int
    ) -> tuple[BarSeries, str]:
        runtime = self._runtime
        contract = runtime.connection.run(runtime.broker.qualify_underlying(symbol))
        series = runtime.connection.run(
            runtime.broker.historical_bars(contract, interval=interval, lookback_days=lookback_days)
        )
        source = series.bars[0].source if series.bars else ""
        return series, source

    def _store(self, key: tuple[str, BarInterval], entry: _Entry) -> None:
        self._cache[key] = entry
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_cached:
            self._cache.popitem(last=False)


def _tail(series: BarSeries, lookback_days: int, now: datetime) -> BarSeries:
    """The last ``lookback_days`` calendar days of a cached series.

    Calendar days rather than a bar count, because that is what the caller asked
    for and what the broker was asked for; a 90-day window over daily bars is
    about 62 bars, and silently returning 90 of them would answer a different
    question.
    """

    cutoff = (now - timedelta(days=lookback_days)).date()
    kept: tuple[Bar, ...] = tuple(bar for bar in series.bars if bar.session_date >= cutoff)
    return BarSeries(symbol=series.symbol, interval=series.interval, bars=kept)


def provider_for(runtime: AppRuntime, state: object) -> BarProvider:
    """The process's single provider, created on first use.

    One per backend, held on the app state rather than per-request: a provider
    rebuilt per request would have an empty cache every time, which would turn
    every panel refresh into a broker request — the exact thing this module
    exists to prevent.
    """

    existing = getattr(state, "bar_provider", None)
    if isinstance(existing, BarProvider):
        return existing
    provider = BarProvider(runtime)
    state.bar_provider = provider  # type: ignore[attr-defined]
    return provider
