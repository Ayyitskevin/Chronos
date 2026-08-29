"""Truth table for the display-only operational-health evaluator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from chronos.operations.health import (
    BackgroundTaskName,
    BrokerConnectionFact,
    CapabilityState,
    ClockFact,
    ClockState,
    ObservationState,
    OperationalFacts,
    ReadinessState,
    ReasonCode,
    StartupFaultCode,
    TaskFailureCode,
    TaskObservation,
    TaskState,
    WriterRole,
    evaluate_operational_health,
)

NOW = datetime(2026, 8, 29, 16, 0, tzinfo=UTC)


def _running_task(name: BackgroundTaskName) -> TaskObservation:
    return TaskObservation(
        name=name,
        state=TaskState.RUNNING,
        observed_at=NOW,
        max_age_seconds=30,
    )


def _positive_facts() -> OperationalFacts:
    return OperationalFacts(
        backend_initialized=True,
        writer_role=WriterRole.WRITER,
        store_readable=True,
        tasks=(
            _running_task(BackgroundTaskName.LEASE_HEARTBEAT),
            _running_task(BackgroundTaskName.RECONCILIATION),
        ),
        broker_loop_running=True,
        broker=BrokerConnectionFact(
            connected=True,
            connection_state="CONNECTED",
            observed_environment="paper",
            observed_at=NOW,
            max_age_seconds=30,
            generation=7,
        ),
        reconciliation_status="RECONCILED",
        reconciliation_generation=11,
        reconciliation_evidence_at=NOW,
        reconciliation_max_age_seconds=30,
        paper_new_exposure_configured=True,
        live_new_exposure_configured=True,
        autonomous_new_exposure_configured=True,
        kill_switch_engaged=False,
        live_armed=True,
        mandate_active=True,
        promotion_present=True,
        clock=ClockFact(
            provider="chrony",
            state=ClockState.SYNCHRONIZED,
            observed_at=NOW,
            max_age_seconds=30,
            maximum_error_seconds=0.01,
            maximum_allowed_error_seconds=0.05,
            generation=3,
            failure_code=None,
        ),
    )


def _reasons(facts: OperationalFacts, lane: str = "paper_new_exposure") -> set[ReasonCode]:
    report = evaluate_operational_health(facts, now=NOW)
    verdict = getattr(report.trading_capability, lane)
    return set(verdict.reasons)


def test_uninitialized_backend_is_live_but_starting_and_incapable() -> None:
    report = evaluate_operational_health(OperationalFacts(), now=NOW)

    assert report.liveness.state == "LIVE"
    assert report.service_readiness.state is ReadinessState.STARTING
    assert report.service_readiness.reasons == (
        ReasonCode.BACKEND_STARTING,
        ReasonCode.STORE_UNKNOWN,
    )
    assert report.trading_capability.paper_new_exposure.state is CapabilityState.BLOCKED


def test_read_only_backend_can_serve_inspection_but_cannot_create_exposure() -> None:
    facts = _positive_facts().model_copy(
        update={
            "writer_role": WriterRole.READ_ONLY,
            "tasks": tuple(
                task.model_copy(
                    update={
                        "state": TaskState.NOT_EXPECTED,
                        "required_for_writer": False,
                    }
                )
                for task in _positive_facts().tasks
            ),
        }
    )

    report = evaluate_operational_health(facts, now=NOW)

    assert report.service_readiness.state is ReadinessState.READY
    for verdict in (
        report.trading_capability.paper_new_exposure,
        report.trading_capability.live_new_exposure,
        report.trading_capability.autonomous_new_exposure,
    ):
        assert verdict.state is CapabilityState.BLOCKED
        assert ReasonCode.WRITER_LEASE_ABSENT in verdict.reasons


def test_every_positive_conjunct_is_required_for_available() -> None:
    report = evaluate_operational_health(_positive_facts(), now=NOW)

    assert report.service_readiness.state is ReadinessState.READY
    assert report.trading_capability.paper_new_exposure.state is CapabilityState.AVAILABLE
    assert report.trading_capability.paper_new_exposure.reasons == ()


def test_clock_unknown_prevents_a_false_available_claim() -> None:
    unknown = _positive_facts().clock.model_copy(
        update={"state": ClockState.UNKNOWN, "failure_code": "command_failed"}
    )
    facts = _positive_facts().model_copy(update={"clock": unknown})

    verdict = evaluate_operational_health(facts, now=NOW).trading_capability.paper_new_exposure

    assert verdict.state is CapabilityState.UNKNOWN
    assert verdict.reasons == (ReasonCode.CLOCK_UNKNOWN,)


def test_synchronized_clock_fact_requires_a_bound_at_or_below_threshold() -> None:
    with pytest.raises(ValueError, match="requires current chrony evidence"):
        ClockFact(
            provider="chrony",
            state=ClockState.SYNCHRONIZED,
            observed_at=NOW,
            maximum_error_seconds=0.051,
            maximum_allowed_error_seconds=0.05,
            failure_code=None,
        )


@pytest.mark.parametrize(
    ("update", "reason", "readiness"),
    [
        ({"writer_role": WriterRole.READ_ONLY}, ReasonCode.WRITER_LEASE_ABSENT, False),
        ({"store_readable": False}, ReasonCode.STORE_UNREADABLE, True),
        (
            {"startup_faults": (StartupFaultCode.SUBMISSION_RECONCILIATION_FAILED,)},
            ReasonCode.STARTUP_DEGRADED,
            True,
        ),
        ({"broker_loop_running": False}, ReasonCode.BROKER_LOOP_DOWN, False),
        ({"reconciliation_status": "PENDING"}, ReasonCode.RECONCILIATION_NOT_READY, False),
        (
            {
                "clock": _positive_facts().clock.model_copy(
                    update={"state": ClockState.UNSYNCHRONIZED}
                )
            },
            ReasonCode.CLOCK_UNSYNCHRONIZED,
            False,
        ),
    ],
)
def test_known_degradation_blocks_without_becoming_permission(
    update: dict[str, object], reason: ReasonCode, readiness: bool
) -> None:
    report = evaluate_operational_health(_positive_facts().model_copy(update=update), now=NOW)

    assert report.trading_capability.paper_new_exposure.state is CapabilityState.BLOCKED
    assert reason in report.trading_capability.paper_new_exposure.reasons
    assert (report.service_readiness.state is ReadinessState.NOT_READY) is readiness


@pytest.mark.parametrize(
    ("state", "failure"),
    [
        (TaskState.STARTING, None),
        (TaskState.FAILED, TaskFailureCode.RAISED),
        (TaskState.STOPPED_EXPECTED, None),
        (TaskState.NOT_EXPECTED, None),
    ],
)
def test_non_running_required_task_makes_writer_not_ready(
    state: TaskState, failure: TaskFailureCode | None
) -> None:
    task = _running_task(BackgroundTaskName.LEASE_HEARTBEAT).model_copy(
        update={"state": state, "failure_code": failure}
    )
    facts = _positive_facts().model_copy(update={"tasks": (task,)})

    report = evaluate_operational_health(facts, now=NOW)

    assert report.service_readiness.state is ReadinessState.NOT_READY
    assert report.trading_capability.paper_new_exposure.state is CapabilityState.BLOCKED


def test_stale_required_task_is_not_ready_and_unknown_for_capability() -> None:
    stale = _running_task(BackgroundTaskName.LEASE_HEARTBEAT).model_copy(
        update={"observed_at": NOW - timedelta(seconds=31)}
    )
    report = evaluate_operational_health(
        _positive_facts().model_copy(update={"tasks": (stale,)}), now=NOW
    )

    assert report.service_readiness.state is ReadinessState.NOT_READY
    assert report.trading_capability.paper_new_exposure.state is CapabilityState.UNKNOWN
    assert ReasonCode.REQUIRED_TASK_STALE in report.service_readiness.reasons


def test_broker_observation_boundary_is_inclusive_then_stale() -> None:
    at_limit = _positive_facts().broker.model_copy(
        update={"observed_at": NOW - timedelta(seconds=30)}
    )
    stale = at_limit.model_copy(update={"observed_at": NOW - timedelta(seconds=30, microseconds=1)})

    current_report = evaluate_operational_health(
        _positive_facts().model_copy(update={"broker": at_limit}), now=NOW
    )
    stale_report = evaluate_operational_health(
        _positive_facts().model_copy(update={"broker": stale}), now=NOW
    )

    assert current_report.observations.broker.observation_state is ObservationState.CURRENT
    assert current_report.trading_capability.paper_new_exposure.state is CapabilityState.AVAILABLE
    assert stale_report.observations.broker.observation_state is ObservationState.STALE
    assert stale_report.trading_capability.paper_new_exposure.state is CapabilityState.UNKNOWN
    assert ReasonCode.BROKER_OBSERVATION_STALE in (
        stale_report.trading_capability.paper_new_exposure.reasons
    )


def test_future_broker_observation_is_unknown_not_fresh() -> None:
    broker = _positive_facts().broker.model_copy(
        update={"observed_at": NOW + timedelta(microseconds=1)}
    )
    report = evaluate_operational_health(
        _positive_facts().model_copy(update={"broker": broker}), now=NOW
    )

    assert report.observations.broker.observation_state is ObservationState.UNKNOWN
    assert report.observations.broker.age_seconds is None
    assert report.trading_capability.paper_new_exposure.state is CapabilityState.UNKNOWN


def test_stale_synchronized_clock_is_reported_unknown() -> None:
    stale_clock = _positive_facts().clock.model_copy(
        update={"observed_at": NOW - timedelta(seconds=31)}
    )
    report = evaluate_operational_health(
        _positive_facts().model_copy(update={"clock": stale_clock}), now=NOW
    )

    assert report.observations.clock is ClockState.UNKNOWN
    assert report.observations.clock_evidence.observation_state is ObservationState.STALE
    assert report.observations.clock_evidence.age_seconds == 31.0
    assert report.trading_capability.paper_new_exposure.state is CapabilityState.UNKNOWN
    assert ReasonCode.CLOCK_UNKNOWN in report.trading_capability.paper_new_exposure.reasons


def test_future_synchronized_clock_is_reported_unknown() -> None:
    future_clock = _positive_facts().clock.model_copy(
        update={"observed_at": NOW + timedelta(microseconds=1)}
    )
    report = evaluate_operational_health(
        _positive_facts().model_copy(update={"clock": future_clock}), now=NOW
    )

    assert report.observations.clock is ClockState.UNKNOWN
    assert report.observations.clock_evidence.observation_state is ObservationState.UNKNOWN
    assert report.observations.clock_evidence.age_seconds is None
    assert report.trading_capability.paper_new_exposure.state is CapabilityState.UNKNOWN


def test_reconciliation_staleness_is_distinct_from_known_not_ready() -> None:
    facts = _positive_facts().model_copy(
        update={"reconciliation_evidence_at": NOW - timedelta(seconds=31)}
    )
    report = evaluate_operational_health(facts, now=NOW)

    assert report.observations.reconciliation.observation_state is ObservationState.STALE
    assert report.trading_capability.paper_new_exposure.state is CapabilityState.UNKNOWN
    assert ReasonCode.RECONCILIATION_STALE in (report.trading_capability.paper_new_exposure.reasons)


def test_live_only_facts_do_not_weaken_paper_projection() -> None:
    facts = _positive_facts().model_copy(update={"kill_switch_engaged": True, "live_armed": False})
    report = evaluate_operational_health(facts, now=NOW)

    assert report.trading_capability.paper_new_exposure.state is CapabilityState.AVAILABLE
    assert report.trading_capability.live_new_exposure.state is CapabilityState.BLOCKED
    assert set(report.trading_capability.live_new_exposure.reasons) >= {
        ReasonCode.ARM_ABSENT,
        ReasonCode.KILL_SWITCH_ENGAGED,
    }


def test_autonomy_only_facts_do_not_weaken_manual_projections() -> None:
    facts = _positive_facts().model_copy(
        update={"mandate_active": False, "promotion_present": False}
    )
    report = evaluate_operational_health(facts, now=NOW)

    assert report.trading_capability.paper_new_exposure.state is CapabilityState.AVAILABLE
    assert report.trading_capability.live_new_exposure.state is CapabilityState.AVAILABLE
    assert report.trading_capability.autonomous_new_exposure.state is CapabilityState.BLOCKED


def test_multiple_faults_are_all_retained_in_deterministic_order() -> None:
    facts = _positive_facts().model_copy(
        update={
            "writer_role": WriterRole.READ_ONLY,
            "store_readable": False,
            "broker_loop_running": False,
            "reconciliation_status": "PENDING",
            "clock": _positive_facts().clock.model_copy(update={"state": ClockState.UNKNOWN}),
        }
    )

    first = evaluate_operational_health(facts, now=NOW)
    second = evaluate_operational_health(facts, now=NOW)
    reasons = first.trading_capability.paper_new_exposure.reasons

    assert first == second
    assert reasons == tuple(sorted(reasons, key=lambda reason: reason.value))
    assert set(reasons) >= {
        ReasonCode.BROKER_LOOP_DOWN,
        ReasonCode.CLOCK_UNKNOWN,
        ReasonCode.RECONCILIATION_NOT_READY,
        ReasonCode.STORE_UNREADABLE,
        ReasonCode.WRITER_LEASE_ABSENT,
    }


def test_weakening_one_fact_never_strengthens_a_verdict() -> None:
    rank = {
        CapabilityState.BLOCKED: 0,
        CapabilityState.UNKNOWN: 1,
        CapabilityState.AVAILABLE: 2,
    }
    baseline = evaluate_operational_health(_positive_facts(), now=NOW)
    weakenings = (
        {"writer_role": WriterRole.READ_ONLY},
        {"store_readable": None},
        {"broker_loop_running": False},
        {"reconciliation_status": "PENDING"},
        {"clock": _positive_facts().clock.model_copy(update={"state": ClockState.UNKNOWN})},
    )

    for update in weakenings:
        weakened = evaluate_operational_health(_positive_facts().model_copy(update=update), now=NOW)
        assert (
            rank[weakened.trading_capability.paper_new_exposure.state]
            <= rank[baseline.trading_capability.paper_new_exposure.state]
        )
