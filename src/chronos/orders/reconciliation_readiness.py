"""Process-lifetime, fail-closed reconciliation readiness.

Broker and local state are authorization evidence for an order submission, not
startup decoration.  This latch therefore starts ``PENDING`` and can become
``RECONCILED`` only for the same connection generation that produced a complete
reconciliation report.  Any connection uncertainty advances the generation and
invalidates older evidence.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
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

    def __init__(
        self,
        *,
        session_id: str | None = None,
        max_evidence_age: timedelta | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """``max_evidence_age`` bounds how long a published proof may authorize.

        ADR-0020. ``None`` keeps the pre-2026-08-02 behaviour — a published proof
        never expires on its own — and remains the default so that constructing a
        latch in a test or a script does not silently acquire a clock. Production
        wiring passes the owner-frozen age.

        Expiry is evaluated in :meth:`snapshot`, deliberately, rather than by the
        task that refreshes it: a proof must stop being trusted **whether or not**
        the refresher is alive, and correctness that depends on the health of the
        component it guards is not correctness. Two consecutive missed refresh
        cycles therefore fail closed by arithmetic alone.

        The same rule subsumes ADR-0020 §4: a proof from before a regular-session
        open cannot survive a 300-second age, so readiness never crosses the open
        and no separate session-boundary mechanism is required.
        """

        normalized_session_id = (session_id or uuid.uuid4().hex).strip()
        if not normalized_session_id:
            raise ValueError("reconciliation session_id must not be blank")
        if max_evidence_age is not None and max_evidence_age <= timedelta(0):
            raise ValueError("max_evidence_age must be positive when set")
        self._max_evidence_age = max_evidence_age
        self._clock = clock
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
            self._expire_stale_evidence_unlocked()
            return self._snapshot_unlocked()

    def _expire_stale_evidence_unlocked(self) -> None:
        """Demote a proof that has outlived the owner-frozen evidence age.

        A no-op unless the latch was given both an age and a clock, and unless a
        ``RECONCILED`` proof is actually in force. A submission in flight is left
        alone: it already claimed this proof and ``submission_guard`` has set the
        status to ``PENDING`` for everyone else, so there is nothing here to
        demote and taking it away mid-send would invalidate a claim the sender is
        entitled to finish.
        """

        if self._max_evidence_age is None or self._clock is None:
            return
        if self._status is not ReconciliationStatus.RECONCILED:
            return
        if self._reconciled_at is None or self._submissions_in_flight:
            return
        age = self._clock() - self._reconciled_at
        if age <= self._max_evidence_age:
            return
        self._status = ReconciliationStatus.PENDING
        self._reason = (
            f"reconciliation evidence is {int(age.total_seconds())}s old, past the "
            f"{int(self._max_evidence_age.total_seconds())}s maximum evidence age"
        )
        self._reconciled_at = None

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
