"""Pure operational-health evaluation for the backend observation surfaces.

The evaluator in this module performs no I/O and grants no authority.  It turns
one immutable fact snapshot into conservative liveness, service-readiness, and
new-exposure capability projections.  Order, risk, mandate, and submission code
must continue to derive their own permissions from their authoritative inputs.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from chronos.operations.clock import ClockFailureCode, ClockProvider, ClockState


class _HealthModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LivenessState(StrEnum):
    LIVE = "LIVE"


class ReadinessState(StrEnum):
    STARTING = "STARTING"
    READY = "READY"
    NOT_READY = "NOT_READY"


class CapabilityState(StrEnum):
    AVAILABLE = "AVAILABLE"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class ObservationState(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class TaskState(StrEnum):
    NOT_EXPECTED = "NOT_EXPECTED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPED_EXPECTED = "STOPPED_EXPECTED"
    FAILED = "FAILED"


class BackgroundTaskName(StrEnum):
    LEASE_HEARTBEAT = "lease_heartbeat"
    RECONCILIATION = "reconciliation"
    AUTONOMY = "autonomy"


class TaskFailureCode(StrEnum):
    CANCELLED_UNEXPECTEDLY = "cancelled_unexpectedly"
    EXITED_UNEXPECTEDLY = "exited_unexpectedly"
    RAISED = "raised"


class WriterRole(StrEnum):
    WRITER = "WRITER"
    READ_ONLY = "READ_ONLY"
    UNKNOWN = "UNKNOWN"


class StartupFaultCode(StrEnum):
    PROPOSER_REGISTRY_INVALID = "proposer_registry_invalid"
    EVIDENCE_POSTURE_INVALID = "evidence_posture_invalid"
    AUTONOMY_POSTURE_UNAUTHENTICATED = "autonomy_posture_unauthenticated"
    SUBMISSION_RECONCILIATION_FAILED = "submission_reconciliation_failed"
    AUTONOMY_WIRING_FAILED = "autonomy_wiring_failed"
    RECOVERY_UNVERIFIED = "recovery_unverified"


class ReasonCode(StrEnum):
    BACKEND_STARTING = "backend_starting"
    STORE_UNREADABLE = "store_unreadable"
    STORE_UNKNOWN = "store_unknown"
    STARTUP_DEGRADED = "startup_degraded"
    ROLE_UNKNOWN = "role_unknown"
    WRITER_LEASE_ABSENT = "writer_lease_absent"
    REQUIRED_TASK_STARTING = "required_task_starting"
    REQUIRED_TASK_FAILED = "required_task_failed"
    REQUIRED_TASK_STALE = "required_task_stale"
    REQUIRED_TASK_UNKNOWN = "required_task_unknown"
    BROKER_LOOP_DOWN = "broker_loop_down"
    BROKER_LOOP_UNKNOWN = "broker_loop_unknown"
    BROKER_CONNECTION_UNKNOWN = "broker_connection_unknown"
    BROKER_DISCONNECTED = "broker_disconnected"
    BROKER_OBSERVATION_STALE = "broker_observation_stale"
    RECONCILIATION_NOT_READY = "reconciliation_not_ready"
    RECONCILIATION_STALE = "reconciliation_stale"
    RECONCILIATION_UNKNOWN = "reconciliation_unknown"
    CLOCK_UNKNOWN = "clock_unknown"
    CLOCK_UNSYNCHRONIZED = "clock_unsynchronized"
    LANE_NOT_CONFIGURED = "lane_not_configured"
    KILL_SWITCH_ENGAGED = "kill_switch_engaged"
    KILL_SWITCH_UNKNOWN = "kill_switch_unknown"
    ARM_ABSENT = "arm_absent"
    ARM_UNKNOWN = "arm_unknown"
    MANDATE_ABSENT = "mandate_absent"
    MANDATE_INACTIVE = "mandate_inactive"
    MANDATE_UNKNOWN = "mandate_unknown"
    PROMOTION_ABSENT = "promotion_absent"
    PROMOTION_UNKNOWN = "promotion_unknown"


class TaskObservation(_HealthModel):
    name: BackgroundTaskName
    state: TaskState
    observed_at: AwareDatetime | None = None
    max_age_seconds: float = Field(gt=0)
    required_for_writer: bool = True
    failure_code: TaskFailureCode | None = None


class BrokerConnectionFact(_HealthModel):
    connected: bool | None = None
    connection_state: str | None = None
    observed_environment: str | None = None
    observed_at: AwareDatetime | None = None
    max_age_seconds: float = Field(default=300.0, gt=0)
    generation: int = Field(default=0, ge=0)


class ClockFact(_HealthModel):
    provider: ClockProvider = ClockProvider.DISABLED
    state: ClockState = ClockState.UNKNOWN
    observed_at: AwareDatetime | None = None
    max_age_seconds: float = Field(default=90.0, gt=0)
    maximum_error_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    maximum_allowed_error_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    failure_code: ClockFailureCode | None = ClockFailureCode.DISABLED
    generation: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_positive_evidence(self) -> ClockFact:
        if self.state is not ClockState.SYNCHRONIZED:
            return self
        if (
            self.provider is not ClockProvider.CHRONY
            or self.observed_at is None
            or self.maximum_error_seconds is None
            or self.maximum_allowed_error_seconds is None
            or self.maximum_error_seconds > self.maximum_allowed_error_seconds
            or self.failure_code is not None
        ):
            raise ValueError(
                "SYNCHRONIZED clock state requires current chrony evidence at or below "
                "the configured maximum error"
            )
        return self


class OperationalFacts(_HealthModel):
    backend_initialized: bool = False
    writer_role: WriterRole = WriterRole.UNKNOWN
    store_readable: bool | None = None
    startup_faults: tuple[StartupFaultCode, ...] = ()
    tasks: tuple[TaskObservation, ...] = ()
    broker_loop_running: bool | None = None
    broker: BrokerConnectionFact = Field(default_factory=BrokerConnectionFact)
    reconciliation_status: str = "PENDING"
    reconciliation_generation: int = Field(default=0, ge=0)
    reconciliation_evidence_at: AwareDatetime | None = None
    reconciliation_max_age_seconds: float = Field(default=300.0, gt=0)
    paper_new_exposure_configured: bool = False
    live_new_exposure_configured: bool = False
    autonomous_new_exposure_configured: bool = False
    kill_switch_engaged: bool | None = None
    live_armed: bool | None = None
    mandate_active: bool | None = None
    promotion_present: bool | None = None
    clock: ClockFact = Field(default_factory=ClockFact)


class LivenessVerdict(_HealthModel):
    state: LivenessState = LivenessState.LIVE
    reasons: tuple[ReasonCode, ...] = ()


class ReadinessVerdict(_HealthModel):
    state: ReadinessState
    reasons: tuple[ReasonCode, ...] = ()


class CapabilityVerdict(_HealthModel):
    state: CapabilityState
    reasons: tuple[ReasonCode, ...] = ()


class TradingCapability(_HealthModel):
    paper_new_exposure: CapabilityVerdict
    live_new_exposure: CapabilityVerdict
    autonomous_new_exposure: CapabilityVerdict


class TaskObservationReport(_HealthModel):
    name: BackgroundTaskName
    state: TaskState
    observation_state: ObservationState
    age_seconds: float | None = None
    required_for_writer: bool
    failure_code: TaskFailureCode | None = None


class BrokerObservationReport(_HealthModel):
    observation_state: ObservationState
    connected: bool | None = None
    connection_state: str | None = None
    observed_environment: str | None = None
    age_seconds: float | None = None
    generation: int = Field(ge=0)


class ReconciliationObservationReport(_HealthModel):
    status: str
    observation_state: ObservationState
    age_seconds: float | None = None
    generation: int = Field(ge=0)


class ClockObservationReport(_HealthModel):
    provider: ClockProvider
    observation_state: ObservationState
    age_seconds: float | None = None
    maximum_error_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    maximum_allowed_error_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    failure_code: ClockFailureCode | None = None
    generation: int = Field(ge=0)


class OperationalObservations(_HealthModel):
    writer_role: WriterRole
    store_readable: bool | None
    startup_faults: tuple[StartupFaultCode, ...]
    tasks: tuple[TaskObservationReport, ...]
    broker_loop_running: bool | None
    broker: BrokerObservationReport
    reconciliation: ReconciliationObservationReport
    clock: ClockState
    clock_evidence: ClockObservationReport


class OperationalHealth(_HealthModel):
    assessed_at: AwareDatetime
    liveness: LivenessVerdict
    service_readiness: ReadinessVerdict
    trading_capability: TradingCapability
    observations: OperationalObservations


def _ordered(reasons: set[ReasonCode]) -> tuple[ReasonCode, ...]:
    return tuple(sorted(reasons, key=lambda reason: reason.value))


def _observation(
    observed_at: datetime | None,
    *,
    max_age_seconds: float,
    now: datetime,
) -> tuple[ObservationState, float | None]:
    if observed_at is None:
        return ObservationState.UNKNOWN, None
    age = (now - observed_at).total_seconds()
    if age < 0:
        return ObservationState.UNKNOWN, None
    rounded_age = round(age, 3)
    if age > max_age_seconds:
        return ObservationState.STALE, rounded_age
    return ObservationState.CURRENT, rounded_age


def _capability(
    known_blockers: set[ReasonCode], unknown_blockers: set[ReasonCode]
) -> CapabilityVerdict:
    reasons = _ordered(known_blockers | unknown_blockers)
    if known_blockers:
        return CapabilityVerdict(state=CapabilityState.BLOCKED, reasons=reasons)
    if unknown_blockers:
        return CapabilityVerdict(state=CapabilityState.UNKNOWN, reasons=reasons)
    return CapabilityVerdict(state=CapabilityState.AVAILABLE)


def evaluate_operational_health(
    facts: OperationalFacts,
    *,
    now: datetime,
) -> OperationalHealth:
    """Evaluate one immutable snapshot; unknown or stale evidence never strengthens it."""

    readiness_reasons: set[ReasonCode] = set()
    common_known: set[ReasonCode] = set()
    common_unknown: set[ReasonCode] = set()

    if not facts.backend_initialized:
        readiness_state = ReadinessState.STARTING
        readiness_reasons.add(ReasonCode.BACKEND_STARTING)
        common_known.add(ReasonCode.BACKEND_STARTING)
    else:
        readiness_state = ReadinessState.READY

    if facts.store_readable is False:
        readiness_reasons.add(ReasonCode.STORE_UNREADABLE)
        common_known.add(ReasonCode.STORE_UNREADABLE)
    elif facts.store_readable is None:
        readiness_reasons.add(ReasonCode.STORE_UNKNOWN)
        common_unknown.add(ReasonCode.STORE_UNKNOWN)

    if facts.startup_faults:
        readiness_reasons.add(ReasonCode.STARTUP_DEGRADED)
        common_known.add(ReasonCode.STARTUP_DEGRADED)

    if facts.writer_role is WriterRole.READ_ONLY:
        common_known.add(ReasonCode.WRITER_LEASE_ABSENT)
    elif facts.writer_role is WriterRole.UNKNOWN:
        common_unknown.add(ReasonCode.ROLE_UNKNOWN)

    task_reports: list[TaskObservationReport] = []
    for task in sorted(facts.tasks, key=lambda item: item.name.value):
        observation_state, age = _observation(
            task.observed_at,
            max_age_seconds=task.max_age_seconds,
            now=now,
        )
        task_reports.append(
            TaskObservationReport(
                name=task.name,
                state=task.state,
                observation_state=observation_state,
                age_seconds=age,
                required_for_writer=task.required_for_writer,
                failure_code=task.failure_code,
            )
        )
        if facts.writer_role is not WriterRole.WRITER or not task.required_for_writer:
            continue
        if task.state is TaskState.STARTING:
            readiness_reasons.add(ReasonCode.REQUIRED_TASK_STARTING)
            common_known.add(ReasonCode.REQUIRED_TASK_STARTING)
        elif task.state is not TaskState.RUNNING:
            readiness_reasons.add(ReasonCode.REQUIRED_TASK_FAILED)
            common_known.add(ReasonCode.REQUIRED_TASK_FAILED)
        elif observation_state is ObservationState.STALE:
            readiness_reasons.add(ReasonCode.REQUIRED_TASK_STALE)
            common_unknown.add(ReasonCode.REQUIRED_TASK_STALE)
        elif observation_state is ObservationState.UNKNOWN:
            readiness_reasons.add(ReasonCode.REQUIRED_TASK_UNKNOWN)
            common_unknown.add(ReasonCode.REQUIRED_TASK_UNKNOWN)

    if readiness_state is not ReadinessState.STARTING and readiness_reasons:
        readiness_state = ReadinessState.NOT_READY

    if facts.broker_loop_running is False:
        common_known.add(ReasonCode.BROKER_LOOP_DOWN)
    elif facts.broker_loop_running is None:
        common_unknown.add(ReasonCode.BROKER_LOOP_UNKNOWN)

    broker_state, broker_age = _observation(
        facts.broker.observed_at,
        max_age_seconds=facts.broker.max_age_seconds,
        now=now,
    )
    if broker_state is ObservationState.STALE:
        common_unknown.add(ReasonCode.BROKER_OBSERVATION_STALE)
    elif broker_state is ObservationState.UNKNOWN or facts.broker.connected is None:
        common_unknown.add(ReasonCode.BROKER_CONNECTION_UNKNOWN)
    elif not facts.broker.connected:
        common_known.add(ReasonCode.BROKER_DISCONNECTED)

    reconciliation_state, reconciliation_age = _observation(
        facts.reconciliation_evidence_at,
        max_age_seconds=facts.reconciliation_max_age_seconds,
        now=now,
    )
    if facts.reconciliation_status != "RECONCILED":
        common_known.add(ReasonCode.RECONCILIATION_NOT_READY)
    elif reconciliation_state is ObservationState.STALE:
        common_unknown.add(ReasonCode.RECONCILIATION_STALE)
    elif reconciliation_state is ObservationState.UNKNOWN:
        common_unknown.add(ReasonCode.RECONCILIATION_UNKNOWN)

    clock_observation_state, clock_age = _observation(
        facts.clock.observed_at,
        max_age_seconds=facts.clock.max_age_seconds,
        now=now,
    )
    effective_clock_state = (
        facts.clock.state
        if clock_observation_state is ObservationState.CURRENT
        else ClockState.UNKNOWN
    )
    if effective_clock_state is ClockState.UNSYNCHRONIZED:
        common_known.add(ReasonCode.CLOCK_UNSYNCHRONIZED)
    elif effective_clock_state is ClockState.UNKNOWN:
        common_unknown.add(ReasonCode.CLOCK_UNKNOWN)

    paper_known = set(common_known)
    paper_unknown = set(common_unknown)
    if not facts.paper_new_exposure_configured:
        paper_known.add(ReasonCode.LANE_NOT_CONFIGURED)

    live_known = set(common_known)
    live_unknown = set(common_unknown)
    if not facts.live_new_exposure_configured:
        live_known.add(ReasonCode.LANE_NOT_CONFIGURED)
    if facts.kill_switch_engaged is True:
        live_known.add(ReasonCode.KILL_SWITCH_ENGAGED)
    elif facts.kill_switch_engaged is None:
        live_unknown.add(ReasonCode.KILL_SWITCH_UNKNOWN)
    if facts.live_armed is False:
        live_known.add(ReasonCode.ARM_ABSENT)
    elif facts.live_armed is None:
        live_unknown.add(ReasonCode.ARM_UNKNOWN)

    autonomous_known = set(common_known)
    autonomous_unknown = set(common_unknown)
    if not facts.autonomous_new_exposure_configured:
        autonomous_known.add(ReasonCode.MANDATE_ABSENT)
    if facts.mandate_active is False:
        autonomous_known.add(ReasonCode.MANDATE_INACTIVE)
    elif facts.mandate_active is None:
        autonomous_unknown.add(ReasonCode.MANDATE_UNKNOWN)
    if facts.promotion_present is False:
        autonomous_known.add(ReasonCode.PROMOTION_ABSENT)
    elif facts.promotion_present is None:
        autonomous_unknown.add(ReasonCode.PROMOTION_UNKNOWN)

    return OperationalHealth(
        assessed_at=now,
        liveness=LivenessVerdict(),
        service_readiness=ReadinessVerdict(
            state=readiness_state,
            reasons=_ordered(readiness_reasons),
        ),
        trading_capability=TradingCapability(
            paper_new_exposure=_capability(paper_known, paper_unknown),
            live_new_exposure=_capability(live_known, live_unknown),
            autonomous_new_exposure=_capability(autonomous_known, autonomous_unknown),
        ),
        observations=OperationalObservations(
            writer_role=facts.writer_role,
            store_readable=facts.store_readable,
            startup_faults=tuple(sorted(facts.startup_faults, key=lambda item: item.value)),
            tasks=tuple(task_reports),
            broker_loop_running=facts.broker_loop_running,
            broker=BrokerObservationReport(
                observation_state=broker_state,
                connected=facts.broker.connected,
                connection_state=facts.broker.connection_state,
                observed_environment=facts.broker.observed_environment,
                age_seconds=broker_age,
                generation=facts.broker.generation,
            ),
            reconciliation=ReconciliationObservationReport(
                status=facts.reconciliation_status,
                observation_state=reconciliation_state,
                age_seconds=reconciliation_age,
                generation=facts.reconciliation_generation,
            ),
            clock=effective_clock_state,
            clock_evidence=ClockObservationReport(
                provider=facts.clock.provider,
                observation_state=clock_observation_state,
                age_seconds=clock_age,
                maximum_error_seconds=facts.clock.maximum_error_seconds,
                maximum_allowed_error_seconds=facts.clock.maximum_allowed_error_seconds,
                failure_code=facts.clock.failure_code,
                generation=facts.clock.generation,
            ),
        ),
    )
