from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from time import monotonic, sleep

import pytest

from chronos.domain.enums import ReconciliationStatus
from chronos.orders.reconciliation_readiness import ReconciliationReadiness

NOW = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)


def test_readiness_defaults_pending_without_implicit_proof() -> None:
    readiness = ReconciliationReadiness()

    snapshot = readiness.snapshot()

    assert snapshot.status is ReconciliationStatus.PENDING
    assert snapshot.generation == 0
    assert snapshot.reconciled_at is None
    assert readiness.is_reconciled() is False


def test_only_current_generation_can_publish_reconciled() -> None:
    readiness = ReconciliationReadiness()
    stale_generation = readiness.begin_reconciliation("initial observation")
    current = readiness.invalidate("connection changed during observation")

    published = readiness.complete(
        expected_generation=stale_generation,
        status=ReconciliationStatus.RECONCILED,
        reason="complete broker and local parity",
        reconciled_at=NOW,
    )

    assert published is False
    assert readiness.snapshot() == current
    assert readiness.is_reconciled() is False


def test_invalidation_clears_a_prior_reconciliation() -> None:
    readiness = ReconciliationReadiness()
    generation = readiness.begin_reconciliation("startup observation")
    assert readiness.complete(
        expected_generation=generation,
        status=ReconciliationStatus.RECONCILED,
        reason="complete broker and local parity",
        reconciled_at=NOW,
    )
    assert readiness.is_reconciled(expected_generation=generation)

    blocked = readiness.invalidate("broker disconnected")

    assert blocked.status is ReconciliationStatus.PENDING
    assert blocked.generation == generation + 1
    assert blocked.reconciled_at is None
    assert readiness.is_reconciled(expected_generation=generation) is False


def test_manual_review_is_explicitly_blocked() -> None:
    readiness = ReconciliationReadiness()
    generation = readiness.begin_reconciliation("startup observation")

    assert readiness.complete(
        expected_generation=generation,
        status=ReconciliationStatus.MANUAL_REVIEW,
        reason="unexplained broker position",
    )
    snapshot = readiness.snapshot()

    assert snapshot.status is ReconciliationStatus.MANUAL_REVIEW
    assert snapshot.ready is False


def test_reconciled_completion_requires_aware_time() -> None:
    readiness = ReconciliationReadiness()
    generation = readiness.begin_reconciliation("startup observation")

    with pytest.raises(ValueError, match="aware completion time"):
        readiness.complete(
            expected_generation=generation,
            status=ReconciliationStatus.RECONCILED,
            reason="complete broker and local parity",
            reconciled_at=NOW.replace(tzinfo=None),
        )


def test_reconciliation_waits_for_an_authorized_submission_to_settle() -> None:
    readiness = ReconciliationReadiness()
    generation = readiness.begin_reconciliation("initial observation")
    assert readiness.complete(
        expected_generation=generation,
        status=ReconciliationStatus.RECONCILED,
        reason="complete broker and local parity",
        reconciled_at=NOW,
    )
    started = Event()
    finished = Event()
    observed_generations: list[int] = []

    def run_reconciliation() -> None:
        started.set()
        observed_generations.append(
            readiness.begin_reconciliation("operator requested fresh evidence")
        )
        finished.set()

    with readiness.submission_guard(expected_generation=generation) as acquired:
        assert acquired is True
        thread = Thread(target=run_reconciliation)
        thread.start()
        assert started.wait(timeout=1)
        deadline = monotonic() + 1
        while readiness.snapshot().generation == generation and monotonic() < deadline:
            sleep(0.001)
        assert readiness.snapshot().status is ReconciliationStatus.PENDING
        assert finished.is_set() is False

    assert finished.wait(timeout=1)
    thread.join(timeout=1)
    assert thread.is_alive() is False
    assert observed_generations == [generation + 1]


def test_submission_claim_consumes_one_reconciled_generation() -> None:
    readiness = ReconciliationReadiness()
    generation = readiness.begin_reconciliation("initial observation")
    assert readiness.complete(
        expected_generation=generation,
        status=ReconciliationStatus.RECONCILED,
        reason="complete broker and local parity",
        reconciled_at=NOW,
    )

    with readiness.submission_guard(expected_generation=generation) as acquired:
        assert acquired is True
        with readiness.submission_guard(expected_generation=generation) as duplicate:
            assert duplicate is False

    consumed = readiness.snapshot()
    assert consumed.status is ReconciliationStatus.PENDING
    assert consumed.ready is False

    with readiness.submission_guard(expected_generation=generation) as later:
        assert later is False


def test_invalidation_linearizes_after_an_atomic_send_boundary() -> None:
    readiness = ReconciliationReadiness()
    generation = readiness.begin_reconciliation("initial observation")
    assert readiness.complete(
        expected_generation=generation,
        status=ReconciliationStatus.RECONCILED,
        reason="complete broker and local parity",
        reconciled_at=NOW,
    )
    invalidation_started = Event()
    invalidation_finished = Event()

    def invalidate() -> None:
        invalidation_started.set()
        readiness.invalidate("connection changed during adapter send")
        invalidation_finished.set()

    with readiness.submission_guard(expected_generation=generation) as acquired:
        assert acquired is True
        with readiness.at_send() as authorized:
            assert authorized is True
            thread = Thread(target=invalidate)
            thread.start()
            assert invalidation_started.wait(timeout=1)
            assert invalidation_finished.is_set() is False
        assert invalidation_finished.wait(timeout=1)

    thread.join(timeout=1)
    assert thread.is_alive() is False
    snapshot = readiness.snapshot()
    assert snapshot.status is ReconciliationStatus.PENDING
    assert snapshot.generation == generation + 1


def test_process_sessions_do_not_share_generation_identity() -> None:
    first = ReconciliationReadiness(session_id="process-a")
    second = ReconciliationReadiness(session_id="process-b")

    assert first.snapshot().generation == second.snapshot().generation


# ---------------------------------------------------- maximum evidence age (ADR-0020)
#
# The age is evaluated in `snapshot()` rather than by the task that refreshes it:
# a proof must stop being trusted whether or not the refresher is alive. These
# tests drive a fake clock directly, so they prove the demotion happens with no
# loop running at all — which is the property the design depends on.


def _aged_latch(clock_holder: list[datetime], age_seconds: int = 300):
    return ReconciliationReadiness(
        max_evidence_age=timedelta(seconds=age_seconds), clock=lambda: clock_holder[0]
    )


def test_evidence_inside_the_age_still_authorizes() -> None:
    now = [NOW]
    latch = _aged_latch(now)
    generation = latch.begin_reconciliation("startup")
    assert latch.complete(
        expected_generation=generation,
        status=ReconciliationStatus.RECONCILED,
        reason="reconciled",
        reconciled_at=NOW,
    )
    now[0] = NOW + timedelta(seconds=299)
    assert latch.snapshot().ready is True


def test_evidence_past_the_age_reads_pending_with_no_loop_running() -> None:
    """The whole point: expiry does not depend on the refresher being alive."""

    now = [NOW]
    latch = _aged_latch(now)
    generation = latch.begin_reconciliation("startup")
    latch.complete(
        expected_generation=generation,
        status=ReconciliationStatus.RECONCILED,
        reason="reconciled",
        reconciled_at=NOW,
    )
    now[0] = NOW + timedelta(seconds=301)
    snapshot = latch.snapshot()
    assert snapshot.ready is False
    assert snapshot.status is ReconciliationStatus.PENDING
    assert "maximum evidence age" in snapshot.reason
    assert snapshot.reconciled_at is None


def test_two_missed_active_cycles_fail_closed_by_arithmetic() -> None:
    """120s cadence: one miss survivable (240s), two are not (360s > 300s)."""

    now = [NOW]
    latch = _aged_latch(now)
    generation = latch.begin_reconciliation("startup")
    latch.complete(
        expected_generation=generation,
        status=ReconciliationStatus.RECONCILED,
        reason="reconciled",
        reconciled_at=NOW,
    )
    now[0] = NOW + timedelta(seconds=240)
    assert latch.snapshot().ready is True, "one missed cycle must still authorize"
    now[0] = NOW + timedelta(seconds=360)
    assert latch.snapshot().ready is False, "two missed cycles must fail closed"


def test_one_missed_idle_cycle_fails_closed() -> None:
    """240s cadence tolerates zero misses (480s > 300s) — stricter, and correct.

    A flat book has no position to protect, so blocking early costs nothing. This
    asymmetry with the active cadence is deliberate, not an oversight.
    """

    now = [NOW]
    latch = _aged_latch(now)
    generation = latch.begin_reconciliation("startup")
    latch.complete(
        expected_generation=generation,
        status=ReconciliationStatus.RECONCILED,
        reason="reconciled",
        reconciled_at=NOW,
    )
    now[0] = NOW + timedelta(seconds=480)
    assert latch.snapshot().ready is False


def test_readiness_cannot_cross_a_session_open() -> None:
    """ADR-0020 §4 needs no separate mechanism — the age already enforces it.

    Overnight assignment is the event most likely to make the book differ from
    what the system believes. A proof from before the open is hours old, so it is
    stale by construction and the first order of the session must re-reconcile.
    """

    now = [NOW]
    latch = _aged_latch(now)
    generation = latch.begin_reconciliation("startup")
    latch.complete(
        expected_generation=generation,
        status=ReconciliationStatus.RECONCILED,
        reason="reconciled before the close",
        reconciled_at=NOW,
    )
    now[0] = NOW + timedelta(hours=18)
    assert latch.snapshot().ready is False


def test_an_in_flight_submission_keeps_its_claim_when_evidence_ages_out() -> None:
    """A sender that already claimed readiness is entitled to finish.

    `submission_guard` has already set the status to PENDING for everyone else,
    so there is nothing left to demote, and taking the claim away mid-send would
    invalidate an authorization the sender legitimately holds.
    """

    now = [NOW]
    latch = _aged_latch(now)
    generation = latch.begin_reconciliation("startup")
    latch.complete(
        expected_generation=generation,
        status=ReconciliationStatus.RECONCILED,
        reason="reconciled",
        reconciled_at=NOW,
    )
    with latch.submission_guard(expected_generation=generation) as acquired:
        assert acquired
        now[0] = NOW + timedelta(seconds=3600)
        assert latch.submission_claim_is_current(expected_generation=generation) is True


def test_the_age_is_opt_in_so_a_bare_latch_never_expires() -> None:
    """Default construction keeps the pre-2026-08-02 behaviour, deliberately."""

    latch = ReconciliationReadiness()
    generation = latch.begin_reconciliation("startup")
    latch.complete(
        expected_generation=generation,
        status=ReconciliationStatus.RECONCILED,
        reason="reconciled",
        reconciled_at=NOW,
    )
    assert latch.snapshot().ready is True


def test_a_non_positive_age_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="max_evidence_age must be positive"):
        ReconciliationReadiness(max_evidence_age=timedelta(0), clock=lambda: NOW)
