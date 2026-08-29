"""FastAPI fact collection for the pure operational-health evaluator."""

from __future__ import annotations

from datetime import datetime

from fastapi import Request

from chronos.api.dependencies import BackendState
from chronos.autonomy import SUBMITTING_AUTONOMY_MODES, AutonomyMode
from chronos.operations.health import (
    BrokerConnectionFact,
    ClockFact,
    OperationalFacts,
    OperationalHealth,
    WriterRole,
    evaluate_operational_health,
)
from chronos.supervisor import durable
from chronos.supervisor.runtime import AutonomyRuntime
from chronos.utils.identifiers import account_fingerprint
from chronos.utils.time import utc_now


def collect_operational_health(
    request: Request,
    *,
    now: datetime | None = None,
) -> OperationalHealth:
    """Capture one sanitized fact snapshot, then call the I/O-free evaluator."""

    moment = now or utc_now()
    candidate = getattr(request.app.state, "backend", None)
    if not isinstance(candidate, BackendState):
        return evaluate_operational_health(OperationalFacts(), now=moment)

    state = candidate
    runtime = state.runtime
    settings = runtime.settings
    store_readable = runtime.database.readable()
    readiness = runtime.reconciliation_readiness.snapshot()
    broker = runtime.connection.connection_observation()
    autonomy_candidate = getattr(request.app.state, "autonomy", None)
    autonomy = autonomy_candidate if isinstance(autonomy_candidate, AutonomyRuntime) else None
    mandate = None if autonomy is None else autonomy.mandate

    mandate_active: bool | None = None
    promotion_present: bool | None = None
    if mandate is not None:
        promotion_present = bool(mandate.promotions)
        try:
            fingerprint = account_fingerprint(runtime.order_management.account_id)
            with runtime.database.sessions.begin() as session:
                activation = durable.load_activation(
                    session,
                    account_fingerprint=fingerprint,
                    mandate=mandate,
                )
            mandate_active = (
                activation is not None and not activation.revoked and mandate.covers_instant(moment)
            )
        except Exception:
            store_readable = False
            mandate_active = None

    try:
        kill_switch_engaged: bool | None = runtime.live_kill_switch.read().engaged
    except Exception:
        kill_switch_engaged = None
    try:
        live_armed: bool | None = runtime.live_arming.state(now=moment).armed
    except Exception:
        live_armed = None

    autonomous_configured = False
    if mandate is not None and mandate.mode in SUBMITTING_AUTONOMY_MODES:
        if mandate.mode is AutonomyMode.PAPER_AUTONOMOUS:
            autonomous_configured = settings.transmission_possible
        else:
            autonomous_configured = settings.live_transmission_possible

    max_evidence_age = settings.reconciliation_max_evidence_age_seconds
    clock = state.clock_health.snapshot()
    facts = OperationalFacts(
        backend_initialized=True,
        writer_role=WriterRole.WRITER if state.writer else WriterRole.READ_ONLY,
        store_readable=store_readable,
        startup_faults=state.startup_faults,
        tasks=state.task_observations.snapshot(),
        broker_loop_running=runtime.connection.running,
        broker=BrokerConnectionFact(
            connected=broker.connected,
            connection_state=broker.connection_state,
            observed_environment=broker.observed_environment,
            observed_at=broker.observed_at,
            max_age_seconds=max_evidence_age,
            generation=broker.generation,
        ),
        reconciliation_status=readiness.status.value,
        reconciliation_generation=readiness.generation,
        reconciliation_evidence_at=readiness.reconciled_at,
        reconciliation_max_age_seconds=max_evidence_age,
        paper_new_exposure_configured=settings.transmission_possible,
        live_new_exposure_configured=settings.live_transmission_possible,
        autonomous_new_exposure_configured=autonomous_configured,
        kill_switch_engaged=kill_switch_engaged,
        live_armed=live_armed,
        mandate_active=mandate_active,
        promotion_present=promotion_present,
        clock=ClockFact(
            provider=clock.provider,
            state=clock.state,
            observed_at=clock.observed_at,
            max_age_seconds=settings.clock_health_observation_max_age_seconds,
            maximum_error_seconds=clock.maximum_error_seconds,
            maximum_allowed_error_seconds=clock.maximum_allowed_error_seconds,
            failure_code=clock.failure_code,
            generation=clock.generation,
        ),
    )
    return evaluate_operational_health(facts, now=moment)
