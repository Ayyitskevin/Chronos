"""The transport an external model worker uses to reach Chronos (M6).

M5 built :mod:`chronos.supervisor.ingress` — a hardened parser that trusts
nothing — and disclosed that authenticating *which* worker is calling belongs to
the transport, deliberately not to the parser. This is that transport, and it
answers the question by **reusing what already exists** rather than inventing a
second, weaker scheme:

- the backend binds **loopback only**, so a worker must already be on this
  machine;
- every route here requires a credential, and submitting a proposal requires
  the **single-writer lease**, because a read-only backend accepting proposals
  would be building state the writer will never see.

Which credential depends on the owner's posture (ADR-0023). With no proposer
registry configured, proposals require the **local API token**, the same one
every other mutating endpoint requires — nothing here is weaker than the
surface it sits beside, and that was M6's design constraint. Once the owner
configures ``AUTONOMY_PROPOSERS_FILE``, the proposal route flips to requiring a
**registered proposer credential** and refuses the local token with a message
that says why. The proposer credential is proposal-only in both directions:
this route is the only one that accepts it, and the route records *which*
registration verified so the queue writer stamps that author's identity — not
a constant — into provenance at drain time. The alert read endpoint stays on
the local token; it is operator surface, not proposer surface.

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

import hashlib
import json
import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status

from chronos.api import bars as bar_plane
from chronos.api.auth import require_proposer, require_token
from chronos.api.dependencies import BackendState, require_writer
from chronos.domain.models import ChronosModel
from chronos.marketdata.bars import BarInterval
from chronos.supervisor import evidence_bundles, evidence_kinds, ingress
from chronos.terminal import views
from chronos.utils.identifiers import account_fingerprint
from chronos.utils.time import utc_now

_logger = logging.getLogger("chronos.api.autonomy")

#: How many symbols one evidence bundle may carry bars for. A bound on the work
#: a proposal-only credential can ask the broker-holding process to do, in the
#: same spirit as the queue's depth cap.
MAX_EVIDENCE_SYMBOLS = 32

# No router-level credential (ADR-0023): the proposal route and the alert route
# authenticate differently on purpose, so each route names its own dependency
# where a reader can see it.
router = APIRouter()

WriterDep = Annotated[BackendState, Depends(require_writer)]

#: The verified proposer_id, or None under the pre-registry posture. Declared
#: before ``WriterDep`` in the endpoint signature so authentication is judged
#: before the writer lease — an unauthenticated caller learns nothing about
#: this backend's read-only state.
ProposerDep = Annotated[str | None, Depends(require_proposer)]

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
    proposer_id: ProposerDep,
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
        # `proposer_id` is what the credential check verified, never anything
        # the payload said (ADR-0023). Recording it on the queue row is what
        # lets the drain stamp the right author after the credential itself is
        # long gone from memory.
        queued = proposal_queue.enqueue(
            db_session,
            account_fingerprint=fingerprint,
            payload=body.decode("utf-8"),
            now=utc_now(),
            proposer_id=proposer_id,
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


class EvidenceRequest(ChronosModel):
    """What a proposer asks for when it wants a bundle (ADR-0028).

    Two shapes, and the ``kind`` decides which fields matter:

    - ``backend_served`` — the backend composes and digests the document. The
      caller names the symbols it wants bars for and nothing else; it cannot
      supply a digest, because the whole point is that the backend computes one
      over bytes it holds.
    - ``alert_attested`` — the caller supplies the digest it computed over
      evidence Chronos never saw. The backend records the claim against the
      credential and the time, and says so in the record's kind.
    """

    kind: str = evidence_kinds.BundleKind.BACKEND_SERVED.value
    symbols: tuple[str, ...] = ()
    lookback_days: int = 180
    #: Required for ``alert_attested``; refused for ``backend_served``, where
    #: accepting a caller-supplied digest would quietly turn a witnessed record
    #: into an attested one under a label claiming otherwise.
    digest: str = ""


class EvidenceIssued(ChronosModel):
    """One issued bundle, and — for the served kind — the exact bytes digested.

    ``document`` is the canonical JSON string the digest was taken over. A
    proposer that renders it **verbatim** into its prompt gets ADR-0028's
    agreement rule for free; one that reformats, truncates, or re-serializes it
    will produce a different digest and be refused at admission, which is the
    accident this protocol is built to catch.
    """

    bundle_id: str
    kind: str
    digest: str
    bundle_version: str
    issued_at: str
    expires_at: str
    document: str = ""


@router.post(
    "/autonomy/evidence",
    # No `response_model`: this route answers with an `EvidenceIssued` on
    # success and a `ProposalAccepted`-shaped refusal otherwise, and pinning one
    # of them would make FastAPI validate the other into a 500 — turning an
    # honest refusal into a server error, which is the opposite of what a
    # fail-closed surface should do.
    status_code=status.HTTP_201_CREATED,
)
async def issue_evidence(
    request: EvidenceRequest,
    response: Response,
    http_request: Request,
    proposer_id: ProposerDep,
    state: WriterDep,
) -> EvidenceIssued | Any:
    """Issue one evidence bundle to the credential that asked for it (ADR-0028).

    **This is the one authorization-surface change ADR-0028 makes, and it is
    stated loudly rather than absorbed.** Issuance is a *write* reachable by a
    proposal-only credential, so this route makes that credential open a second
    route for the first time since ADR-0023 made it proposal-only. R-48's
    enumeration test — every mutating route, every way a confused process could
    present the credential, all 401 — grows this route as a deliberate, **named,
    tested exception** rather than absorbing it into the general rule. ADR-0028's
    own recommendation stands: this should be the last route that credential
    opens without a further ADR.

    Three bounds come with the surface, and none of them is optional:

    - **Only a registered proposer may ask.** A bundle is issued *to* a
      credential. With no registry configured there is no author to issue to, so
      the route refuses rather than issuing an unattributable record — the same
      combination startup already alerts on.
    - **Per-proposer issuance cap.** A proposer that could mint unbounded rows is
      a disk-filling denial of service against the process holding the broker
      connection, so the cap refuses rather than evicting an in-flight bundle
      (``evidence_bundles.MAX_LIVE_BUNDLES_PER_PROPOSER``, the shape
      ``proposals.MAX_PENDING`` already uses).
    - **The writer lease.** A read-only backend issuing bundles would be building
      state the writer will never judge against.

    201 rather than 202: unlike a proposal, something *was* created that the
    caller now holds — a record with an id it will cite. It authorizes nothing on
    its own; every gate downstream is unchanged.
    """

    runtime = state.runtime
    if not getattr(http_request.app.state, "evidence_binding", False):
        # Not an error the caller can fix, and deliberately not a silent success:
        # issuing bundles nothing will ever check would manufacture records that
        # look like evidence binding is in force when it is not.
        response.status_code = status.HTTP_404_NOT_FOUND
        return ProposalAccepted(
            accepted=False,
            stage="EVIDENCE",
            refusal="EVIDENCE_BINDING_DISABLED",
            detail=(
                "evidence binding is not configured on this backend; set "
                "AUTONOMY_EVIDENCE_BUNDLES to issue bundles"
            ),
        )
    if getattr(http_request.app.state, "evidence_posture_broken", False) or proposer_id is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ProposalAccepted(
            accepted=False,
            stage="EVIDENCE",
            refusal="EVIDENCE_POSTURE_INVALID",
            detail=(
                "evidence binding is configured without a proposer registry; a bundle is "
                "issued to a credential, so issuance refuses until the owner configures "
                "AUTONOMY_PROPOSERS_FILE"
            ),
        )
    fingerprint = _fingerprint_of(runtime)
    if not fingerprint:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ProposalAccepted(
            accepted=False,
            stage="EVIDENCE",
            refusal="BACKEND_UNSCOPED",
            detail="this backend is not bound to an account; evidence cannot be issued",
        )

    try:
        kind = evidence_kinds.BundleKind(request.kind)
    except ValueError:
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        return ProposalAccepted(
            accepted=False,
            stage="EVIDENCE",
            refusal="EVIDENCE_KIND_UNKNOWN",
            detail=f"{request.kind!r} is not an evidence bundle kind",
        )

    document = ""
    if kind is evidence_kinds.BundleKind.BACKEND_SERVED:
        if request.digest:
            # Accepting a caller's digest here would relabel an attestation as a
            # witnessing — the exact substitution ADR-0028 forbids between kinds.
            response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
            return ProposalAccepted(
                accepted=False,
                stage="EVIDENCE",
                refusal="EVIDENCE_DIGEST_NOT_ACCEPTED",
                detail=(
                    "a backend_served bundle digests the bytes the backend serves; a "
                    "caller-supplied digest would make it attested while labelled served"
                ),
            )
        composed = compose_served_document(
            runtime,
            state,
            symbols=request.symbols,
            lookback_days=request.lookback_days,
            now=utc_now(),
        )
        if composed is None:
            # The fact-gathering doctrine, applied to issuance: evidence is
            # never invented to keep a caller moving.
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return ProposalAccepted(
                accepted=False,
                stage="EVIDENCE",
                refusal="EVIDENCE_UNAVAILABLE",
                detail=(
                    "the backend could not gather the facts a bundle is composed from; no "
                    "bundle is issued rather than one describing facts it does not have"
                ),
            )
        document, digest = composed
    else:
        digest = request.digest.strip().lower()

    now = utc_now()
    try:
        with runtime.database.sessions.begin() as db_session:
            issued = evidence_bundles.issue(
                db_session,
                account_fingerprint=fingerprint,
                proposer_id=proposer_id,
                kind=kind,
                digest=digest,
                now=now,
                ttl_seconds=runtime.settings.autonomy_evidence_ttl_seconds,
            )
    except evidence_bundles.IssuanceRefused as error:
        # 429 for the cap (a load condition to back off from), 422 for a
        # malformed digest (a request the caller can fix).
        response.status_code = (
            status.HTTP_429_TOO_MANY_REQUESTS
            if "cap" in str(error)
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        return ProposalAccepted(
            accepted=False,
            stage="EVIDENCE",
            refusal="EVIDENCE_ISSUANCE_REFUSED",
            detail=str(error),
        )

    return EvidenceIssued(
        bundle_id=issued.bundle_id,
        kind=issued.kind.value,
        digest=issued.digest,
        bundle_version=issued.bundle_version,
        issued_at=issued.issued_at.isoformat(),
        expires_at=issued.expires_at.isoformat(),
        document=document,
    )


def compose_served_document(
    runtime: Any,
    state: Any,
    *,
    symbols: tuple[str, ...],
    lookback_days: int,
    now: datetime,
) -> tuple[str, str] | None:
    """The canonical evidence document and its digest, or ``None`` if ungatherable.

    Composes the same facts a worker used to fetch for itself — account summary,
    positions, Chronos-owned orders, daily bars per symbol — into one canonical
    JSON string, and takes SHA-256 over its exact UTF-8 bytes. Canonicalized the
    same way the hash chain canonicalizes payloads (sorted keys, no insignificant
    whitespace), so a key-order difference cannot produce a different digest for
    identical facts and break the comparison for no reason.

    Returns the string, not a structure, because **the string is the artifact**:
    the digest is over these bytes, and a proposer that re-serializes an
    equivalent structure has not rendered what was digested.

    ``None`` on any failed read. Issuing a bundle over partial facts would
    produce a record whose digest binds a document that silently omits what could
    not be fetched — a worse outcome than refusing, and the same doctrine the
    supervisor's fact gatherers already follow.
    """

    bounded = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip()))
    if len(bounded) > MAX_EVIDENCE_SYMBOLS:
        bounded = bounded[:MAX_EVIDENCE_SYMBOLS]
    lookback = max(1, min(lookback_days, bar_plane.MAX_LOOKBACK_DAYS))

    try:
        summary = runtime.connection.run(runtime.broker.account_summary())
        positions = runtime.connection.run(runtime.broker.positions())
        orders = runtime.order_management.list_orders()
    except Exception:
        _logger.exception("Evidence composition failed gathering account facts")
        return None

    payload: dict[str, Any] = {
        "account": summary.model_dump(mode="json"),
        "positions": [position.model_dump(mode="json") for position in positions],
        "open_orders": [_order_fact(record) for record in orders],
    }
    bars: dict[str, Any] = {}
    for symbol in bounded:
        try:
            answer = bar_plane.provider_for(runtime, state).bars(
                symbol, interval=BarInterval.DAY_1, lookback_days=lookback
            )
        except Exception:
            _logger.exception("Evidence composition failed gathering bars for %s", symbol)
            return None
        # Rendered through the terminal's own view builder, so the bars a bundle
        # carries are the same payload the worker used to fetch for itself — the
        # facts moved behind one credential, they did not change shape. A paced
        # refusal travels as data exactly as that route renders it: the model is
        # told the chart is unavailable rather than shown a fabricated one, and
        # the digest covers that honest statement.
        bars[symbol] = views.bars_view(
            answer.series,
            lookback_days=lookback,
            source=answer.source,
            fetched_at=answer.fetched_at,
            stale=answer.stale,
            refusal=answer.refusal,
            now=now,
        ).model_dump(mode="json")
    payload["daily_bars"] = bars
    payload["watchlist"] = list(bounded)
    payload["as_of"] = now.isoformat()
    payload["bundle_version"] = evidence_bundles.BUNDLE_VERSION

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _order_fact(record: Any) -> dict[str, Any]:
    """One Chronos-owned order, as evidence. No broker handles, no raw ids."""

    intent = getattr(record, "intent", None)
    return {
        "intent_id": str(getattr(intent, "intent_id", "") or ""),
        "chronos_reference": str(getattr(intent, "chronos_reference", "") or ""),
        "symbol": str(getattr(intent, "symbol", "") or ""),
        "lifecycle": str(getattr(getattr(record, "lifecycle", None), "value", "") or ""),
        "quantity": str(getattr(intent, "quantity", "") or ""),
    }


class AlertView(ChronosModel):
    """One unacknowledged alert, for an operator UI or a polling script."""

    id: int
    severity: str
    kind: str
    summary: str
    occurrences: int
    raised_at: str
    delivered: bool


@router.get(
    "/autonomy/alerts",
    response_model=list[AlertView],
    dependencies=[Depends(require_token)],
)
def list_alerts(state: WriterDep) -> list[AlertView]:
    """Everything the owner has not acknowledged, most severe first.

    A read endpoint on the *pull* side of alerting. It complements the delivery
    sinks rather than replacing them: R-32 is about the owner being told without
    having to look, and an endpoint is something you have to look at.

    Stays on the local API token under every posture: this is operator surface,
    and a proposer credential deliberately opens nothing but the proposal route.
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

    Derived from ``order_management.account_id`` exactly as the terminal's
    read-models and ``build_autonomy_runtime`` derive it. Until 2026-08-12 this
    read ``runtime.account_fingerprint`` — an attribute :class:`AppRuntime`
    never had — so every proposal POST answered 503 BACKEND_UNSCOPED and the
    alert endpoint always answered ``[]``, on every backend, since the route
    existed. Fail-closed in direction (a refusal, never a wrong account) and
    still a live defect of the inert-control shape (R-24..R-27):
    ``tests/safety/test_proposer_credentials_exercised.py`` now proves a
    proposal actually reaches the queue.
    """

    account_id = str(getattr(runtime.order_management, "account_id", "") or "").strip()
    if not account_id:
        return ""
    return account_fingerprint(account_id)
