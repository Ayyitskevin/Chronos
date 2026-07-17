"""Backend runtime ownership and FastAPI dependency accessors.

The backend process owns exactly one :class:`~chronos.runtime.AppRuntime`
(the only broker connection and, from Milestone 5 on, the only order-writing
authority). It is created during application lifespan startup together with
the single-writer lease; when the lease is held by another process the app
starts in **read-only** mode: inspection endpoints work, every mutating
endpoint refuses.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from chronos.runtime import AppRuntime
from chronos.utils.locking import WriterLease


@dataclass(slots=True)
class BackendState:
    runtime: AppRuntime
    lease: WriterLease | None
    read_only: bool

    @property
    def writer(self) -> bool:
        return not self.read_only and self.lease is not None


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
