"""Backend runtime ownership and FastAPI dependency accessors.

The backend process owns exactly one :class:`~chronos.runtime.AppRuntime`
(the only broker connection and, from Milestone 5 on, the only order-writing
authority). It is created during application lifespan startup together with
the single-writer lease; when the lease is held by another process the app
starts in **read-only** mode: inspection endpoints work, every mutating
endpoint refuses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import HTTPException, Request, status

from chronos.api.task_observations import TaskObservationRegistry
from chronos.operations.clock import ClockHealthCache
from chronos.operations.health import StartupFaultCode
from chronos.orders.recovery_hold import RecoveryHold
from chronos.runtime import AppRuntime
from chronos.utils.locking import WriterLease


@dataclass(slots=True)
class BackendState:
    runtime: AppRuntime
    lease: WriterLease | None
    read_only: bool
    task_observations: TaskObservationRegistry = field(default_factory=TaskObservationRegistry)
    clock_health: ClockHealthCache = field(default_factory=ClockHealthCache)
    startup_faults: tuple[StartupFaultCode, ...] = ()
    #: Set at writer startup when this boot cannot prove it does not follow a
    #: restore (ADR-0054). It is deliberately NOT folded into ``read_only``:
    #: that flag means "another process holds the lease", and the heartbeat and
    #: the startup-abort path both branch on it, so overloading it would make
    #: this process stop renewing a lease it still holds.
    recovery_hold: RecoveryHold | None = None
    #: The process's single bar provider, created on first use by
    #: ``chronos.api.bars.provider_for`` and cached here so a panel refresh does
    #: not become a broker request.
    #:
    #: **It is a declared field because this class is ``slots=True``.** Until
    #: 2026-08-14 ``provider_for`` cached by *assigning a new attribute* to this
    #: object, which a slotted dataclass refuses — so every call raised
    #: ``AttributeError`` and ``GET /terminal/bars`` answered 500 for every
    #: symbol, on every backend, since the route existed. No test covered that
    #: route, which is why it survived; ADR-0028's issuance handler composes the
    #: same bars and is what surfaced it. Typed ``object | None`` rather than
    #: ``BarProvider | None`` only to keep this module free of the bars import —
    #: ``provider_for`` re-checks the concrete type before returning it.
    bar_provider: object | None = None

    @property
    def writer(self) -> bool:
        return not self.read_only and self.lease is not None

    @property
    def may_write(self) -> bool:
        """Holds the lease **and** has no unacknowledged recovery hold (ADR-0054).

        The one predicate both write gates ask, so the route layer and the
        submission boundary cannot drift apart: ``require_writer`` refuses on it,
        and the lifespan's lease verifier -- which the autonomy handoff reaches
        without passing through any route -- is built from it.
        """

        return self.writer and self.recovery_hold is None

    def note_startup_fault(self, fault: StartupFaultCode) -> None:
        """Retain a closed, non-secret startup outcome for later health reads."""

        self.startup_faults = tuple(
            sorted({*self.startup_faults, fault}, key=lambda item: item.value)
        )


def get_state(request: Request) -> BackendState:
    state: BackendState | None = getattr(request.app.state, "backend", None)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backend runtime is not initialized",
        )
    return state


def require_lease_holder(request: Request) -> BackendState:
    """The lease-holding writer, tolerating a recovery hold.

    Exactly one endpoint needs this rather than :func:`require_writer`: the
    recovery acknowledgement itself, which must stay reachable *while* a hold is
    in force or the hold would have no escape. It still requires the lease,
    because acknowledging writes durable state.
    """

    state = get_state(request)
    if not state.writer:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This backend is running read-only: another Chronos backend holds the "
                "single-writer lease for this database. Order submission and modification "
                "are disabled here."
            ),
        )
    return state


def require_writer(request: Request) -> BackendState:
    """Dependency for mutating endpoints: refuse in read-only mode.

    Also refuses under a recovery hold (ADR-0054), which is why the two
    deliberately lease-free endpoints in ``routes/live.py`` -- disarm and engage
    kill -- keep using ``get_state``: both only ever *remove* authority, and a
    restore is not a reason to make the emergency stop unreachable. Disengaging
    the kill switch is writer-gated and therefore held, which is the intended
    order: acknowledge the restore first, then decide about the stop.
    """

    state = require_lease_holder(request)
    if state.recovery_hold is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This backend booted under a recovery hold and is running read-only: "
                f"{state.recovery_hold.detail}. Acknowledge it with a note at "
                "POST /live/recovery/acknowledge and restart."
            ),
        )
    return state
