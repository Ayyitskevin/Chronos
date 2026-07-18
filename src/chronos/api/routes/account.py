"""Read-only account and portfolio endpoints (token-protected)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from chronos.api.auth import require_token
from chronos.api.dependencies import BackendState, get_state
from chronos.domain.models import AccountSummary, BrokerPosition, ConnectionStatus

router = APIRouter(dependencies=[Depends(require_token)])

StateDep = Annotated[BackendState, Depends(get_state)]


@router.get("/account/summary", response_model=AccountSummary)
def account_summary(state: StateDep) -> AccountSummary:
    runtime = state.runtime
    return runtime.connection.run(runtime.broker.account_summary())


@router.get("/account/connection", response_model=ConnectionStatus)
def connection_status(state: StateDep) -> ConnectionStatus:
    runtime = state.runtime
    return runtime.connection.run(runtime.broker.connection_status())


@router.get("/account/positions", response_model=tuple[BrokerPosition, ...])
def positions(state: StateDep) -> tuple[BrokerPosition, ...]:
    runtime = state.runtime
    return runtime.connection.run(runtime.broker.positions())
