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
from chronos.operations.health import StartupFaultCode
from chronos.runtime import AppRuntime
from chronos.utils.locking import WriterLease


@dataclass(slots=True)
class BackendState:
    runtime: AppRuntime
    lease: WriterLease | None
    read_only: bool
    task_observations: TaskObservationRegistry = field(default_factory=TaskObservationRegistry)
    startup_faults: tuple[StartupFaultCode, ...] = ()
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


def require_writer(request: Request) -> BackendState:
    """Dependency for mutating endpoints: refuse in read-only mode."""

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
