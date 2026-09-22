"""ADR-0020: the bounded periodic reconciliation task.

Exercised tests, not presence tests: each one drives the decision function or the
loop and asserts the behaviour fires, because the four defects this repository
was burned by (R-24..R-27) were all fully wired, documented, and covered by
passing tests while being structurally unable to act.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from chronos.api import reconciliation_loop
from chronos.api.reconciliation_loop import (
    evidence_age,
    market_session_is_open,
    next_interval,
    reconcile_once,
)
from chronos.config.settings import Settings
from chronos.domain.enums import ReconciliationStatus
from chronos.orders.reconciliation_readiness import ReconciliationReadiness

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

    def reconcile_submission_readiness(self, *, trigger: str = "unattributed") -> _Report:
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


class _ScriptedRuntime(_Runtime):
    """One outcome per call, in order: an exception instance raises, a report returns."""

    def __init__(self, outcomes: list[_Report | Exception]) -> None:
        super().__init__()
        self._outcomes = list(outcomes)

    def reconcile_submission_readiness(self, *, trigger: str = "unattributed") -> _Report:
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Latch:
    """A latch whose snapshot reports one fixed status name."""

    def __init__(self, status: str) -> None:
        self._status = status

    def snapshot(self) -> Any:
        return _Readiness(status=_Status(self._status))


class _FakeAsyncio:
    """Stand-in for the loop module's ``asyncio`` binding: no real sleeping, no wall clock.

    D-1 (2026-09-13 §2) found the loop tests cancelling the task during its FIRST
    ``asyncio.sleep(delay)`` — every branch after that sleep was unreachable, and a
    PENDING latch satisfied the "warm skip" test exactly as a RECONCILED one did. This
    namespace makes a delay a *crossing*: ``sleep`` records the requested delay, yields
    to the real event loop exactly once, and counts completion. On the call after the
    N-th crossing it cancels the running task instead, so the coroutine sees the same
    ``CancelledError`` the lifespan's shutdown delivers, at the same await.
    ``to_thread`` runs the callable inline; ``CancelledError`` is the real class, so the
    loop's ``except asyncio.CancelledError`` still matches what is thrown.
    """

    CancelledError = asyncio.CancelledError

    def __init__(self, *, crossings: int) -> None:
        self.crossings = crossings
        self.delays: list[float] = []
        self.completed = 0

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) > self.crossings:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
        await asyncio.sleep(0)  # the real yield: a pending cancel lands here
        self.completed += 1

    @staticmethod
    async def to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)


def _drive(
    runtime: _Runtime,
    *,
    crossings: int,
    moment: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> _FakeAsyncio:
    """Run the task through exactly ``crossings`` completed delays at a fixed moment.

    Returns the fake so the test can read the delays it was asked for. The task ends
    by cancellation on the delay AFTER the last crossing, so ``delays`` holds
    ``crossings + 1`` entries: the last one is the cadence chosen from the final cycle's
    evidence, which is the observation the cadence-transition tests need.
    """

    fake = _FakeAsyncio(crossings=crossings)
    monkeypatch.setattr(reconciliation_loop, "asyncio", fake)
    monkeypatch.setattr(reconciliation_loop, "utc_now", lambda: moment)

    async def _run() -> None:
        task = asyncio.create_task(reconciliation_loop.reconciliation_task(runtime))  # type: ignore[arg-type]
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    assert fake.completed == crossings, fake.delays
    assert len(fake.delays) == crossings + 1, fake.delays
    return fake


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
    latch = ReconciliationReadiness()  # the real latch, default PENDING, no clock
    runtime.reconciliation_readiness = latch  # type: ignore[attr-defined]

    assert reconcile_once(runtime) == (False, True)
    assert runtime.calls == 1  # attempted once
    # ...and published nothing: the real latch is still PENDING, not merely "not RECONCILED".
    assert latch.snapshot().status is ReconciliationStatus.PENDING
    assert latch.snapshot().ready is False


# -------------------------------------------------------------------------- the loop


def test_the_loop_skips_the_broker_entirely_while_readiness_is_still_warm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-proving a live proof spends budget taken from the cancel path.

    R-42's lesson generalised: this task shares one connection with order
    submission, so a request it does not need is headroom it takes away.
    """

    runtime = _Runtime(report=_report(positioned=False))
    runtime.reconciliation_readiness = _Latch("RECONCILED")  # type: ignore[attr-defined]

    fake = _drive(runtime, crossings=1, moment=WEDNESDAY_MIDDAY_UTC, monkeypatch=monkeypatch)

    # The delay was crossed BEFORE the assertion below: the task asked for the active
    # interval (positioned is assumed until evidence says otherwise), that sleep completed,
    # and the latch was then inspected. calls == 0 therefore means the skip branch ran —
    # not that the task was cancelled while still asleep (D-1 §2).
    settings = runtime.settings
    assert fake.completed == 1
    assert fake.delays[0] == settings.reconciliation_interval_active_seconds
    assert runtime.calls == 0
    # No evidence arrived, so the assumed cadence is kept for the next delay too.
    assert fake.delays == [settings.reconciliation_interval_active_seconds] * 2


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


@pytest.mark.parametrize("crossings", [1, 2])
def test_a_pending_latch_reconciles_once_per_crossing(
    crossings: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each completed delay with a PENDING latch costs exactly one broker call.

    This is the positive control for the warm-skip test above: the same driver, the
    same first delay, and the only difference is the latch — so a skip that "passes"
    because nothing ran cannot pass here.
    """

    runtime = _Runtime(report=_report(positioned=True))
    runtime.reconciliation_readiness = _Latch("PENDING")  # type: ignore[attr-defined]

    fake = _drive(
        runtime, crossings=crossings, moment=WEDNESDAY_MIDDAY_UTC, monkeypatch=monkeypatch
    )

    assert fake.completed == crossings
    assert runtime.calls == crossings


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if getattr(record, "event", None) == name]


@contextmanager
def _captured(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Deliver this module's records to caplog whatever the session did to logging.

    ``configure_logging`` sets ``chronos.propagate = False`` (utils/logging.py), so once
    any earlier test has called it, caplog's root handler never sees a chronos record and
    every ``_events`` count reads 0 — green alone, red in the full gate (found by the
    pre-commit gate on 2026-09-13). caplog's handler is attached to the module logger
    itself for the duration, with propagation off so an unconfigured session does not
    deliver the same record twice.
    """

    logger = logging.getLogger(reconciliation_loop.__name__)
    previous = logger.propagate
    logger.addHandler(caplog.handler)
    logger.propagate = False
    try:
        with caplog.at_level(logging.DEBUG, logger=reconciliation_loop.__name__):
            yield
    finally:
        logger.propagate = previous
        logger.removeHandler(caplog.handler)


def test_two_consecutive_failures_alert_exactly_once_at_the_second(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The degraded alert fires on the second failure in a row, and only then.

    The loop keeps running either way; what the operator is owed is the difference
    between "blocked because the market is shut" and "blocked because reconciliation
    has been broken for two cycles" — one alert, at the threshold, not one per cycle.
    Three failing crossings are driven, not two, so the third proves the alert does
    not fire AGAIN once the streak passes the threshold: ``==`` at the threshold, not
    ``>=`` — a loop that alerted every cycle from the second on would be a pager, not
    a threshold.
    """

    runtime = _Runtime(error=RuntimeError("broker unreachable"))
    runtime.reconciliation_readiness = _Latch("PENDING")  # type: ignore[attr-defined]

    with _captured(caplog):
        _drive(runtime, crossings=3, moment=WEDNESDAY_MIDDAY_UTC, monkeypatch=monkeypatch)

    assert runtime.calls == 3
    assert len(_events(caplog, "periodic_reconciliation_failed")) == 3
    degraded = _events(caplog, "periodic_reconciliation_degraded")
    assert len(degraded) == 1, [record.consecutive_failures for record in degraded]  # type: ignore[attr-defined]
    assert degraded[0].consecutive_failures == 2  # type: ignore[attr-defined]
    assert degraded[0].levelno == logging.ERROR


def test_a_success_between_two_failures_resets_the_alert_counter(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fail, succeed, fail — never two in a row, so the alert must not fire."""

    runtime = _ScriptedRuntime(
        [
            RuntimeError("broker unreachable"),
            _report(positioned=True),
            RuntimeError("broker unreachable again"),
        ]
    )
    runtime.reconciliation_readiness = _Latch("PENDING")  # type: ignore[attr-defined]

    with _captured(caplog):
        _drive(runtime, crossings=3, moment=WEDNESDAY_MIDDAY_UTC, monkeypatch=monkeypatch)

    assert runtime.calls == 3
    assert len(_events(caplog, "periodic_reconciliation_failed")) == 2
    assert len(_events(caplog, "periodic_reconciliation_finished")) == 1
    assert _events(caplog, "periodic_reconciliation_degraded") == []


def test_the_cadence_moves_from_active_to_idle_after_a_flat_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In session: the first delay assumes positioned; a flat report moves it to idle.

    The values are read from the runtime's settings, never written here — they are
    frozen elsewhere (ADR-0020 §2) and this test proves the running task selects
    them, not what they are.
    """

    runtime = _Runtime(report=_report(positioned=False))
    runtime.reconciliation_readiness = _Latch("PENDING")  # type: ignore[attr-defined]

    fake = _drive(runtime, crossings=1, moment=WEDNESDAY_MIDDAY_UTC, monkeypatch=monkeypatch)

    settings = runtime.settings
    assert runtime.calls == 1
    assert fake.delays == [
        settings.reconciliation_interval_active_seconds,
        settings.reconciliation_interval_idle_seconds,
    ]


@pytest.mark.parametrize("positioned", [True, False])
def test_a_closed_market_holds_the_closed_cadence_regardless_of_position(
    positioned: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _Runtime(report=_report(positioned=positioned))
    runtime.reconciliation_readiness = _Latch("PENDING")  # type: ignore[attr-defined]

    fake = _drive(runtime, crossings=1, moment=WEDNESDAY_NIGHT_UTC, monkeypatch=monkeypatch)

    settings = runtime.settings
    assert runtime.calls == 1
    assert fake.delays == [settings.reconciliation_interval_closed_seconds] * 2
