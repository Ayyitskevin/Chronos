"""Chronos backend application factory.

The backend is the sole owner of the broker connection and (from Milestone 5)
the sole order-writing authority. It binds to loopback only; every endpoint
except ``/health`` requires the local API token. If another backend already
holds the single-writer lease for the configured database, this instance
starts **read-only** with inspection available and mutation refused.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from chronos.api.auth import load_or_create_token
from chronos.api.dependencies import BackendState
from chronos.api.routes.account import router as account_router
from chronos.api.routes.autonomy import router as autonomy_router
from chronos.api.routes.health import router as health_router
from chronos.api.routes.live import router as live_router
from chronos.api.routes.orders import router as orders_router
from chronos.api.routes.strategy import router as strategy_router
from chronos.runtime import build_runtime
from chronos.utils.locking import WriterLease

_logger = logging.getLogger("chronos.api")

#: The lease is renewed this many times per TTL, so the renewal interval is a
#: third of the TTL and ordinary scheduling jitter cannot let the lease lapse.
#: It is **not** a retry budget: a single failed renewal demotes immediately,
#: because a lease we could not renew may already have been taken by another
#: writer, and two processes believing they are authoritative is precisely the
#: split-brain R-24 exists to prevent. Fail closed on the first failure.
_RENEWALS_PER_TTL = 3


async def _heartbeat_lease(state: BackendState, lease: WriterLease, period: float) -> None:
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
    try:
        read_only = not lease.acquire()
        if read_only:
            holder = lease.state().holder
            _logger.warning(
                "Another Chronos backend holds the writer lease; starting READ-ONLY",
                extra={"event": "backend_read_only", "lease_holder": holder},
            )
        app.state.backend = BackendState(
            runtime=runtime,
            lease=None if read_only else lease,
            read_only=read_only,
        )
        app.state.api_token = load_or_create_token(runtime.settings.backend_token_file)
        if not read_only:
            # R-24: the boundary re-checks lease ownership in the database
            # immediately before transmitting, instead of trusting the
            # startup-time `writer_lease_held` flag.
            backend_state = app.state.backend

            def _still_the_writer() -> bool:
                """Both halves must hold, and either alone is insufficient.

                ``lease.holds()`` alone would let a submission already in flight
                transmit after the heartbeat demoted this process, because the
                demotion is a local decision the database row does not record
                until the lease actually lapses. ``read_only`` alone would trust
                a startup-time flag, which is the R-24 defect itself.
                """

                return not backend_state.read_only and lease.holds()

            runtime.order_management.submission_boundary.bind_lease_verifier(_still_the_writer)
            # A writer publishes submission readiness only after both restart-order
            # recovery and full portfolio reconciliation complete against broker
            # truth. Read-only backends cannot persist recovery and stay PENDING.
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
    if not read_only:
        # R-24: without this the lease silently lapses after its TTL.
        period = lease.ttl.total_seconds() / _RENEWALS_PER_TTL
        heartbeat = asyncio.create_task(_heartbeat_lease(app.state.backend, lease, period))
    try:
        yield
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        if not read_only:
            lease.release()
        runtime.close()


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
    return app
