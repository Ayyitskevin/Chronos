"""Chronos backend application factory.

The backend is the sole owner of the broker connection and (from Milestone 5)
the sole order-writing authority. It binds to loopback only; every endpoint
except ``/health`` requires the local API token. If another backend already
holds the single-writer lease for the configured database, this instance
starts **read-only** with inspection available and mutation refused.

From M8a it also serves the operator terminal's browser client as static files
(ADR-0018 §6). Two properties of that are worth stating where the app is built,
because both are easy to lose in a later edit:

- **The shell is served without the API token; the data behind it is not.** A
  browser cannot put a header on a document load, so ``/terminal/app`` is
  reachable unauthenticated — on loopback, from this process, from files that
  ship with the package. Every ``/terminal/*`` data route still requires a
  credential. Since M8b that credential may be either the ``X-Chronos-Token``
  header, as before, or a session cookie obtained by presenting that same token
  to ``POST /terminal/session`` — which is how the shell authenticates itself
  once loaded. **The cookie is scoped to ``/terminal``**, so the browser never
  attaches it to the order plane; that scope is the property that makes an
  ambient credential acceptable in this process at all, and
  :mod:`chronos.api.terminal_session` explains why the other flags do not
  substitute for it.
- **Same-origin serving is what removes CORS from the picture entirely.** There
  is no cross-origin request to permit, no preflight, and no credential handed
  to JavaScript loaded from somewhere else. The loopback binding and the
  existing token posture are unchanged by the terminal existing.
- **The browser is told what the page is allowed to do, not just trusted to
  behave.** Every ``/terminal`` response carries a Content-Security-Policy and
  ``nosniff`` (:class:`_TerminalSecurityHeaders`). The client already refuses
  every HTML sink, but that refusal lives in hand-written DOM code and a
  structural test over it; the header is the layer that survives the day one of
  those regresses.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chronos.api.auth import load_or_create_token, load_proposer_auth
from chronos.api.autonomy_wiring import (
    UnauthenticatedSubmittingMandate,
    UnsafeMandateFile,
    alert_broken_evidence_posture,
    alert_invalid_proposer_registry,
    autonomy_tick_task,
    build_autonomy_runtime,
    evidence_binding_in_force,
    evidence_posture_is_broken,
)
from chronos.api.dependencies import BackendState
from chronos.api.reconciliation_loop import reconciliation_task
from chronos.api.routes.account import router as account_router
from chronos.api.routes.autonomy import router as autonomy_router
from chronos.api.routes.health import router as health_router
from chronos.api.routes.live import router as live_router
from chronos.api.routes.orders import router as orders_router
from chronos.api.routes.strategy import router as strategy_router
from chronos.api.routes.terminal import router as terminal_router
from chronos.api.routes.terminal import session_router as terminal_session_router
from chronos.api.task_observations import TaskObservationRegistry
from chronos.operations.clock import (
    ChronyClockSampler,
    ClockHealthCache,
    ClockProvider,
    clock_health_monitor,
    refresh_clock_health,
)
from chronos.operations.health import BackgroundTaskName, StartupFaultCode
from chronos.orders.recovery_hold import evaluate_startup_recovery_hold
from chronos.orders.state_generation import StateGenerationMarker
from chronos.runtime import build_runtime
from chronos.supervisor.proposers import UnsafeProposerRegistry
from chronos.utils.locking import WriterLease
from chronos.utils.time import utc_now

_logger = logging.getLogger("chronos.api")

#: The terminal client's directory, derived from this file's own location and
#: from nothing else. It is not a setting, not an environment variable, and not
#: influenced by any request — so there is no operator-supplied path to validate
#: and no traversal input to sanitize. ``StaticFiles`` additionally refuses any
#: resolved path outside the directory it was given, which is the guarantee that
#: matters once a URL is in play.
_TERMINAL_CLIENT_DIR = Path(__file__).resolve().parent.parent / "terminal" / "static"

#: The security headers every ``/terminal`` response carries, pre-encoded because
#: they are constants and the middleware below runs on every one of those
#: responses.
#:
#: The policy is as closed as a policy can be while still describing this page.
#: The shell loads one stylesheet and one module, both from this origin, and
#: nothing else — ``index.html`` says so in its own header and
#: ``tests/safety/test_terminal_client_has_no_html_sinks.py`` proves it — so
#: ``default-src 'none'`` with ``'self'`` for script, style and connect costs
#: exactly zero functionality, and ``img-src``, ``base-uri``, ``form-action`` and
#: ``frame-ancestors`` close surfaces the page never uses at all.
#:
#: Why it exists when the client already assigns nothing but ``textContent``:
#: that property is enforced by a structural test over hand-written DOM code, and
#: a test is a claim about the file as it stands today. The header is what makes
#: the *next* ``innerHTML`` inert rather than exploitable. That distinction earns
#: its keep as of M8b: this page now **does** hold a credential, so script
#: executing here can act as the operator against ``/terminal/*``. Two things
#: bound what that is worth to an attacker — the session cookie's ``/terminal``
#: path scope, which keeps it away from ``/orders/*`` entirely, and this policy,
#: which is what stops the script from running in the first place.
#:
#: ``nosniff`` is here for the same reason one layer down. The terminal serves
#: operator-authored notes and worker-derived narrative inside JSON, and a
#: browser that sniffed one of those bodies into ``text/html`` would render it as
#: a document on this origin, which is where the policy above would then apply.
_TERMINAL_SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (
        b"content-security-policy",
        b"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        b"img-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    ),
    (b"x-content-type-options", b"nosniff"),
)

_TERMINAL_SECURITY_HEADER_NAMES = frozenset(name for name, _ in _TERMINAL_SECURITY_HEADERS)

#: The lease is renewed this many times per TTL, so the renewal interval is a
#: third of the TTL and ordinary scheduling jitter cannot let the lease lapse.
#: It is **not** a retry budget: a single failed renewal demotes immediately,
#: because a lease we could not renew may already have been taken by another
#: writer, and two processes believing they are authoritative is precisely the
#: split-brain R-24 exists to prevent. Fail closed on the first failure.
_RENEWALS_PER_TTL = 3


async def _heartbeat_lease(
    state: BackendState,
    lease: WriterLease,
    period: float,
    *,
    on_progress: Callable[[], None] | None = None,
) -> None:
    """Renew the single-writer lease, and demote to read-only if it is lost.

    RISK_REGISTER R-24: before this existed, ``WriterLease.renew()`` had no
    production caller at all. The lease therefore expired after its 30-second
    TTL while this process went on believing it was the writer — a second
    backend could take the lease and both would consider themselves authoritative.
    Losing the lease is not recoverable by re-acquiring it here: another writer
    may already have acted on it, so this process demotes itself permanently and
    an operator restarts it deliberately.
    """

    while True:
        await asyncio.sleep(period)
        try:
            renewed = await asyncio.to_thread(lease.renew)
        except Exception:
            _logger.exception(
                "Writer-lease renewal raised; demoting this backend to READ-ONLY",
                extra={"event": "writer_lease_renew_error"},
            )
            renewed = False
        if renewed:
            if on_progress is not None:
                on_progress()
            continue
        state.read_only = True
        state.lease = None
        _logger.error(
            "Lost the single-writer lease; this backend is now READ-ONLY. Order "
            "submission, modification, cancellation, manual resolution, arming and "
            "kill-switch disengagement are disabled until restart. Engaging the kill "
            "switch and disarming remain available: both only ever remove authority.",
            extra={"event": "writer_lease_lost", "outcome": "read_only", "passed": False},
        )
        return


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime = build_runtime(register_atexit=False)
    lease = WriterLease(
        runtime.database.sessions,
        holder=f"backend:{runtime.settings.backend_host}:{runtime.settings.backend_port}",
    )
    # Everything between the lease acquire and the yield must be cleaned up if it
    # raises — otherwise a failure here (e.g. an unwritable token dir) would leak
    # the runtime AND leave the single-writer lease held by a dead process, so a
    # restart would boot read-only.
    read_only = True
    clock_sampler: ChronyClockSampler | None = None
    try:
        read_only = not lease.acquire()
        if read_only:
            holder = lease.state().holder
            _logger.warning(
                "Another Chronos backend holds the writer lease; starting READ-ONLY",
                extra={"event": "backend_read_only", "lease_holder": holder},
            )
        task_observations = TaskObservationRegistry()
        clock_provider = ClockProvider(runtime.settings.clock_health_provider)
        clock_health = ClockHealthCache(
            provider=clock_provider,
            maximum_allowed_error_seconds=runtime.settings.clock_health_maximum_error_seconds,
        )
        app.state.backend = BackendState(
            runtime=runtime,
            lease=None if read_only else lease,
            read_only=read_only,
            task_observations=task_observations,
            clock_health=clock_health,
        )
        backend_state = app.state.backend
        if runtime.settings.clock_health_provider == "chrony":
            maximum_error = runtime.settings.clock_health_maximum_error_seconds
            assert maximum_error is not None  # Settings validation makes this structural.
            clock_sampler = ChronyClockSampler(
                maximum_allowed_error_seconds=maximum_error,
                timeout_seconds=runtime.settings.clock_health_command_timeout_seconds,
            )
            # Both writer and read-only services observe the same host clock.
            # Sampling stays inside the startup cleanup guard: cancellation or
            # an unexpected failure here must release the lease and runtime.
            await refresh_clock_health(backend_state.clock_health, clock_sampler)
        app.state.api_token = load_or_create_token(runtime.settings.backend_token_file)
        # ADR-0023: the proposal route's authentication posture is resolved
        # once, at startup, from the owner's registry file — for every
        # backend, read-only included, because refusing correctly is not a
        # writer privilege. A configured-but-broken registry refuses all
        # proposals rather than falling back to the token posture, and the
        # owner hears about it the way they hear about a broken mandate.
        proposer_auth = load_proposer_auth(runtime.settings.autonomy_proposers_file)
        app.state.proposer_auth = proposer_auth
        if proposer_auth.configured and proposer_auth.registry is None:
            # Two different operator problems behind one refusal: a grant
            # document that does not parse, and a file this process may not
            # trust at all (symlinked, foreign-owned, group-writable). Both
            # refuse every proposal; only the second says someone else could
            # have written the document that says who may propose.
            if proposer_auth.unsafe:
                backend_state.note_startup_fault(StartupFaultCode.AUTHORITY_FILE_UNSAFE)
            backend_state.note_startup_fault(StartupFaultCode.PROPOSER_REGISTRY_INVALID)
            _logger.error(
                "Proposer registry is configured but unreadable or invalid; every "
                "proposal will refuse until the file is fixed",
                extra={
                    "event": "proposer_registry_invalid",
                    "passed": False,
                    "unsafe_file": proposer_auth.unsafe,
                },
            )
            if not read_only:
                alert_invalid_proposer_registry(runtime)
        # ADR-0028: a bundle is issued *to* a credential, so turning evidence
        # binding on without a proposer registry names no author to issue to.
        # It refuses loudly rather than falling back — ADR-0023's posture rule,
        # applied to the setting that arrived after it.
        app.state.evidence_binding = evidence_binding_in_force(runtime.settings)
        app.state.evidence_posture_broken = evidence_posture_is_broken(runtime.settings)
        if app.state.evidence_posture_broken:
            backend_state.note_startup_fault(StartupFaultCode.EVIDENCE_POSTURE_INVALID)
            _logger.error(
                "AUTONOMY_EVIDENCE_BUNDLES is set with no AUTONOMY_PROPOSERS_FILE; every "
                "proposal will refuse until one is configured or the other unset",
                extra={"event": "evidence_posture_invalid", "passed": False},
            )
            if not read_only:
                alert_broken_evidence_posture(runtime)
        if not read_only:
            # ADR-0054: before anything is bound or published, ask whether this
            # boot can prove it does not follow a restore. Writer-only: seeding
            # the first witness is a write, and a read-only backend already
            # satisfies everything a hold enforces.
            backend_state.recovery_hold = evaluate_startup_recovery_hold(
                runtime.database.sessions,
                StateGenerationMarker.beside(runtime.settings.live_kill_switch_file),
                state_directory=runtime.settings.live_kill_switch_file.parent,
                now=utc_now(),
            )
            if backend_state.recovery_hold is not None:
                backend_state.note_startup_fault(StartupFaultCode.RECOVERY_UNVERIFIED)
                _logger.error(
                    "This boot cannot prove it does not follow a restore (%s); the "
                    "backend is read-only and unreconciled until an operator "
                    "acknowledges it with a note",
                    backend_state.recovery_hold.reason.value,
                    extra={
                        "event": "recovery_hold_engaged",
                        "recovery_hold_reason": backend_state.recovery_hold.reason.value,
                        "outcome": "locked",
                        "passed": False,
                    },
                )

            # R-24: the boundary re-checks lease ownership in the database
            # immediately before transmitting, instead of trusting the
            # startup-time `writer_lease_held` flag.
            def _still_the_writer() -> bool:
                """Both halves must hold, and either alone is insufficient.

                ``lease.holds()`` alone would let a submission already in flight
                transmit after the heartbeat demoted this process, because the
                demotion is a local decision the database row does not record
                until the lease actually lapses. ``read_only`` alone would trust
                a startup-time flag, which is the R-24 defect itself.
                """

                return backend_state.may_write and lease.holds()

            runtime.order_management.submission_boundary.bind_lease_verifier(_still_the_writer)
            # A writer publishes submission readiness only after both restart-order
            # recovery and full portfolio reconciliation complete against broker
            # truth. Read-only backends cannot persist recovery and stay PENDING.
            if backend_state.recovery_hold is not None:
                _logger.warning(
                    "Skipping startup reconciliation under a recovery hold; submission "
                    "readiness stays PENDING",
                    extra={"event": "submission_reconciliation_skipped", "outcome": "locked"},
                )
            else:
                try:
                    report = runtime.reconcile_submission_readiness()
                    readiness = report.readiness
                    log = _logger.info if readiness.ready else _logger.warning
                    log(
                        "Submission reconciliation finished %s "
                        "(applied=%d unresolved=%d remaining_active=%d)",
                        readiness.status.value,
                        len(report.restart.applied_updates),
                        len(report.restart.unresolved),
                        len(report.restart.remaining_active),
                        extra={
                            "event": "submission_reconciliation_finished",
                            "reconciliation_status": readiness.status.value,
                            "applied_count": len(report.restart.applied_updates),
                            "outcome": "ready" if readiness.ready else "locked",
                            "passed": readiness.ready,
                            "proven_count": len(report.restart.proven),
                            "unresolved_count": len(report.restart.unresolved),
                            "remaining_active_count": len(report.restart.remaining_active),
                        },
                    )
                except Exception as error:
                    backend_state.note_startup_fault(
                        StartupFaultCode.SUBMISSION_RECONCILIATION_FAILED
                    )
                    _logger.error(
                        "Submission reconciliation failed; submission remains locked "
                        "while inspection, cancellation, and recovery stay available",
                        extra={
                            "event": "submission_reconciliation_failed",
                            "error_type": type(error).__name__,
                            "outcome": "locked",
                            "passed": False,
                        },
                    )
    except BaseException:
        if not read_only:
            try:
                lease.release()
            except Exception:
                _logger.exception(
                    "Failed to release the writer lease during aborted startup",
                    extra={"event": "backend_startup_lease_release_failed"},
                )
        runtime.close()
        raise
    heartbeat: asyncio.Task[None] | None = None
    autonomy_task: asyncio.Task[None] | None = None
    reconcile_task: asyncio.Task[None] | None = None
    clock_task: asyncio.Task[None] | None = None
    if clock_sampler is not None:
        clock_task = asyncio.create_task(
            clock_health_monitor(
                app.state.backend.clock_health,
                clock_sampler,
                poll_interval_seconds=runtime.settings.clock_health_poll_interval_seconds,
            )
        )
    if not read_only:
        task_observations = app.state.backend.task_observations
        # R-24: without this the lease silently lapses after its TTL.
        period = lease.ttl.total_seconds() / _RENEWALS_PER_TTL
        heartbeat_age = max(period * 2, 1.0)
        task_observations.starting(
            BackgroundTaskName.LEASE_HEARTBEAT,
            max_age_seconds=heartbeat_age,
        )
        heartbeat = asyncio.create_task(
            _heartbeat_lease(
                app.state.backend,
                lease,
                period,
                on_progress=lambda: task_observations.progress(BackgroundTaskName.LEASE_HEARTBEAT),
            )
        )
        task_observations.bind(
            BackgroundTaskName.LEASE_HEARTBEAT,
            heartbeat,
            max_age_seconds=heartbeat_age,
        )
        # ADR-0020: re-arm submission readiness on the owner-frozen cadence.
        # Writer-only, because a read-only backend cannot persist recovery and
        # must stay PENDING. Without this the first opening order of the process
        # consumes readiness and nothing ever re-arms it.
        reconciliation_age = (
            max(
                runtime.settings.reconciliation_interval_active_seconds,
                runtime.settings.reconciliation_interval_idle_seconds,
                runtime.settings.reconciliation_interval_closed_seconds,
            )
            + 60.0
        )
        # ADR-0054: not under a recovery hold. The refresher would re-arm exactly
        # the readiness the hold exists to withhold, so "unreconciled" has to mean
        # the task does not run, not merely that startup skipped one cycle.
        recovery_held = app.state.backend.recovery_hold is not None
        if recovery_held:
            task_observations.not_expected(
                BackgroundTaskName.RECONCILIATION,
                max_age_seconds=reconciliation_age,
            )
        else:
            task_observations.starting(
                BackgroundTaskName.RECONCILIATION,
                max_age_seconds=reconciliation_age,
            )
            reconcile_task = asyncio.create_task(
                reconciliation_task(
                    runtime,
                    on_progress=lambda: task_observations.progress(
                        BackgroundTaskName.RECONCILIATION
                    ),
                )
            )
            task_observations.bind(
                BackgroundTaskName.RECONCILIATION,
                reconcile_task,
                max_age_seconds=reconciliation_age,
            )
        # ADR-0017: the persistent mandate is the owner's standing grant. When
        # one is configured and valid, the autonomy runtime starts with the
        # backend — no per-boot ritual. No grant (the default) boots inert, and
        # a broken grant alerts and boots inert: absence of the owner act is
        # absence of the authority, never a crash of the process that can still
        # close positions.
        # ADR-0054: and not under a recovery hold either. `build_autonomy_runtime`
        # is where ADR-0017's auto-activation happens, so not calling it is what
        # keeps a restored mandate file from re-arming autonomy on this boot.
        # Not calling it is deliberately not the same as revoking: no revocation
        # row is written, so acknowledging the hold costs the owner nothing.
        autonomy = None
        if not recovery_held:
            try:
                # `is_writer` is read per submission, never captured: the lease
                # heartbeat can demote this process mid-session, and the autonomous
                # path must see that at gate 1 exactly as the human path does
                # (`state.writer` in routes/orders.py) rather than being turned away
                # later by the CAS-window re-check.
                autonomy_backend = app.state.backend
                autonomy = build_autonomy_runtime(
                    runtime,
                    process_generation=int(app.state.backend.lease is not None),
                    is_writer=lambda: bool(autonomy_backend.writer),
                )
            except UnauthenticatedSubmittingMandate:
                # ADR-0051: typed, not the generic wiring fault — the owner must be
                # able to tell "the posture is wrong" from "assembly crashed".
                app.state.backend.note_startup_fault(
                    StartupFaultCode.AUTONOMY_POSTURE_UNAUTHENTICATED
                )
                _logger.critical(
                    "A submitting mandate met a static proposer posture; autonomy stays inert "
                    "until AUTONOMY_PROPOSERS_FILE and AUTONOMY_EVIDENCE_BUNDLES are configured "
                    "or the mandate is re-authored at SHADOW",
                    extra={"event": "autonomy_posture_unauthenticated", "passed": False},
                )
                autonomy = None
            except (UnsafeMandateFile, UnsafeProposerRegistry) as error:
                # ADR-0056, and ABOVE the generic handler on purpose: an unsafe
                # grant is assembly correctly REFUSING, not assembly crashing.
                # Reporting it as a wiring failure sends the owner looking for a
                # bug instead of at their file's permissions. Both grants note
                # the same fault because it is a property of the file; which one
                # it was is in this line and in the owner alert.
                app.state.backend.note_startup_fault(StartupFaultCode.AUTHORITY_FILE_UNSAFE)
                _logger.critical(
                    "An owner-authored grant is unsafe; autonomy stays inert: %s",
                    error,
                    extra={"event": "authority_file_unsafe", "passed": False},
                )
                autonomy = None
            except Exception:
                app.state.backend.note_startup_fault(StartupFaultCode.AUTONOMY_WIRING_FAILED)
                _logger.exception(
                    "Autonomy wiring failed; the backend continues without it",
                    extra={"event": "autonomy_wiring_failed"},
                )
                autonomy = None
        if autonomy is not None:
            app.state.autonomy = autonomy
            task_observations.starting(
                BackgroundTaskName.AUTONOMY,
                max_age_seconds=5.0,
            )
            autonomy_task = asyncio.create_task(
                autonomy_tick_task(
                    autonomy,
                    on_progress=lambda: task_observations.progress(BackgroundTaskName.AUTONOMY),
                )
            )
            task_observations.bind(
                BackgroundTaskName.AUTONOMY,
                autonomy_task,
                max_age_seconds=5.0,
            )
            _logger.info(
                "Autonomy runtime started under the persistent mandate",
                extra={"event": "autonomy_started"},
            )
        else:
            task_observations.not_expected(
                BackgroundTaskName.AUTONOMY,
                max_age_seconds=5.0,
            )
    else:
        for task_name in BackgroundTaskName:
            app.state.backend.task_observations.not_expected(
                task_name,
                max_age_seconds=5.0,
            )
    try:
        yield
    finally:
        if clock_task is not None:
            clock_task.cancel()
            with suppress(asyncio.CancelledError):
                await clock_task
        if autonomy_task is not None:
            app.state.backend.task_observations.expect_stop(BackgroundTaskName.AUTONOMY)
            autonomy_task.cancel()
            with suppress(asyncio.CancelledError):
                await autonomy_task
        if reconcile_task is not None:
            app.state.backend.task_observations.expect_stop(BackgroundTaskName.RECONCILIATION)
            reconcile_task.cancel()
            with suppress(asyncio.CancelledError):
                await reconcile_task
        if heartbeat is not None:
            app.state.backend.task_observations.expect_stop(BackgroundTaskName.LEASE_HEARTBEAT)
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        if not read_only:
            lease.release()
        runtime.close()


def _is_terminal_path(path: str) -> bool:
    """Whether a request path belongs to the terminal.

    Spelled out rather than ``path.startswith("/terminal")`` so that a future
    ``/terminals`` or ``/terminal-export`` route does not silently inherit a
    policy written for a document it is not.
    """

    return path == "/terminal" or path.startswith("/terminal/")


class _TerminalSecurityHeaders:
    """Attach :data:`_TERMINAL_SECURITY_HEADERS` to every ``/terminal`` response.

    A pure ASGI wrapper rather than ``BaseHTTPMiddleware``: the whole job is two
    response headers, and ``BaseHTTPMiddleware`` would buy that by putting an
    anyio task pair around every request in the application, including the order
    routes that have nothing to do with the terminal.

    Two alternatives were considered and rejected. A ``StaticFiles`` subclass
    overriding its response headers is narrower still, but it covers the assets
    and misses the shell route — and the shell is the one response a browser
    parses as a document, which is the only place a CSP does any work. Applying
    the headers app-wide was the other, and the headers would do no harm on the
    JSON routes; but ``default-src 'none'`` is a statement about a page, and a
    header nobody can point at a rendered document is a header nobody maintains.

    The path test happens before the response is touched, so a request outside
    ``/terminal`` passes through this object unchanged apart from one string
    comparison.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_terminal_path(scope.get("path", "")):
            await self._app(scope, receive, send)
            return

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                # Replace rather than append, so exactly one of each header is
                # emitted. Two ``Content-Security-Policy`` headers are enforced
                # as their intersection, which is a policy no reader of this file
                # ever wrote down.
                headers: list[tuple[bytes, bytes]] = [
                    (name, value)
                    for name, value in message.get("headers", ())
                    if name.lower() not in _TERMINAL_SECURITY_HEADER_NAMES
                ]
                headers.extend(_TERMINAL_SECURITY_HEADERS)
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, _send)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Chronos Backend",
        description=(
            "Local order-management and portfolio backend. Loopback-only; "
            "token-protected; single-writer."
        ),
        lifespan=_lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(health_router)
    app.include_router(account_router)
    app.include_router(strategy_router)
    app.include_router(orders_router)
    app.include_router(live_router)
    app.include_router(autonomy_router)
    app.include_router(terminal_router)
    # The login route that issues the terminal's session cookie. It is a
    # separate router because it cannot sit behind the credential it hands
    # out; it authenticates from its body instead (M8b).
    app.include_router(terminal_session_router)

    # The terminal client is served from this process, same-origin with the data
    # it draws — which is the whole reason there is no CORS middleware, no
    # preflight, and no bearer token handed to JavaScript from another origin
    # (ADR-0018 §6). Neither the shell route nor the asset mount below carries a
    # token dependency, on purpose: a browser cannot put a header on a document
    # load, and the module docstring states plainly what that costs.
    #
    # What the browser *is* told is what this page may do — see
    # ``_TERMINAL_SECURITY_HEADERS`` for why that header exists behind a client
    # that already refuses every HTML sink.
    app.add_middleware(_TerminalSecurityHeaders)

    # No path under /terminal redirects, and that is the point rather than a
    # detail. ``StaticFiles(html=True)`` used to serve the shell as a directory,
    # so ``GET /terminal/app`` answered 307 with an absolute ``Location`` built
    # from the request's ``Host`` header — path preserved and a slash appended,
    # so never an attacker-chosen destination, but the claim that the mount
    # "cannot be redirected by user input" was not true as written. A route that
    # returns one named file has no directory to resolve and nothing to redirect
    # to. Both spellings are registered because the entry point has been written
    # down with and without the trailing slash; a second route entry is cheaper
    # than a redirect and says less.
    def terminal_shell() -> FileResponse:
        """The shell document, addressed by name — never by resolving a directory."""

        return FileResponse(_TERMINAL_CLIENT_DIR / "index.html", media_type="text/html")

    for path in ("/terminal/app", "/terminal/app/"):
        app.add_api_route(path, terminal_shell, methods=["GET"], include_in_schema=False)

    # ``redirect_slashes`` goes with it. No route in this application is declared
    # with a trailing slash, so all it ever did was turn a mistyped URL into that
    # same Host-derived 307 — including ``GET /terminal/static``, which the shell
    # route above would otherwise have left behind. A URL that does not exist now
    # gets a 404, which is what it is.
    app.router.redirect_slashes = False

    # ``index.html`` hard-codes this prefix absolutely, and it must stay absolute:
    # a relative href resolves against the URL the *document* was served at, so it
    # would break under one of the two shell spellings above. ``html=False`` so
    # that no request resolves to a directory here either — ``/terminal/static/``
    # is not a page, and the shell has its own address.
    app.mount(
        "/terminal/static",
        StaticFiles(directory=_TERMINAL_CLIENT_DIR),
        name="terminal-static",
    )
    return app
