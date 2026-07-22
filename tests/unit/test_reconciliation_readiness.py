from datetime import UTC, datetime
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
