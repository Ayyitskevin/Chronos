"""The operator terminal's data plane: ``/terminal/*`` (M8a, ADR-0018).

``chronos.terminal.commands`` declares what the terminal can ask for and
``chronos.terminal.views`` declares what it may be told. This module is the only
thing that joins them to a running backend, and it is deliberately thin: it
resolves the account pseudonym, opens a session, calls one assembler, and
returns what the assembler built. Every judgement about *what may be said* lives
in the read-models, where it can be tested against an in-memory database with no
app, no lease, and no broker.

## What this router is not

ADR-0018 §4: the terminal is a window and a set of owner buttons, **never a
gateway**. Nothing here constructs, prices, modifies, or transmits an order, and
there is no route below through which one could. The terminal's execution
surface is the *existing* order routes in :mod:`chronos.api.routes.orders`,
called by the browser exactly as any other client calls them; this module has no
name for them and adds no authority to them.

The only things it reads from the order plane are the account id — hashed
immediately into the pseudonym, never returned — and the two safety-layer
*states* the status bar shows. Neither is a method that can send anything.

## Why the read routes are not writer-gated

Every ``GET`` here depends on :func:`~chronos.api.dependencies.get_state`, not
on ``require_writer``. A backend that lost the single-writer lease is precisely
the backend whose operator most needs to see queue depth, refusals, and alerts;
gating inspection on the lease would make the terminal go dark at the moment it
became most useful. This mirrors the asymmetry :mod:`chronos.api.routes.live`
already applies to the kill switch, for the same reason.

``GET /terminal/alerts`` therefore differs **on purpose** from the pre-existing
``GET /autonomy/alerts``, which requires the lease (ADR-0018 residual 3). A
demoted terminal that cannot show alerts is a terminal that hides the conditions
an operator is being asked to judge. The older route keeps its gate until that
is revisited deliberately rather than as a side effect of this milestone.

The two mutating routes — acknowledge and revoke — do require the lease. Both
write durable state that the writer owns, and a read-only backend accepting them
would be building history the writer never sees.

## Revoking a mandate is a new authorization surface

Until now ``chronos.supervisor.durable.revoke`` had no route and no CLI: the
only way to stand the supervisor down was to edit the database. ``POST
/terminal/mandate/revoke`` makes it reachable over HTTP for the first time, and
ADR-0018 residual 4 names it so an adversarial review has a target. What bounds
it:

- **Owner-only in the same sense every other mutation here is**: loopback
  binding, the local API token, and the single-writer lease.
- **A reason is required and non-empty.** ``durable.revoke`` stores whatever
  string it is given, so the requirement is enforced here — an unexplained
  revocation is indistinguishable from a stray request in the audit trail, and
  the record exists to make those two different.
- **It binds to the grant the owner was shown.** The request may name the
  ``mandate_id`` the panel was displaying, and a request naming a different grant
  than the one in force is refused rather than applied to whichever one this
  process happens to be under. ADR-0017 auto-activation means those can diverge
  while the owner types (:class:`RevokeRequest`), and the typed-confirmation gate
  is worth nothing if what it confirms and what it does are different mandates.
- **It is audited.** The revocation marks the activation in place and appends to
  the authority hash chain; nothing is deleted.
- **It survives a restart** (ADR-0017). Auto-activation refuses to re-activate a
  mandate version whose activation was revoked, so revoking is not a race
  against the process supervisor. Re-granting is a new ``mandate_version``,
  which is a fresh owner act.
- **It only ever removes authority**, which is why exposing it is a smaller step
  than it first appears: there is no corresponding route that grants one.

``revoked: false`` is an **answer, not an error**. ``durable.revoke`` returns
``False`` when nothing was in force, and reporting that as a 4xx would tell the
owner their request failed when in fact their intent already held.

Revocation does not reach into the running tick. It is durable state that
:mod:`chronos.supervisor.admission` re-derives before every cycle, so a cycle
already in flight finishes and the next one refuses. That is stated plainly in
the response detail rather than left for the operator to assume.

## The account pseudonym, derived the way the supervisor derives it

:func:`_fingerprint_of` hashes ``order_management.account_id`` exactly as
``chronos.api.autonomy_wiring.build_autonomy_runtime`` does, so this module reads
the rows the supervisor writes. It is worth saying why that is spelled out
rather than shared: ``chronos.api.routes.autonomy._fingerprint_of`` derives the
same value with ``getattr(runtime, "account_fingerprint", "")``, and no
``AppRuntime`` carries that attribute — so that route silently scopes itself to
"no account" and answers empty. Copying it would have made the terminal quietly
wrong in the same way, and a panel that reports "no alerts" because it looked in
the wrong place is worse than one that reports nothing at all.

An unscoped backend yields an empty pseudonym, which the read-models turn into
``None`` everywhere — *unknown*, never zero (``views`` module docstring).

## Which mandate is "the mandate"

:func:`_mandate_in_force` prefers the mandate the **autonomy runtime is actually
judging against**, and falls back to the owner's grant file only when this
process has no runtime — which is the read-only case, since ``create_app``
builds a runtime only for the writer. The order matters:

- Preferring the runtime means an owner who edits the file after boot is not
  shown a grant this process is not using. The failure that ordering prevents is
  a panel that looks *safer* than the process it describes.
- The fallback means a demoted terminal still shows the authority in force,
  because the activation fields it displays (``activated_at``, ``revoked``,
  ``owner_event_id``) come from the shared database, not from the file. A file
  that was edited, or never activated, shows ``activated_at: null`` — which is
  the truth about that document.

## Honest residuals

1. **The shell cannot authenticate itself.** ``/terminal/app`` is served without
   the token (a browser cannot put a header on a document load), while every
   route in this module requires it. The bundled client sends no token, so a
   browser that loads the shell today gets ``401`` on every panel and renders
   that refusal honestly rather than blank. Closing this needs a deliberate
   browser-session decision — a cookie, or an operator-pasted token — and
   inventing one here would be adding an unreviewed authorization surface in the
   same change that adds the first one ADR-0018 already flagged.
2. **Polling, not streaming** (ADR-0018 residual 2). Each panel is one request
   per interval against the process that holds the broker connection. Every read
   below is a single short transaction for that reason.
3. **The mandate file is re-read per request on a runtime-less backend.** No
   caching, deliberately: a cached grant outliving the file it came from is the
   kind of stale authority this terminal exists to expose. A broken file also
   logs on each read, which is noise, but a broken grant is a condition an
   operator should keep being told about.
4. **Session counters are keyed to the supervisor's market timezone.**
   ``AUTONOMY_MARKET_TIMEZONE`` is not validated by settings, so an unusable
   value makes the session boundary unnameable and ``GET /terminal/counters``
   answers 503 with that reason. Falling back to UTC would name a *different*
   session and show its counters as this one's, which is the fabrication
   ``durable.session_key`` refuses for the same reason.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import Field

from chronos.api.auth import require_token
from chronos.api.autonomy_wiring import load_persistent_mandate
from chronos.api.dependencies import BackendState, get_state, require_writer
from chronos.autonomy import AutonomyMandate
from chronos.domain.models import ChronosModel
from chronos.runtime import AppRuntime
from chronos.supervisor import alerts as alert_module
from chronos.supervisor import durable
from chronos.supervisor.runtime import AutonomyRuntime
from chronos.terminal import commands, views
from chronos.utils.identifiers import account_fingerprint
from chronos.utils.time import utc_now

_logger = logging.getLogger("chronos.api.terminal")

router = APIRouter(dependencies=[Depends(require_token)])

StateDep = Annotated[BackendState, Depends(get_state)]
WriterDep = Annotated[BackendState, Depends(require_writer)]


class CommandRegistry(ChronosModel):
    """The command surface, exactly as :data:`chronos.terminal.commands.COMMANDS` declares it.

    The entries are carried as plain JSON objects rather than as a second
    Pydantic model mirroring :class:`~chronos.terminal.commands.TerminalCommand`.
    A mirror would be a second declaration of a command's shape, and the whole
    reason ADR-0018 §6 moves the registry into Python is that there should be
    exactly one. ``registry_payload()`` is that one; this route serves it.
    """

    commands: tuple[dict[str, object], ...]


class AcknowledgeRequest(ChronosModel):
    """The owner's note, and nothing else.

    No severity, no kind, no "resolve" flag: acknowledging records that a human
    saw the condition. It does not resolve it, delete it, or stop it recurring,
    and a body that could say otherwise would invite the belief that it did.
    """

    note: str = ""


class AcknowledgeResult(ChronosModel):
    """What the acknowledgement did. ``acknowledged`` is the whole answer."""

    acknowledged: bool
    alert_id: int
    detail: str = ""


class RevokeRequest(ChronosModel):
    """Why the owner is standing the supervisor down, and which grant they meant.

    ``reason`` is required and recorded. ``mandate_id`` is the grant the mandate
    panel was displaying when the owner typed the confirmation phrase, and it is
    what binds the recorded act to the stated intent. Without it this route
    re-derives whatever is in force at request time, and ADR-0017 auto-activation
    makes the gap between those two moments a real sequence rather than a
    hypothetical: the backend can restart under an edited grant M-2 while the
    owner is still typing a reason about M-1, and the authority chain would then
    record the revocation of a mandate nobody named.

    It stays **optional** because it is a check, not an argument to the
    revocation. A caller with no panel in front of it — curl, a script, a future
    CLI — has no displayed grant to bind to and gets the old behaviour, which
    fails safe in the same direction revocation always does. The typed
    confirmation always sends it, because that is the surface where a name was
    shown to a human.

    The bound is :class:`~chronos.autonomy.AutonomyMandate`'s own, because this
    field exists to name one of those and the refusal below quotes what it was
    given back to the operator. A cap invented here would either refuse a legal
    mandate id or let the echo carry something no mandate could be called.
    """

    reason: str
    mandate_id: str | None = Field(default=None, max_length=128)


class RevokeResult(ChronosModel):
    """What the revocation did, including "nothing was in force".

    The reason is deliberately not echoed. It is owner-authored text that is
    already durable in the activation row and the authority chain; reflecting it
    would put it in a response body and a browser buffer for no gain.
    """

    revoked: bool
    mandate_id: str | None = None
    detail: str = ""


@router.get("/terminal/commands", response_model=CommandRegistry)
def command_registry() -> CommandRegistry:
    """The registry the client draws its command surface from.

    Deliberately free of :func:`~chronos.api.dependencies.get_state`: the
    registry is a module-level constant validated at import, so it can be
    answered by a backend whose runtime failed to initialize. HELP working when
    nothing else does is worth the one route that does not touch state — an
    operator facing a broken backend should still be able to read what the
    terminal would have been able to ask.

    Still token-gated, because the router is. Nothing here is secret; it is
    simply not a reason to open a hole.
    """

    return CommandRegistry(commands=tuple(commands.registry_payload()))


@router.get("/terminal/system", response_model=views.SystemView)
def system(request: Request, state: StateDep) -> views.SystemView:
    """The supervision status bar: is anything running, and may it act.

    ``read_only`` reports ``not state.writer`` rather than ``state.read_only``,
    because that is the exact predicate ``require_writer`` applies. A badge
    derived from a different one could tell the owner they are the writer and
    then have the mutating routes refuse them, and a terminal that offers a
    button the backend will reject teaches the operator to distrust the display.

    ``kill_switch_engaged`` and ``live_armed`` are passed as concrete booleans
    rather than ``None`` because this backend can genuinely observe both: the
    kill switch reads a file and fails closed to ENGAGED when it cannot parse
    it, and arming is this process's own memory. The ``None`` arm of those
    fields exists for callers that cannot observe them; claiming to be one would
    be its own small dishonesty.
    """

    runtime = state.runtime
    now = utc_now()
    autonomy = _autonomy_of(request)
    with runtime.database.sessions.begin() as session:
        return views.system_view(
            session,
            account_fingerprint=_fingerprint_of(runtime),
            now=now,
            read_only=not state.writer,
            autonomy=autonomy,
            mandate=_mandate_in_force(runtime, autonomy),
            kill_switch_engaged=runtime.live_kill_switch.read().engaged,
            live_armed=runtime.live_arming.state(now=now).armed,
        )


@router.get("/terminal/mandate", response_model=views.MandateView)
def mandate(request: Request, state: StateDep) -> views.MandateView:
    """The authority in force, as the owner granted it.

    A read, and only a read. ``active`` here reports that an unrevoked
    activation covers now; it is never a permission, because admission
    re-derives account match, mode, promotion, restart behaviour and every limit
    before anything trades.
    """

    runtime = state.runtime
    autonomy = _autonomy_of(request)
    with runtime.database.sessions.begin() as session:
        return views.mandate_view(
            session,
            account_fingerprint=_fingerprint_of(runtime),
            now=utc_now(),
            mandate=_mandate_in_force(runtime, autonomy),
        )


@router.get("/terminal/journal", response_model=views.JournalView)
def journal(
    state: StateDep,
    limit: Annotated[int, Query(description="How many cycles to return.")] = (
        views.DEFAULT_JOURNAL_LIMIT
    ),
) -> views.JournalView:
    """Where each judged proposal stopped, and why, newest first.

    ``limit`` is **clamped** rather than validated into a 422. The bound that
    matters is the server's own — the assembler never returns more than
    ``MAX_JOURNAL_LIMIT`` rows whatever is asked — and refusing an out-of-range
    number as well would only teach an operator poking at the URL that the
    terminal argues with them. The applied bound is returned in
    :attr:`~chronos.terminal.views.JournalView.limit`, so what was clamped is
    visible rather than silent.
    """

    runtime = state.runtime
    with runtime.database.sessions.begin() as session:
        return views.journal_view(
            session,
            account_fingerprint=_fingerprint_of(runtime),
            now=utc_now(),
            limit=limit,
        )


@router.get("/terminal/counters", response_model=views.CountersView)
def counters(state: StateDep) -> views.CountersView:
    """This session's observed activity, or an explicit statement that none was.

    The session boundary comes from ``AUTONOMY_MARKET_TIMEZONE`` — the same
    setting the cycle stamps onto its facts — because the counters are keyed by
    it. Reading them under a different timezone would look up a different
    session and present its figures as this one's, which near a session boundary
    is exactly when an operator is checking.
    """

    runtime = state.runtime
    with runtime.database.sessions.begin() as session:
        try:
            return views.counters_view(
                session,
                account_fingerprint=_fingerprint_of(runtime),
                now=utc_now(),
                market_timezone=runtime.settings.autonomy_market_timezone,
            )
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "the session boundary cannot be named, so no counter row can be "
                    f"identified: {error}"
                ),
            ) from error


@router.get("/terminal/queue", response_model=views.QueueView)
def queue(state: StateDep) -> views.QueueView:
    """How much work is waiting for a cycle, and how the recent arrivals came out.

    The stored proposal payload is never returned — see
    :class:`~chronos.terminal.views.QueueEntryView`. What an operator needs is
    what the cycle concluded, not a verbatim copy of untrusted worker input in
    the surface they read to decide whether to intervene.
    """

    runtime = state.runtime
    with runtime.database.sessions.begin() as session:
        return views.queue_view(
            session,
            account_fingerprint=_fingerprint_of(runtime),
            now=utc_now(),
        )


@router.get("/terminal/alerts", response_model=views.AlertsView)
def alerts(state: StateDep) -> views.AlertsView:
    """Everything the owner has not acknowledged, most severe first.

    Not writer-gated, unlike ``GET /autonomy/alerts`` — see the module
    docstring. A demoted backend still holds the alert rows, and an operator
    looking at a terminal that has gone quiet cannot tell "nothing is wrong"
    from "this backend refuses to say".
    """

    runtime = state.runtime
    with runtime.database.sessions.begin() as session:
        return views.alerts_view(
            session,
            account_fingerprint=_fingerprint_of(runtime),
            now=utc_now(),
        )


@router.post("/terminal/alerts/{alert_id}/acknowledge", response_model=AcknowledgeResult)
def acknowledge_alert(
    alert_id: int, body: AcknowledgeRequest, state: WriterDep
) -> AcknowledgeResult:
    """Record that the owner saw an alert. It resolves nothing.

    The "a note is required" rule is **not** re-implemented here.
    ``chronos.supervisor.alerts.acknowledge`` owns it and raises; this route
    translates that into a 422 carrying the domain's own words. Two places
    deciding what counts as an adequate note is how they come to disagree.

    A 404 covers two cases the store cannot distinguish — no such alert for this
    account, and an alert already acknowledged — and the detail says so rather
    than asserting the first.
    """

    runtime = state.runtime
    fingerprint = _fingerprint_of(runtime)
    if not fingerprint:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "this backend is not bound to an account, so it cannot say whose alert "
                "this would be; nothing was acknowledged"
            ),
        )
    with runtime.database.sessions.begin() as session:
        try:
            done = alert_module.acknowledge(
                session,
                account_fingerprint=fingerprint,
                alert_id=alert_id,
                note=body.note,
                now=utc_now(),
            )
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
            ) from error
    if not done:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"no unacknowledged alert {alert_id} exists for this account; it may never "
                "have existed, or it may already have been acknowledged"
            ),
        )
    _logger.info(
        "Owner acknowledged alert %d",
        alert_id,
        extra={"event": "terminal_alert_acknowledged", "alert_id": alert_id},
    )
    return AcknowledgeResult(
        acknowledged=True,
        alert_id=alert_id,
        detail=(
            "the acknowledgement is recorded and audited; the condition that raised the "
            "alert is unchanged and may raise it again"
        ),
    )


@router.post("/terminal/mandate/revoke", response_model=RevokeResult)
def revoke_mandate(body: RevokeRequest, request: Request, state: WriterDep) -> RevokeResult:
    """Stand the supervisor down. Owner-only, reasoned, audited, durable.

    See the module docstring for why this route is called out as a new
    authorization surface. Three behaviours are worth stating at the point of
    use:

    - An empty or whitespace reason is refused with 422 **before** anything is
      written. ``durable.revoke`` would have accepted it; a revocation nobody
      explained is not auditable, and the audit trail is the entire reason the
      row is marked rather than deleted.
    - A ``mandate_id`` that names a grant other than the one in force is refused
      with 409, and nothing is written. See :class:`RevokeRequest` for why the
      two can differ. This is not a safety gate — revoking the wrong mandate
      still only removes authority — it is what keeps the confirmation the owner
      typed and the act the chain records the same event.
    - No mandate in force is answered with ``revoked: false`` and 200, not a
      404. The owner asked for authority to be gone; it already was. That case
      stays a 200 even when a ``mandate_id`` was named, because nothing is
      recorded against any grant: there is no act here for the name to disagree
      with, and the body says outright that nothing was withdrawn.
    - Nothing in the running process is touched. Revocation is durable state
      that admission re-derives before every cycle, so a cycle already in flight
      finishes and the next one refuses. The detail says so, because an owner
      who has just revoked deserves to know what is still allowed to complete.
    """

    reason = body.reason.strip()
    if not reason:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "revoking a mandate requires a reason; an unexplained revocation cannot be "
                "told apart from a stray request in the audit trail"
            ),
        )

    runtime = state.runtime
    fingerprint = _fingerprint_of(runtime)
    if not fingerprint:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "this backend is not bound to an account, so it cannot say whose authority "
                "this would revoke; nothing was revoked"
            ),
        )
    in_force = _mandate_in_force(runtime, _autonomy_of(request))
    if in_force is None:
        return RevokeResult(
            revoked=False,
            detail=(
                "this backend has no mandate to revoke: none is loaded, so there is no "
                "grant here to withdraw. That is an answer, not a failure"
            ),
        )

    named = (body.mandate_id or "").strip()
    if named and named != in_force.mandate_id:
        _logger.warning(
            "Refused a revocation naming mandate %s while %s is in force",
            named,
            in_force.mandate_id,
            extra={
                "event": "terminal_mandate_revoke_conflict",
                "mandate_id": in_force.mandate_id,
                "named_mandate_id": named,
                "outcome": "conflict",
                "passed": False,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"the grant in force is {in_force.mandate_id}, not the {named} this request "
                "named; nothing was revoked. Re-read the mandate panel and confirm against "
                "the grant that is actually in force"
            ),
        )

    with runtime.database.sessions.begin() as session:
        revoked = durable.revoke(
            session,
            account_fingerprint=fingerprint,
            mandate_id=in_force.mandate_id,
            reason=reason,
            now=utc_now(),
        )
    _logger.warning(
        "Owner revoked mandate %s",
        in_force.mandate_id,
        extra={
            "event": "terminal_mandate_revoked",
            "mandate_id": in_force.mandate_id,
            "outcome": "revoked" if revoked else "nothing_in_force",
            "passed": revoked,
        },
    )
    if not revoked:
        return RevokeResult(
            revoked=False,
            mandate_id=in_force.mandate_id,
            detail=(
                "no unrevoked activation of this mandate existed, so nothing changed. It "
                "was never activated here, or it was already revoked"
            ),
        )
    return RevokeResult(
        revoked=True,
        mandate_id=in_force.mandate_id,
        detail=(
            "authority is withdrawn and the revocation is recorded in the authority chain. "
            "It survives a restart: the mandate on disk will not re-activate itself, and "
            "re-granting requires a new mandate_version. A cycle already in flight finishes; "
            "the next one is refused at admission"
        ),
    )


# ------------------------------------------------------------------ internals


def _fingerprint_of(runtime: AppRuntime) -> str:
    """The pseudonym this backend is scoped to, or empty when it is scoped to none.

    Derived the way ``build_autonomy_runtime`` derives it, so the terminal reads
    the rows the supervisor writes. Empty rather than raising: a backend with no
    bound account has nothing to show, and the read-models turn the emptiness
    into ``None`` — *unknown* — rather than into a zero.
    """

    account_id = runtime.order_management.account_id.strip()
    if not account_id:
        return ""
    return account_fingerprint(account_id)


def _autonomy_of(request: Request) -> AutonomyRuntime | None:
    """The autonomy runtime this process is driving, or ``None``.

    ``isinstance`` rather than duck typing: ``app.state`` is a namespace anything
    can write to, and a status bar that reported a tick schedule from an object
    that merely looked like one would be reporting a schedule nobody is keeping.
    ``None`` is a real answer here — a read-only backend never builds a runtime —
    and the read-models distinguish "not configured" from "configured and
    stopped".
    """

    candidate = getattr(request.app.state, "autonomy", None)
    return candidate if isinstance(candidate, AutonomyRuntime) else None


def _mandate_in_force(
    runtime: AppRuntime, autonomy: AutonomyRuntime | None
) -> AutonomyMandate | None:
    """The grant this backend is under: the runtime's first, the file second.

    See the module docstring for why that order is the safe one. Both arms can
    answer ``None``, and ``None`` means *no authority established here* — which
    the mandate panel states outright rather than rendering as an empty scope.
    """

    if autonomy is not None:
        return autonomy.mandate
    path = runtime.settings.autonomy_mandate_file
    if path is None:
        return None
    loaded = load_persistent_mandate(path)
    return None if loaded is None else loaded.mandate
