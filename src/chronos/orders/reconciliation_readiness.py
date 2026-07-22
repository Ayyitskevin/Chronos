"""Process-lifetime, fail-closed reconciliation readiness.

Broker and local state are authorization evidence for an order submission, not
startup decoration.  This latch therefore starts ``PENDING`` and can become
``RECONCILED`` only for the same connection generation that produced a complete
reconciliation report.  Any connection uncertainty advances the generation and
invalidates older evidence.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from threading import Condition, Lock

from chronos.domain.enums import ReconciliationStatus


@dataclass(frozen=True, slots=True)
class ReconciliationReadinessSnapshot:
    """One atomic view of the current submission-readiness proof."""

    status: ReconciliationStatus
    session_id: str
    generation: int
    reason: str
    reconciled_at: datetime | None = None

    @property
    def ready(self) -> bool:
        return self.status is ReconciliationStatus.RECONCILED


class ReconciliationReadiness:
    """Thread-safe readiness latch bound to one broker connection generation."""

    def __init__(self, *, session_id: str | None = None) -> None:
        normalized_session_id = (session_id or uuid.uuid4().hex).strip()
        if not normalized_session_id:
            raise ValueError("reconciliation session_id must not be blank")
        self._lock = Lock()
        self._session_id = normalized_session_id
        self._idle = Condition(self._lock)
        self._submissions_in_flight = 0
        self._active_submission_generation: int | None = None
        self._send_started = False
        self._reconciliation_lock = Lock()
        self._status = ReconciliationStatus.PENDING
        self._generation = 0
        self._reason = "startup reconciliation has not completed"
        self._reconciled_at: datetime | None = None

    def snapshot(self) -> ReconciliationReadinessSnapshot:
        with self._lock:
            return self._snapshot_unlocked()

    @contextmanager
    def reconciliation_session(self, reason: str) -> Iterator[int]:
        """Serialize one complete reconciliation report and readiness publication."""

        with self._reconciliation_lock:
            yield self.begin_reconciliation(reason)

    def begin_reconciliation(self, reason: str) -> int:
        """Invalidate evidence, then wait for already-authorized submits to settle."""

        normalized = reason.strip() or "reconciliation observation in progress"
        with self._idle:
            self._invalidate_unlocked(normalized)
            while self._submissions_in_flight:
                self._idle.wait()
            return self._generation

    def invalidate(self, reason: str) -> ReconciliationReadinessSnapshot:
        """Advance the generation and fail closed with a non-secret reason."""

        normalized = reason.strip() or "broker state became uncertain"
        with self._lock:
            self._invalidate_unlocked(normalized)
            return self._snapshot_unlocked()

    @contextmanager
    def invalidating_transition(self, reason: str) -> Iterator[None]:
        """Keep readiness locked while connection scope state is replaced."""

        normalized = reason.strip() or "broker state became uncertain"
        with self._lock:
            self._invalidate_unlocked(normalized)
            yield

    def _invalidate_unlocked(self, reason: str) -> None:
        self._generation += 1
        self._status = ReconciliationStatus.PENDING
        self._reason = reason
        self._reconciled_at = None

    def complete(
        self,
        *,
        expected_generation: int,
        status: ReconciliationStatus,
        reason: str,
        reconciled_at: datetime | None = None,
    ) -> bool:
        """Publish a result only if no lifecycle event raced the observation.

        A late successful result can never re-arm a newer connection generation.
        ``reconciled_at`` is accepted only for a proven ``RECONCILED`` result.
        """

        normalized = reason.strip() or "reconciliation did not provide a reason"
        if status is ReconciliationStatus.RECONCILED:
            if reconciled_at is None or reconciled_at.tzinfo is None:
                raise ValueError("reconciled readiness requires an aware completion time")
        elif reconciled_at is not None:
            raise ValueError("blocked readiness must not carry a reconciled timestamp")

        with self._lock:
            if expected_generation != self._generation or self._submissions_in_flight:
                return False
            self._status = status
            self._reason = normalized
            self._reconciled_at = reconciled_at
            return True

    @contextmanager
    def submission_guard(self, *, expected_generation: int) -> Iterator[bool]:
        """Exclusively claim and consume readiness for one opening-order submit."""

        acquired = False
        with self._lock:
            if (
                expected_generation == self._generation
                and self._status is ReconciliationStatus.RECONCILED
                and self._submissions_in_flight == 0
            ):
                self._submissions_in_flight += 1
                self._active_submission_generation = expected_generation
                self._send_started = False
                # A proven empty/parity snapshot authorizes at most one opening
                # submission. The claimant may finish, but every later opening
                # requires fresh broker/local reconciliation.
                self._status = ReconciliationStatus.PENDING
                self._reason = "reconciliation consumed by an opening-order submission"
                self._reconciled_at = None
                acquired = True
        try:
            yield acquired
        finally:
            if acquired:
                with self._idle:
                    self._submissions_in_flight -= 1
                    self._active_submission_generation = None
                    self._send_started = False
                    self._idle.notify_all()

    def submission_claim_is_current(self, *, expected_generation: int) -> bool:
        """Return whether the exclusive claimant survived lifecycle invalidation."""

        with self._lock:
            return (
                expected_generation == self._generation
                and self._active_submission_generation == expected_generation
                and self._submissions_in_flight == 1
            )

    @contextmanager
    def at_send(self) -> Iterator[bool]:
        """Linearize invalidation against the adapter's synchronous send call."""

        with self._lock:
            authorized = (
                self._submissions_in_flight == 1
                and self._active_submission_generation == self._generation
                and not self._send_started
            )
            if authorized:
                self._send_started = True
            yield authorized

    def is_reconciled(self, *, expected_generation: int | None = None) -> bool:
        with self._lock:
            generation_matches = (
                expected_generation is None or expected_generation == self._generation
            )
            return generation_matches and self._status is ReconciliationStatus.RECONCILED

    def _snapshot_unlocked(self) -> ReconciliationReadinessSnapshot:
        return ReconciliationReadinessSnapshot(
            status=self._status,
            generation=self._generation,
            session_id=self._session_id,
            reason=self._reason,
            reconciled_at=self._reconciled_at,
        )
