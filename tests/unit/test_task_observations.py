from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest

from chronos.api.dependencies import BackendState
from chronos.api.main import _heartbeat_lease
from chronos.api.task_observations import TaskObservationRegistry
from chronos.operations.health import (
    BackgroundTaskName,
    TaskFailureCode,
    TaskState,
)
from chronos.utils.locking import WriterLease

NOW = datetime(2026, 8, 29, 16, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_running_task_progress_and_expected_stop_are_retained() -> None:
    moment = [NOW]
    registry = TaskObservationRegistry(clock=lambda: moment[0])
    release = asyncio.Event()

    async def run() -> None:
        await release.wait()

    task = asyncio.create_task(run())
    registry.bind(BackgroundTaskName.RECONCILIATION, task, max_age_seconds=10)
    assert registry.snapshot()[0].state is TaskState.RUNNING

    moment[0] += timedelta(seconds=2)
    registry.progress(BackgroundTaskName.RECONCILIATION)
    assert registry.snapshot()[0].observed_at == moment[0]

    registry.expect_stop(BackgroundTaskName.RECONCILIATION)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    stopped = registry.snapshot()[0]
    assert stopped.state is TaskState.STOPPED_EXPECTED
    assert stopped.failure_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_unexpected_exit_is_failed_without_exception_text(raises: bool) -> None:
    registry = TaskObservationRegistry(clock=lambda: NOW)

    async def run() -> None:
        if raises:
            raise RuntimeError("secret broker detail DU1234567")

    task = asyncio.create_task(run())
    registry.bind(BackgroundTaskName.AUTONOMY, task, max_age_seconds=10)
    with suppress(RuntimeError):
        await task
    await asyncio.sleep(0)

    observation = registry.snapshot()[0]
    assert observation.state is TaskState.FAILED
    assert observation.failure_code is (
        TaskFailureCode.RAISED if raises else TaskFailureCode.EXITED_UNEXPECTEDLY
    )
    assert "DU1234567" not in observation.model_dump_json()


@pytest.mark.asyncio
async def test_unexpected_cancellation_is_failed() -> None:
    registry = TaskObservationRegistry(clock=lambda: NOW)
    task = asyncio.create_task(asyncio.sleep(60))
    registry.bind(BackgroundTaskName.LEASE_HEARTBEAT, task, max_age_seconds=10)

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    observation = registry.snapshot()[0]
    assert observation.state is TaskState.FAILED
    assert observation.failure_code is TaskFailureCode.CANCELLED_UNEXPECTEDLY


def test_not_expected_task_is_explicit_not_missing() -> None:
    registry = TaskObservationRegistry(clock=lambda: NOW)
    registry.not_expected(
        BackgroundTaskName.AUTONOMY,
        max_age_seconds=5,
        required_for_writer=False,
    )

    observation = registry.snapshot()[0]
    assert observation.state is TaskState.NOT_EXPECTED
    assert observation.required_for_writer is False


@pytest.mark.asyncio
async def test_lease_loss_demotes_and_retains_the_stopped_heartbeat() -> None:
    registry = TaskObservationRegistry(clock=lambda: NOW)
    state = SimpleNamespace(read_only=False, lease=object())

    class _LostLease:
        def renew(self) -> bool:
            return False

    task = asyncio.create_task(
        _heartbeat_lease(
            cast(BackendState, state),
            cast(WriterLease, _LostLease()),
            0,
            on_progress=lambda: registry.progress(BackgroundTaskName.LEASE_HEARTBEAT),
        )
    )
    registry.bind(BackgroundTaskName.LEASE_HEARTBEAT, task, max_age_seconds=10)
    await task
    await asyncio.sleep(0)

    assert state.read_only is True
    assert state.lease is None
    observation = registry.snapshot()[0]
    assert observation.state is TaskState.FAILED
    assert observation.failure_code is TaskFailureCode.EXITED_UNEXPECTEDLY
