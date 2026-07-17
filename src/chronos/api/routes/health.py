"""Unauthenticated liveness/status endpoint (never exposes account data)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    broker_mode: str
    environment: str
    read_only: bool
    writer_lease_held: bool


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    backend = getattr(request.app.state, "backend", None)
    if backend is None:
        return HealthResponse(
            status="starting",
            broker_mode="unknown",
            environment="unknown",
            read_only=True,
            writer_lease_held=False,
        )
    settings = backend.runtime.settings
    return HealthResponse(
        status="ok",
        broker_mode=settings.broker_mode.value,
        environment=settings.ib_environment.value,
        read_only=backend.read_only,
        writer_lease_held=backend.writer,
    )
