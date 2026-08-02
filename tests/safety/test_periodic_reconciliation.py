"""ADR-0020: the bounded periodic reconciliation task.

Exercised tests, not presence tests: each one drives the decision function or the
loop and asserts the behaviour fires, because the four defects this repository
was burned by (R-24..R-27) were all fully wired, documented, and covered by
passing tests while being structurally unable to act.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from chronos.api.reconciliation_loop import (
    evidence_age,
    market_session_is_open,
    next_interval,
    reconcile_once,
)
from chronos.config.settings import Settings

# A Wednesday, so weekday logic is not accidentally satisfied by the date.
WEDNESDAY_MIDDAY_UTC = datetime(2026, 7, 22, 16, 0, tzinfo=UTC)  # 12:00 New York
WEDNESDAY_NIGHT_UTC = datetime(2026, 7, 22, 23, 0, tzinfo=UTC)  # 19:00 New York
SATURDAY_MIDDAY_UTC = datetime(2026, 7, 25, 16, 0, tzinfo=UTC)  # 12:00 New York


@dataclass
class _Restart:
    remaining_active: tuple[object, ...] = ()


@dataclass
class _Readiness:
    status: Any = None


@dataclass
class _Report:
    restart: _Restart
    readiness: _Readiness


class _Runtime:
    """The narrow surface the loop actually touches."""

    def __init__(self, *, report: _Report | None = None, error: Exception | None = None) -> None:
        self.settings = Settings()
        self._report = report
        self._error = error
        self.calls = 0

    def reconcile_submission_readiness(self) -> _Report:
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._report is not None
        return self._report


class _Status:
    def __init__(self, name: str) -> None:
        self.name = name
        self.value = name.lower()


def _report(*, positioned: bool, status: str = "RECONCILED") -> _Report:
    return _Report(
        restart=_Restart(remaining_active=(object(),) if positioned else ()),
        readiness=_Readiness(status=_Status(status)),
    )


# --------------------------------------------------------------- session approximation


def test_the_session_window_is_recognised_inside_rth() -> None:
    assert market_session_is_open(WEDNESDAY_MIDDAY_UTC, "America/New_York") is True


def test_after_the_close_is_not_the_session() -> None:
    assert market_session_is_open(WEDNESDAY_NIGHT_UTC, "America/New_York") is False


def test_the_weekend_is_not_the_session() -> None:
    assert market_session_is_open(SATURDAY_MIDDAY_UTC, "America/New_York") is False


def test_an_unusable_timezone_assumes_open() -> None:
    """Wrong here costs requests, never an authorization — so bias toward looking.

    This is the opposite of what the session GATE does (R-26 fails closed to
    AMBIGUOUS), and the asymmetry is deliberate: that gate decides whether an
    order may pass, this one decides how often to poll.
    """

    assert market_session_is_open(WEDNESDAY_MIDDAY_UTC, "Not/AZone") is True


# ------------------------------------------------------------------- interval choice


def test_positioned_in_session_uses_the_active_interval() -> None:
    runtime = _Runtime()
    chosen = next_interval(runtime, positioned=True, moment=WEDNESDAY_MIDDAY_UTC)
    assert chosen == runtime.settings.reconciliation_interval_active_seconds == 120.0


def test_flat_in_session_uses_the_idle_interval() -> None:
    runtime = _Runtime()
    chosen = next_interval(runtime, positioned=False, moment=WEDNESDAY_MIDDAY_UTC)
    assert chosen == runtime.settings.reconciliation_interval_idle_seconds == 240.0


def test_a_closed_market_uses_the_closed_interval_even_when_positioned() -> None:
    """The closed interval is reachable, not an inert setting.

    AGENTS.md treats an inert threshold as a release blocker, so this asserts the
    frozen 1800s value is actually selected rather than merely configured.
    """

    runtime = _Runtime()
    chosen = next_interval(runtime, positioned=True, moment=WEDNESDAY_NIGHT_UTC)
    assert chosen == runtime.settings.reconciliation_interval_closed_seconds == 1800.0


def test_every_frozen_interval_is_reachable_from_some_state() -> None:
    runtime = _Runtime()
    reachable = {
        next_interval(runtime, positioned=True, moment=WEDNESDAY_MIDDAY_UTC),
        next_interval(runtime, positioned=False, moment=WEDNESDAY_MIDDAY_UTC),
        next_interval(runtime, positioned=True, moment=WEDNESDAY_NIGHT_UTC),
    }
    assert reachable == {120.0, 240.0, 1800.0}


def test_the_evidence_age_matches_the_frozen_setting() -> None:
    runtime = _Runtime()
    assert evidence_age(runtime).total_seconds() == 300.0


# ----------------------------------------------------------------------- one cycle


def test_a_successful_cycle_reports_the_positioned_state_it_observed() -> None:
    runtime = _Runtime(report=_report(positioned=True))
    assert reconcile_once(runtime) == (True, True)
    assert runtime.calls == 1

    flat = _Runtime(report=_report(positioned=False))
    assert reconcile_once(flat) == (True, False)


def test_a_failed_cycle_does_not_raise_and_assumes_the_shorter_cadence() -> None:
    """A failure means we do not know whether the book is flat.

    The state we cannot see is the one worth looking at sooner, and the task must
    survive the error — a refresher that dies stops renewing readiness silently.
    """

    runtime = _Runtime(error=RuntimeError("broker unreachable"))
    succeeded, positioned = reconcile_once(runtime)
    assert succeeded is False
    assert positioned is True


def test_a_failed_cycle_never_publishes_readiness() -> None:
    """Failure leaves the latch alone so its own age expires it (ADR-0020 §3)."""

    runtime = _Runtime(error=RuntimeError("broker unreachable"))
    reconcile_once(runtime)
    assert runtime.calls == 1  # attempted once, published nothing


# -------------------------------------------------------------------------- the loop


def test_the_loop_skips_the_broker_entirely_while_readiness_is_still_warm() -> None:
    """Re-proving a live proof spends budget taken from the cancel path.

    R-42's lesson generalised: this task shares one connection with order
    submission, so a request it does not need is headroom it takes away.
    """

    from chronos.api import reconciliation_loop

    runtime = _Runtime(report=_report(positioned=False))

    class _WarmLatch:
        def snapshot(self) -> Any:
            return _Readiness(status=_Status("RECONCILED"))

    runtime.reconciliation_readiness = _WarmLatch()  # type: ignore[attr-defined]

    async def _run() -> None:
        task = asyncio.create_task(reconciliation_loop.reconciliation_task(runtime))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    assert runtime.calls == 0


def test_the_loop_stops_cleanly_on_cancellation() -> None:
    """The lifespan cancels it on shutdown; it must not swallow that."""

    from chronos.api import reconciliation_loop

    runtime = _Runtime(report=_report(positioned=False))

    class _PendingLatch:
        def snapshot(self) -> Any:
            return _Readiness(status=_Status("PENDING"))

    runtime.reconciliation_readiness = _PendingLatch()  # type: ignore[attr-defined]

    async def _run() -> None:
        task = asyncio.create_task(reconciliation_loop.reconciliation_task(runtime))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
