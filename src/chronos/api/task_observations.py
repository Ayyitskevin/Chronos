"""Thread-safe lifecycle observations for lifespan-owned asyncio tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from threading import Lock

from chronos.operations.health import (
    BackgroundTaskName,
    TaskFailureCode,
    TaskObservation,
    TaskState,
)
from chronos.utils.time import utc_now


class TaskObservationRegistry:
    """Retain sanitized task state after the local ``Task`` has disappeared."""

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        self._lock = Lock()
        self._observations: dict[BackgroundTaskName, TaskObservation] = {}
        self._expected_stops: set[BackgroundTaskName] = set()

    def starting(
        self,
        name: BackgroundTaskName,
        *,
        max_age_seconds: float,
        required_for_writer: bool = True,
    ) -> None:
        self._replace(
            name,
            state=TaskState.STARTING,
            max_age_seconds=max_age_seconds,
            required_for_writer=required_for_writer,
        )

    def bind(
        self,
        name: BackgroundTaskName,
        task: asyncio.Task[None],
        *,
        max_age_seconds: float,
        required_for_writer: bool = True,
    ) -> None:
        self._replace(
            name,
            state=TaskState.RUNNING,
            max_age_seconds=max_age_seconds,
            required_for_writer=required_for_writer,
        )
        task.add_done_callback(lambda completed: self._completed(name, completed))

    def progress(self, name: BackgroundTaskName) -> None:
        with self._lock:
            current = self._observations.get(name)
            if current is None or current.state is not TaskState.RUNNING:
                return
            self._observations[name] = current.model_copy(update={"observed_at": self._clock()})

    def expect_stop(self, name: BackgroundTaskName) -> None:
        with self._lock:
            self._expected_stops.add(name)

    def not_expected(
        self,
        name: BackgroundTaskName,
        *,
        max_age_seconds: float,
        required_for_writer: bool = False,
    ) -> None:
        self._replace(
            name,
            state=TaskState.NOT_EXPECTED,
            max_age_seconds=max_age_seconds,
            required_for_writer=required_for_writer,
        )

    def snapshot(self) -> tuple[TaskObservation, ...]:
        with self._lock:
            return tuple(
                self._observations[name]
                for name in sorted(self._observations, key=lambda item: item.value)
            )

    def _replace(
        self,
        name: BackgroundTaskName,
        *,
        state: TaskState,
        max_age_seconds: float,
        required_for_writer: bool,
        failure_code: TaskFailureCode | None = None,
    ) -> None:
        with self._lock:
            self._observations[name] = TaskObservation(
                name=name,
                state=state,
                observed_at=self._clock(),
                max_age_seconds=max_age_seconds,
                required_for_writer=required_for_writer,
                failure_code=failure_code,
            )

    def _completed(self, name: BackgroundTaskName, task: asyncio.Task[None]) -> None:
        with self._lock:
            current = self._observations.get(name)
            if current is None:
                return
            expected = name in self._expected_stops
            self._expected_stops.discard(name)
            if expected:
                state = TaskState.STOPPED_EXPECTED
                failure_code = None
            elif task.cancelled():
                state = TaskState.FAILED
                failure_code = TaskFailureCode.CANCELLED_UNEXPECTEDLY
            elif task.exception() is None:
                state = TaskState.FAILED
                failure_code = TaskFailureCode.EXITED_UNEXPECTEDLY
            else:
                state = TaskState.FAILED
                failure_code = TaskFailureCode.RAISED
            self._observations[name] = current.model_copy(
                update={
                    "state": state,
                    "observed_at": self._clock(),
                    "failure_code": failure_code,
                }
            )
