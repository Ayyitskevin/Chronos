"""Unauthenticated liveness/status endpoint (never exposes account data)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import AwareDatetime, BaseModel

from chronos.api.dependencies import BackendState
from chronos.api.operational_health import collect_operational_health
from chronos.operations.health import (
    LivenessVerdict,
    OperationalObservations,
    ReadinessVerdict,
    TradingCapability,
)

router = APIRouter()


class HealthResponse(BaseModel):
    schema_version: Literal[2] = 2
    status: str
    status_scope: Literal["compatibility_only"] = "compatibility_only"
    broker_mode: str
    environment: str
    read_only: bool
    writer_lease_held: bool
    reconciliation_status: str
    reconciliation_generation: int
    assessed_at: AwareDatetime
    liveness: LivenessVerdict
    service_readiness: ReadinessVerdict
    trading_capability: TradingCapability
    observations: OperationalObservations


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    operational = collect_operational_health(request)
    candidate = getattr(request.app.state, "backend", None)
    if not isinstance(candidate, BackendState):
        return HealthResponse(
            status="starting",
            broker_mode="unknown",
            environment="unknown",
            read_only=True,
            writer_lease_held=False,
            reconciliation_status="PENDING",
            reconciliation_generation=0,
            assessed_at=operational.assessed_at,
            liveness=operational.liveness,
            service_readiness=operational.service_readiness,
            trading_capability=operational.trading_capability,
            observations=operational.observations,
        )
    backend = candidate
    settings = backend.runtime.settings
    readiness = operational.observations.reconciliation
    return HealthResponse(
        status="ok",
        broker_mode=settings.broker_mode.value,
        environment=settings.ib_environment.value,
        read_only=backend.read_only,
        writer_lease_held=backend.writer,
        reconciliation_status=readiness.status,
        reconciliation_generation=readiness.generation,
        assessed_at=operational.assessed_at,
        liveness=operational.liveness,
        service_readiness=operational.service_readiness,
        trading_capability=operational.trading_capability,
        observations=operational.observations,
    )
