"""The transport an external model worker uses to reach Chronos (M6).

M5 built :mod:`chronos.supervisor.ingress` — a hardened parser that trusts
nothing — and disclosed that authenticating *which* worker is calling belongs to
the transport, deliberately not to the parser. This is that transport, and it
answers the question by **reusing what already exists** rather than inventing a
second, weaker scheme:

- the backend binds **loopback only**, so a worker must already be on this
  machine;
- every route here requires the **local API token**, the same one every other
  mutating endpoint requires;
- and submitting a proposal requires the **single-writer lease**, because a
  read-only backend accepting proposals would be building state the writer will
  never see.

A worker therefore needs local access plus the token — which is exactly the bar
for reaching any other part of this backend. Nothing here is weaker than the
surface it sits beside, and that was the design constraint.

## What a proposal endpoint may and may not be

``POST /autonomy/proposals`` accepts a proposal and returns what the gateway
decided. It is **not** an order endpoint, and the difference is structural
rather than a matter of naming:

- The body is parsed by the ingress, which refuses anything carrying
  ``provenance`` or ``decision_id`` — so a caller cannot attribute or name its
  own decision even over an authenticated channel.
- Everything downstream is the same gate stack a proposal from any source
  faces. The route adds no authority; it only supplies the transport.
- **The cycle does not run in the request (M7).** The route validates, stores
  the raw payload in a bounded durable queue, and returns. The runtime judges
  queued proposals on its own tick, so the rate of broker interaction is set by
  configuration rather than by however fast a caller can POST.

## Why the response says so much

It returns the stage the cycle stopped at and the refusal. A worker that cannot
see why it was refused will retry blindly, and blind retries are what R-31's
bound exists to survive — better that the worker learns it is out of scope than
that it keeps asking. The response deliberately carries no account identifier,
no broker detail, and no evidence content.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status

from chronos.api.auth import require_token
from chronos.api.dependencies import BackendState, require_writer
from chronos.domain.models import ChronosModel
from chronos.supervisor import ingress

router = APIRouter(dependencies=[Depends(require_token)])

WriterDep = Annotated[BackendState, Depends(require_writer)]

#: A conservative cap on one request body, applied before the ingress sees it.
#: The ingress has its own bound; this one stops a large body being read into
#: memory by the server at all.
MAX_BODY_BYTES = ingress.MAX_PAYLOAD_BYTES


class ProposalAccepted(ChronosModel):
    """What the gateway decided about one proposal.

    Deliberately narrow: a stage, a refusal code, and a detail string that
    Chronos itself wrote. No account id, no broker state, no evidence content.
    """

    accepted: bool
    stage: str
    refusal: str = ""
    detail: str = ""


@router.post(
    "/autonomy/proposals",
    response_model=ProposalAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_proposal(
    request: Request,
    response: Response,
    state: WriterDep,
) -> ProposalAccepted:
    """Accept one proposal from an external model worker.

    Returns 202 rather than 200 or 201: the proposal was *received and judged*,
    and no resource was created that the caller owns. A 201 would imply an order
    exists, which is precisely the impression this endpoint must not give.

    The body is read as raw bytes and handed to the ingress unparsed. FastAPI's
    own model binding is deliberately bypassed for the proposal itself, because
    the ingress's guarantees — size bound before parsing, NaN refusal,
    writer-owned-field refusal — are the ones that matter here, and letting a
    second parser see the bytes first would mean two parsers to reason about.
    """

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        response.status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        return ProposalAccepted(
            accepted=False,
            stage="INGRESS",
            refusal="PAYLOAD_TOO_LARGE",
            detail=f"a proposal must be under {MAX_BODY_BYTES} bytes",
        )

    outcome = ingress.parse_proposal(body)
    if outcome.proposal is None:
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        return ProposalAccepted(
            accepted=False,
            stage="INGRESS",
            refusal="MALFORMED_PROPOSAL",
            detail=outcome.refusal,
        )

    # The cycle does NOT run here (M7). Running it inside the request would let
    # the caller's HTTP schedule drive broker interaction — the unbounded
    # event-driven shape the runtime design rejects. The proposal is stored
    # durably and the runtime judges it on its own tick; the queue is bounded so
    # a runaway worker cannot fill the disk of the process holding the broker
    # connection.
    runtime = state.runtime
    fingerprint = _fingerprint_of(runtime)
    if not fingerprint:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ProposalAccepted(
            accepted=False,
            stage="INGRESS",
            refusal="BACKEND_UNSCOPED",
            detail="this backend is not bound to an account; proposals cannot be queued",
        )
    from chronos.supervisor import proposals as proposal_queue
    from chronos.utils.time import utc_now

    with runtime.database.sessions.begin() as db_session:
        queued = proposal_queue.enqueue(
            db_session,
            account_fingerprint=fingerprint,
            payload=body.decode("utf-8"),
            now=utc_now(),
        )
    if not queued.queued:
        # 429: the queue is a load condition the worker should back off from,
        # not a malformed request or a server fault.
        response.status_code = status.HTTP_429_TOO_MANY_REQUESTS
        return ProposalAccepted(
            accepted=False,
            stage="INGRESS",
            refusal="QUEUE_FULL",
            detail=queued.refusal,
        )
    return ProposalAccepted(
        accepted=True,
        stage="QUEUED",
        detail=(
            f"proposal is well-formed and queued at depth {queued.pending_depth}; the "
            "runtime judges it on its own tick. Queued is received, not authorized — "
            "nothing has been judged or sent"
        ),
    )


class AlertView(ChronosModel):
    """One unacknowledged alert, for an operator UI or a polling script."""

    id: int
    severity: str
    kind: str
    summary: str
    occurrences: int
    raised_at: str
    delivered: bool


@router.get("/autonomy/alerts", response_model=list[AlertView])
def list_alerts(state: WriterDep) -> list[AlertView]:
    """Everything the owner has not acknowledged, most severe first.

    A read endpoint on the *pull* side of alerting. It complements the delivery
    sinks rather than replacing them: R-32 is about the owner being told without
    having to look, and an endpoint is something you have to look at.
    """

    from chronos.supervisor import alerts as alert_module

    runtime = state.runtime
    fingerprint = _fingerprint_of(runtime)
    if not fingerprint:
        return []
    with runtime.database.sessions.begin() as session:
        pending = alert_module.unacknowledged(session, account_fingerprint=fingerprint)
        return [
            AlertView(
                id=alert.id,
                severity=alert.severity.value,
                kind=alert.kind,
                summary=alert.summary,
                occurrences=alert.occurrences,
                raised_at=alert.raised_at.isoformat(),
                delivered=False,
            )
            for alert in pending
        ]


def _fingerprint_of(runtime: Any) -> str:
    """The account fingerprint this backend is scoped to, or empty.

    Empty rather than raising: an unscoped database has no alerts to show, and a
    500 on a read endpoint would be a worse answer than an empty list.
    """

    scope = getattr(runtime, "account_fingerprint", "")
    return str(scope) if scope else ""
