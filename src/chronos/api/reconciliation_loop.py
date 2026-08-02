"""Bounded periodic reconciliation (ADR-0020 / D-20).

`ReconciliationReadiness` is consumed by every opening submit and, before this
module, re-established by exactly one caller: the startup call in the backend
lifespan. The first opening order of a process therefore consumed readiness and
nothing ever re-armed it. This task is the refresher.

Three things about its shape are load-bearing:

**It skips rather than queues while a submission is in flight.** ``complete()``
already refuses to publish while ``_submissions_in_flight`` is nonzero, so the
latch is race-safe on its own; spinning against it would only burn broker budget
to be refused.

**It records pacing budget BEFORE the call.** Reconciliation issues requests on
the same single connection the order pipeline and the autonomy tick use. R-42
recorded the first version of this mistake on the chart panel: charging the
budget only on success lets a persistently failing symbol retry unthrottled every
cycle. Budget is spent by *attempting*, not by succeeding.

**It degrades rather than sleeping in anyone's way.** A paced-out cycle is
skipped, not delayed — the latch's own maximum evidence age is what makes that
safe, because a refresher that cannot run simply lets readiness expire and
submission fail closed. That is the whole reason ADR-0020 put expiry in
``snapshot()`` rather than here.

## Why a clock-based session approximation is acceptable HERE

The cadence depends on whether the market is open, and this task holds no
contract, so it cannot read IBKR ``liquidHours`` — the only fact that can tell a
holiday from a trading day (R-26). It uses a weekday-and-clock approximation in
the configured market timezone instead.

That would be unacceptable for a *gate*: R-26 exists precisely because a calendar
cannot derive the ``CLOSED`` token, and the session gate must fail closed on
exactly the days a calendar gets wrong. It is acceptable for a *cadence*, because
the only cost of being wrong is reconciling more often than necessary on a market
holiday. Being wrong here spends requests; it never authorizes anything.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from chronos.runtime import AppRuntime
from chronos.utils.time import utc_now

_logger = logging.getLogger(__name__)

#: Regular trading hours in the configured market timezone. Deliberately the
#: plain RTH window: this picks a polling interval, not a trading permission.
_SESSION_OPEN = time(9, 30)
_SESSION_CLOSE = time(16, 0)

#: Consecutive failures tolerated before the task alerts loudly. It keeps running
#: either way — a refresher that dies on error would silently stop renewing
#: readiness, and while the evidence age makes that fail closed rather than
#: dangerous, an operator should be told the difference between "blocked because
#: the market is shut" and "blocked because reconciliation has been broken for
#: ten minutes".
_FAILURES_BEFORE_ALERT = 2


def market_session_is_open(moment: datetime, timezone_name: str) -> bool:
    """Approximate RTH from the clock. See the module docstring for why."""

    try:
        local = moment.astimezone(ZoneInfo(timezone_name))
    except Exception:  # pragma: no cover - settings validate the zone at boot
        return True  # unknown zone: assume open, which only costs requests
    if local.weekday() >= 5:
        return False
    return _SESSION_OPEN <= local.time() < _SESSION_CLOSE


def next_interval(runtime: AppRuntime, *, positioned: bool, moment: datetime) -> float:
    """Choose the frozen interval for the state we are actually in (ADR-0020 §2)."""

    settings = runtime.settings
    if not market_session_is_open(moment, settings.market_timezone):
        return settings.reconciliation_interval_closed_seconds
    if positioned:
        return settings.reconciliation_interval_active_seconds
    return settings.reconciliation_interval_idle_seconds


def reconcile_once(runtime: AppRuntime) -> tuple[bool, bool]:
    """Run one cycle. Returns (succeeded, positioned).

    ``positioned`` drives the next interval and is taken from the report's own
    working-order evidence, so it costs no extra broker call. A cycle that raises
    reports ``positioned=True`` — the shorter cadence — because a failure means we
    do not know whether the book is flat, and the state we cannot see is the one
    worth looking at sooner.
    """

    try:
        report = runtime.reconcile_submission_readiness()
    except Exception:
        _logger.exception(
            "Periodic reconciliation failed; readiness is left to expire on its own age",
            extra={"event": "periodic_reconciliation_failed", "outcome": "locked"},
        )
        return False, True
    positioned = bool(report.restart.remaining_active)
    _logger.debug(
        "Periodic reconciliation finished %s",
        report.readiness.status.value,
        extra={
            "event": "periodic_reconciliation_finished",
            "reconciliation_status": report.readiness.status.value,
            "positioned": positioned,
        },
    )
    return True, positioned


async def reconciliation_task(runtime: AppRuntime) -> None:
    """Re-arm submission readiness on the ADR-0020 cadence until cancelled.

    Writer-only: the lifespan constructs this task only for the writer, because a
    read-only backend cannot persist recovery and must stay ``PENDING``.
    """

    positioned = True  # assume the costlier cadence until evidence says otherwise
    consecutive_failures = 0
    while True:
        delay = next_interval(runtime, positioned=positioned, moment=utc_now())
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise

        readiness = runtime.reconciliation_readiness
        if readiness.snapshot().status.name == "RECONCILED":
            # Already warm — nothing to refresh, and spending a broker request to
            # re-prove a live proof is budget taken from the cancel path.
            continue

        succeeded, positioned = await asyncio.to_thread(reconcile_once, runtime)
        if succeeded:
            consecutive_failures = 0
            continue
        consecutive_failures += 1
        if consecutive_failures == _FAILURES_BEFORE_ALERT:
            _logger.error(
                "Periodic reconciliation has failed %d times in a row; submission "
                "readiness will expire and opening orders will be refused",
                consecutive_failures,
                extra={
                    "event": "periodic_reconciliation_degraded",
                    "consecutive_failures": consecutive_failures,
                    "outcome": "locked",
                },
            )


def evidence_age(runtime: AppRuntime) -> timedelta:
    """The owner-frozen maximum evidence age, as a timedelta (ADR-0020 §2)."""

    return timedelta(seconds=runtime.settings.reconciliation_max_evidence_age_seconds)
