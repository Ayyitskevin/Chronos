"""Live safety-layer endpoints (Milestone 6): arming and the kill switch.

Arming and disarming, and engaging/disengaging the kill switch, are mutating
and require the local token AND the single-writer lease. Status is read-only.
The typed arm phrase is carried in the request body, compared server-side in
constant time, and NEVER echoed back or logged — responses carry only the arm
STATE (armed flag + expiry), never the phrase.

Milestone 6 builds this layer; it does not yet enable live transmission (that
is Milestone 7). Arming here authorizes nothing on its own — it is one of the
ten live gates.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from chronos.api.auth import require_token
from chronos.api.dependencies import BackendState, get_state, require_writer
from chronos.domain.models import ChronosModel
from chronos.orders.arming import ArmState, LiveArmingError
from chronos.orders.kill_switch import KillSwitchState
from chronos.utils.time import utc_now

router = APIRouter(dependencies=[Depends(require_token)])

StateDep = Annotated[BackendState, Depends(get_state)]
WriterDep = Annotated[BackendState, Depends(require_writer)]


class ArmRequest(ChronosModel):
    phrase: str
    reason: str = "operator arm"


class KillRequest(ChronosModel):
    reason: str


class DisengageRequest(ChronosModel):
    note: str


class LiveStatus(ChronosModel):
    arm: ArmState
    kill_switch: KillSwitchState


def _now() -> datetime:
    return utc_now()


@router.get("/live/status", response_model=LiveStatus)
def live_status(state: StateDep) -> LiveStatus:
    runtime = state.runtime
    return LiveStatus(
        arm=runtime.live_arming.state(now=_now()),
        kill_switch=runtime.live_kill_switch.read(),
    )


@router.post("/live/arm", response_model=ArmState)
def arm(request: ArmRequest, state: WriterDep) -> ArmState:
    try:
        return state.runtime.live_arming.arm(request.phrase, now=_now(), reason=request.reason)
    except LiveArmingError as error:
        # Do not echo the phrase; a generic 400 is enough.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="arm phrase did not match the required phrase",
        ) from error


@router.post("/live/disarm", response_model=ArmState)
def disarm(state: WriterDep) -> ArmState:
    return state.runtime.live_arming.revoke(now=_now())


@router.post("/live/kill", response_model=KillSwitchState)
def engage_kill_switch(request: KillRequest, state: WriterDep) -> KillSwitchState:
    try:
        return state.runtime.live_kill_switch.engage(
            reason=request.reason, initiated_by="operator", now=_now()
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error


@router.post("/live/kill/disengage", response_model=KillSwitchState)
def disengage_kill_switch(request: DisengageRequest, state: WriterDep) -> KillSwitchState:
    try:
        return state.runtime.live_kill_switch.disengage(
            operator_note=request.note, initiated_by="operator", now=_now()
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
