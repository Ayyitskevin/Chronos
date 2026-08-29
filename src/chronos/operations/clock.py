"""Bounded, sanitized clock-health evidence for operational projections.

This module observes the local chronyd client and publishes a cache.  It does
not grant authority, contact a remote time source, install or configure a time
daemon, or expose raw command output.  Health requests consume the cache only.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import selectors
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from threading import Lock

from chronos.utils.time import utc_now

_logger = logging.getLogger("chronos.operations.clock")

# This is fixed command structure, not an operator-provided command string.
# ``-n`` prevents DNS lookups, so the observation stays local and bounded.
_CHRONYC_ARGV = ("/usr/bin/chronyc", "-n", "tracking")
_MAX_CAPTURE_BYTES = 64 * 1024
_LOCAL_REFERENCE_ID = "7F7F0101"
_DECIMAL = r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
_SYSTEM_TIME = re.compile(
    rf"^(?P<magnitude>{_DECIMAL}) seconds (?P<direction>fast|slow) of NTP time$"
)
_SECONDS = re.compile(rf"^(?P<value>{_DECIMAL}) seconds$")


class _ClockOutputTooLarge(Exception):
    pass


class ClockProvider(StrEnum):
    DISABLED = "disabled"
    CHRONY = "chrony"


class ClockState(StrEnum):
    SYNCHRONIZED = "SYNCHRONIZED"
    UNSYNCHRONIZED = "UNSYNCHRONIZED"
    UNKNOWN = "UNKNOWN"


class ClockFailureCode(StrEnum):
    DISABLED = "disabled"
    NOT_OBSERVED = "not_observed"
    BINARY_MISSING = "binary_missing"
    TIMED_OUT = "timed_out"
    COMMAND_FAILED = "command_failed"
    OUTPUT_TOO_LARGE = "output_too_large"
    OUTPUT_MALFORMED = "output_malformed"
    LOCAL_REFERENCE = "local_reference"
    NOT_SYNCHRONIZED = "not_synchronized"
    LEAP_STATUS_UNCERTAIN = "leap_status_uncertain"
    ERROR_BOUND_EXCEEDED = "error_bound_exceeded"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class ClockObservation:
    """Sanitized clock evidence safe for an unauthenticated health response."""

    provider: ClockProvider
    state: ClockState
    observed_at: datetime | None
    maximum_error_seconds: float | None
    maximum_allowed_error_seconds: float | None
    failure_code: ClockFailureCode | None
    generation: int = 0


@dataclass(frozen=True, slots=True)
class _TrackingEvidence:
    reference_id: str
    system_offset_seconds: Decimal
    root_delay_seconds: Decimal
    root_dispersion_seconds: Decimal
    leap_status: str

    @property
    def maximum_error_seconds(self) -> Decimal:
        # chrony 4.8 documents this bound for the system clock's maximum error:
        # abs(system time offset) + root dispersion + 0.5 * root delay.
        # https://chrony-project.org/doc/4.8/chronyc.html#tracking
        return (
            abs(self.system_offset_seconds)
            + self.root_dispersion_seconds
            + Decimal("0.5") * self.root_delay_seconds
        )


def _field_map(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        key, separator, value = raw_line.partition(":")
        if not separator:
            continue
        normalized = key.strip()
        if normalized in fields:
            raise ValueError("duplicate chrony tracking field")
        fields[normalized] = value.strip()
    return fields


def _nonnegative_seconds(value: str) -> Decimal:
    match = _SECONDS.fullmatch(value)
    if match is None:
        raise ValueError("invalid chrony seconds field")
    try:
        parsed = Decimal(match.group("value"))
    except InvalidOperation as error:
        raise ValueError("invalid chrony decimal") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("chrony duration must be finite and nonnegative")
    return parsed


def _parse_tracking(output: str) -> _TrackingEvidence:
    fields = _field_map(output)
    required = ("Reference ID", "System time", "Root delay", "Root dispersion", "Leap status")
    if any(name not in fields for name in required):
        raise ValueError("chrony tracking output is missing a required field")

    reference_id = fields["Reference ID"].split(maxsplit=1)[0].upper()
    if not re.fullmatch(r"[0-9A-F]{8}", reference_id):
        raise ValueError("invalid chrony reference id")

    system_match = _SYSTEM_TIME.fullmatch(fields["System time"])
    if system_match is None:
        raise ValueError("invalid chrony system time field")
    try:
        magnitude = Decimal(system_match.group("magnitude"))
    except InvalidOperation as error:
        raise ValueError("invalid chrony system offset") from error
    if not magnitude.is_finite() or magnitude < 0:
        raise ValueError("chrony system offset must be finite and nonnegative")
    direction = system_match.group("direction")
    signed_offset = magnitude if direction == "fast" else -magnitude

    return _TrackingEvidence(
        reference_id=reference_id,
        system_offset_seconds=signed_offset,
        root_delay_seconds=_nonnegative_seconds(fields["Root delay"]),
        root_dispersion_seconds=_nonnegative_seconds(fields["Root dispersion"]),
        leap_status=fields["Leap status"],
    )


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        with suppress(ProcessLookupError):
            process.kill()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1)


def _run_chronyc(timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    """Run one fixed, local chronyc query with bounded time and captured output.

    ``subprocess.Popen`` receives an argument vector and ``shell=False``; no
    operator text can become a shell command. Pipes are drained incrementally
    so the byte limit is enforced before output can accumulate in memory:
    https://docs.python.org/3.12/library/subprocess.html#popen-constructor
    """

    # The executable and every argument are module constants; no request or
    # setting contributes command text.
    process = subprocess.Popen(
        _CHRONYC_ARGV,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        cwd="/",
        env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        close_fds=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    deadline = time.monotonic() + timeout_seconds
    streams = selectors.DefaultSelector()
    streams.register(process.stdout, selectors.EVENT_READ, "stdout")
    streams.register(process.stderr, selectors.EVENT_READ, "stderr")
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    try:
        while streams.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(_CHRONYC_ARGV, timeout_seconds)
            ready = streams.select(remaining)
            if not ready:
                raise subprocess.TimeoutExpired(_CHRONYC_ARGV, timeout_seconds)
            for key, _ in ready:
                stream = key.fileobj
                read_size = min(8192, max(1, _MAX_CAPTURE_BYTES + 1 - total))
                chunk = os.read(key.fd, read_size)
                if not chunk:
                    streams.unregister(stream)
                    continue
                total += len(chunk)
                if total > _MAX_CAPTURE_BYTES:
                    raise _ClockOutputTooLarge
                captured[str(key.data)].extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(_CHRONYC_ARGV, timeout_seconds)
        returncode = process.wait(timeout=remaining)
    except BaseException:
        _terminate(process)
        raise
    finally:
        streams.close()
        process.stdout.close()
        process.stderr.close()

    return subprocess.CompletedProcess(
        args=_CHRONYC_ARGV,
        returncode=returncode,
        stdout=captured["stdout"].decode("utf-8", errors="strict"),
        stderr=captured["stderr"].decode("utf-8", errors="strict"),
    )


class ChronyClockSampler:
    """Translate one fixed chronyc tracking query into sanitized evidence."""

    def __init__(
        self,
        *,
        maximum_allowed_error_seconds: float,
        timeout_seconds: float,
        runner: Callable[[float], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self._maximum_allowed = Decimal(str(maximum_allowed_error_seconds))
        if not self._maximum_allowed.is_finite() or self._maximum_allowed <= 0:
            raise ValueError("maximum allowed clock error must be finite and positive")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("clock command timeout must be finite and positive")
        self._timeout_seconds = timeout_seconds
        self._runner = runner or _run_chronyc

    def _unknown(
        self,
        failure_code: ClockFailureCode,
        *,
        observed_at: datetime,
    ) -> ClockObservation:
        return ClockObservation(
            provider=ClockProvider.CHRONY,
            state=ClockState.UNKNOWN,
            observed_at=observed_at,
            maximum_error_seconds=None,
            maximum_allowed_error_seconds=float(self._maximum_allowed),
            failure_code=failure_code,
        )

    def sample(self, *, now: datetime | None = None) -> ClockObservation:
        observed_at = now or utc_now()
        try:
            result = self._runner(self._timeout_seconds)
        except FileNotFoundError:
            return self._unknown(ClockFailureCode.BINARY_MISSING, observed_at=observed_at)
        except subprocess.TimeoutExpired:
            return self._unknown(ClockFailureCode.TIMED_OUT, observed_at=observed_at)
        except _ClockOutputTooLarge:
            return self._unknown(ClockFailureCode.OUTPUT_TOO_LARGE, observed_at=observed_at)
        except UnicodeError:
            return self._unknown(ClockFailureCode.OUTPUT_MALFORMED, observed_at=observed_at)
        except OSError:
            return self._unknown(ClockFailureCode.COMMAND_FAILED, observed_at=observed_at)

        captured_size = len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8"))
        if captured_size > _MAX_CAPTURE_BYTES:
            return self._unknown(ClockFailureCode.OUTPUT_TOO_LARGE, observed_at=observed_at)
        if result.returncode != 0:
            return self._unknown(ClockFailureCode.COMMAND_FAILED, observed_at=observed_at)
        try:
            evidence = _parse_tracking(result.stdout)
        except ValueError:
            return self._unknown(ClockFailureCode.OUTPUT_MALFORMED, observed_at=observed_at)

        if evidence.reference_id == _LOCAL_REFERENCE_ID:
            return self._unknown(ClockFailureCode.LOCAL_REFERENCE, observed_at=observed_at)
        if evidence.leap_status == "Not synchronised":
            return ClockObservation(
                provider=ClockProvider.CHRONY,
                state=ClockState.UNSYNCHRONIZED,
                observed_at=observed_at,
                maximum_error_seconds=None,
                maximum_allowed_error_seconds=float(self._maximum_allowed),
                failure_code=ClockFailureCode.NOT_SYNCHRONIZED,
            )
        if evidence.leap_status != "Normal":
            return self._unknown(ClockFailureCode.LEAP_STATUS_UNCERTAIN, observed_at=observed_at)

        maximum_error = float(evidence.maximum_error_seconds)
        if not math.isfinite(maximum_error):
            return self._unknown(ClockFailureCode.OUTPUT_MALFORMED, observed_at=observed_at)
        state = (
            ClockState.SYNCHRONIZED
            if evidence.maximum_error_seconds <= self._maximum_allowed
            else ClockState.UNSYNCHRONIZED
        )
        failure_code = (
            None if state is ClockState.SYNCHRONIZED else ClockFailureCode.ERROR_BOUND_EXCEEDED
        )
        return ClockObservation(
            provider=ClockProvider.CHRONY,
            state=state,
            observed_at=observed_at,
            maximum_error_seconds=maximum_error,
            maximum_allowed_error_seconds=float(self._maximum_allowed),
            failure_code=failure_code,
        )


class ClockHealthCache:
    """Thread-safe, generation-counted clock evidence cache."""

    def __init__(
        self,
        *,
        provider: ClockProvider = ClockProvider.DISABLED,
        maximum_allowed_error_seconds: float | None = None,
    ) -> None:
        failure = (
            ClockFailureCode.DISABLED
            if provider is ClockProvider.DISABLED
            else ClockFailureCode.NOT_OBSERVED
        )
        self._observation = ClockObservation(
            provider=provider,
            state=ClockState.UNKNOWN,
            observed_at=None,
            maximum_error_seconds=None,
            maximum_allowed_error_seconds=maximum_allowed_error_seconds,
            failure_code=failure,
        )
        self._lock = Lock()

    def publish(self, observation: ClockObservation) -> ClockObservation:
        with self._lock:
            if observation.provider is not self._observation.provider:
                raise ValueError("clock provider cannot change during a process lifetime")
            self._observation = replace(
                observation,
                generation=self._observation.generation + 1,
            )
            return self._observation

    def snapshot(self) -> ClockObservation:
        with self._lock:
            return self._observation


async def refresh_clock_health(
    cache: ClockHealthCache,
    sampler: ChronyClockSampler,
) -> ClockObservation:
    """Take one bounded sample without leaking an unexpected failure."""

    try:
        observation = await asyncio.to_thread(sampler.sample)
    except Exception as error:
        _logger.error(
            "Clock-health sampling raised unexpectedly; publishing UNKNOWN",
            extra={
                "event": "clock_health_sample_failed",
                "error_type": type(error).__name__,
                "passed": False,
            },
        )
        previous = cache.snapshot()
        observation = ClockObservation(
            provider=ClockProvider.CHRONY,
            state=ClockState.UNKNOWN,
            observed_at=utc_now(),
            maximum_error_seconds=None,
            maximum_allowed_error_seconds=previous.maximum_allowed_error_seconds,
            failure_code=ClockFailureCode.INTERNAL_ERROR,
        )
    return cache.publish(observation)


async def clock_health_monitor(
    cache: ClockHealthCache,
    sampler: ChronyClockSampler,
    *,
    poll_interval_seconds: float,
) -> None:
    """Refresh the cache periodically; callers take the initial sample."""

    while True:
        await asyncio.sleep(poll_interval_seconds)
        await refresh_clock_health(cache, sampler)
