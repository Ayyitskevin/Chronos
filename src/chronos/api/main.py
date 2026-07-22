"""Chronos backend application factory.

The backend is the sole owner of the broker connection and (from Milestone 5)
the sole order-writing authority. It binds to loopback only; every endpoint
except ``/health`` requires the local API token. If another backend already
holds the single-writer lease for the configured database, this instance
starts **read-only** with inspection available and mutation refused.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from chronos.api.auth import load_or_create_token
from chronos.api.dependencies import BackendState
from chronos.api.routes.account import router as account_router
from chronos.api.routes.health import router as health_router
from chronos.api.routes.live import router as live_router
from chronos.api.routes.orders import router as orders_router
from chronos.api.routes.strategy import router as strategy_router
from chronos.runtime import build_runtime
from chronos.utils.locking import WriterLease

_logger = logging.getLogger("chronos.api")


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
    try:
        yield
    finally:
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
    return app
