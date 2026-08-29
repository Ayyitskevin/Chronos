from __future__ import annotations

from datetime import UTC, datetime, timedelta

from chronos.operations.health import (
    BackgroundTaskName,
    BrokerConnectionFact,
    CapabilityState,
    ClockState,
    OperationalFacts,
    ReasonCode,
    TaskObservation,
    TaskState,
    WriterRole,
    evaluate_operational_health,
)

NOW = datetime(2026, 8, 29, 16, 0, tzinfo=UTC)


def test_simultaneous_task_broker_store_and_clock_faults_remain_visible() -> None:
    facts = OperationalFacts(
        backend_initialized=True,
        writer_role=WriterRole.WRITER,
        store_readable=False,
        tasks=(
            TaskObservation(
                name=BackgroundTaskName.LEASE_HEARTBEAT,
                state=TaskState.FAILED,
                observed_at=NOW,
                max_age_seconds=10,
            ),
            TaskObservation(
                name=BackgroundTaskName.RECONCILIATION,
                state=TaskState.RUNNING,
                observed_at=NOW - timedelta(seconds=11),
                max_age_seconds=10,
            ),
        ),
        broker_loop_running=True,
        broker=BrokerConnectionFact(
            connected=False,
            observed_at=NOW,
            max_age_seconds=10,
            generation=5,
        ),
        reconciliation_status="RECONCILED",
        reconciliation_evidence_at=NOW - timedelta(seconds=11),
        reconciliation_max_age_seconds=10,
        paper_new_exposure_configured=True,
        clock_state=ClockState.UNSYNCHRONIZED,
    )

    report = evaluate_operational_health(facts, now=NOW)
    reasons = set(report.trading_capability.paper_new_exposure.reasons)

    assert report.service_readiness.state == "NOT_READY"
    assert report.trading_capability.paper_new_exposure.state is CapabilityState.BLOCKED
    assert reasons >= {
        ReasonCode.BROKER_DISCONNECTED,
        ReasonCode.CLOCK_UNSYNCHRONIZED,
        ReasonCode.RECONCILIATION_STALE,
        ReasonCode.REQUIRED_TASK_FAILED,
        ReasonCode.REQUIRED_TASK_STALE,
        ReasonCode.STORE_UNREADABLE,
    }


def test_live_kill_switch_and_unknown_clock_never_raise_a_lane() -> None:
    facts = OperationalFacts(
        backend_initialized=True,
        writer_role=WriterRole.WRITER,
        store_readable=True,
        broker_loop_running=True,
        broker=BrokerConnectionFact(
            connected=True,
            observed_at=NOW,
            max_age_seconds=10,
        ),
        reconciliation_status="RECONCILED",
        reconciliation_evidence_at=NOW,
        reconciliation_max_age_seconds=10,
        live_new_exposure_configured=True,
        kill_switch_engaged=True,
        live_armed=True,
        clock_state=ClockState.UNKNOWN,
    )

    verdict = evaluate_operational_health(facts, now=NOW).trading_capability.live_new_exposure

    assert verdict.state is CapabilityState.BLOCKED
    assert verdict.reasons == (ReasonCode.CLOCK_UNKNOWN, ReasonCode.KILL_SWITCH_ENGAGED)
