"""Live safety-layer endpoints (Milestone 6): arming and the kill switch.

Every endpoint here requires the local token. The single-writer lease is
required only by the operations that **grant** authority; the two that remove
it are deliberately reachable without it:

===========================  ==========  ==========================================
Endpoint                     Lease?      Why
===========================  ==========  ==========================================
``POST /live/arm``           required    grants live authority
``POST /live/kill/disengage``required    restores trading after a halt
``POST /live/disarm``        NOT req'd   only ever removes authority
``POST /live/kill``          NOT req'd   the emergency stop must always be reachable
===========================  ==========  ==========================================

The M2 review found this asymmetry missing, and the writer-lease heartbeat
(R-24) is what made it urgent: before the heartbeat, read-only was a
*startup* condition, so the operator of a lease-holding process could always
reach the kill switch. A running backend can now demote itself mid-session on
a lost lease, and with a uniform ``require_writer`` that demotion would have
locked its operator out of the emergency stop at the exact moment something had
already gone wrong. Both spared operations are monotonically restricting and
both write lock-protected state, so serving them from a non-lease-holder is
fail-safe: the worst case is that trading stops when it need not have.

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
def disarm(state: StateDep) -> ArmState:
    """Revoke arming. Deliberately not writer-gated: this only removes authority."""

    return state.runtime.live_arming.revoke(now=_now())


@router.post("/live/kill", response_model=KillSwitchState)
def engage_kill_switch(request: KillRequest, state: StateDep) -> KillSwitchState:
    """Engage the halt. Deliberately not writer-gated — see the module docstring.

    A backend that lost its lease is exactly the backend whose operator most
    needs the emergency stop, and refusing here would have been the one refusal
    that increases risk.
    """

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
