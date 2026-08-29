from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from chronos.operations import clock as clock_module
from chronos.operations.clock import (
    ChronyClockSampler,
    ClockFailureCode,
    ClockHealthCache,
    ClockProvider,
    ClockState,
    _run_chronyc,
    clock_health_monitor,
    refresh_clock_health,
)

NOW = datetime(2026, 8, 29, 17, 0, tzinfo=UTC)


def _output(
    *,
    reference_id: str = "C0A80101 (ntp.example.invalid)",
    system_time: str = "0.010000000 seconds slow of NTP time",
    root_delay: str = "0.020000000 seconds",
    root_dispersion: str = "0.005000000 seconds",
    leap_status: str = "Normal",
) -> str:
    return "\n".join(
        (
            f"Reference ID    : {reference_id}",
            "Stratum         : 3",
            f"System time     : {system_time}",
            f"Root delay      : {root_delay}",
            f"Root dispersion : {root_dispersion}",
            f"Leap status     : {leap_status}",
        )
    )


def _result(
    stdout: str,
    *,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=("/usr/bin/chronyc", "-n", "tracking"),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _sampler(
    output: str,
    *,
    maximum_allowed: float = 0.025,
) -> ChronyClockSampler:
    return ChronyClockSampler(
        maximum_allowed_error_seconds=maximum_allowed,
        timeout_seconds=2,
        runner=lambda _: _result(output),
    )


def test_quantitative_bound_at_threshold_is_synchronized() -> None:
    # abs(-0.010) + 0.005 + 0.5 * 0.020 == 0.025 seconds.
    observation = _sampler(_output()).sample(now=NOW)

    assert observation.state is ClockState.SYNCHRONIZED
    assert observation.maximum_error_seconds == pytest.approx(0.025)
    assert observation.maximum_allowed_error_seconds == 0.025
    assert observation.failure_code is None
    assert observation.observed_at == NOW


def test_quantitative_bound_above_threshold_is_known_unsynchronized() -> None:
    observation = _sampler(_output(), maximum_allowed=0.024999).sample(now=NOW)

    assert observation.state is ClockState.UNSYNCHRONIZED
    assert observation.failure_code is ClockFailureCode.ERROR_BOUND_EXCEEDED
    assert observation.maximum_error_seconds == pytest.approx(0.025)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (_output(reference_id="7F7F0101 (LOCAL)"), ClockFailureCode.LOCAL_REFERENCE),
        (
            _output(leap_status="Not synchronised"),
            ClockFailureCode.NOT_SYNCHRONIZED,
        ),
        (_output(leap_status="Insert second"), ClockFailureCode.LEAP_STATUS_UNCERTAIN),
        (_output().replace("Root delay", "Root wait"), ClockFailureCode.OUTPUT_MALFORMED),
        (
            _output(root_dispersion="-0.001 seconds"),
            ClockFailureCode.OUTPUT_MALFORMED,
        ),
    ],
)
def test_uncertain_or_invalid_tracking_output_never_claims_synchronized(
    output: str,
    expected: ClockFailureCode,
) -> None:
    observation = _sampler(output).sample(now=NOW)

    assert observation.state is not ClockState.SYNCHRONIZED
    assert observation.failure_code is expected


@pytest.mark.parametrize(
    ("runner", "expected"),
    [
        (
            lambda _: (_ for _ in ()).throw(FileNotFoundError()),
            ClockFailureCode.BINARY_MISSING,
        ),
        (
            lambda _: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(("/usr/bin/chronyc",), timeout=2)
            ),
            ClockFailureCode.TIMED_OUT,
        ),
        (
            lambda _: _result("", returncode=1, stderr="private daemon detail"),
            ClockFailureCode.COMMAND_FAILED,
        ),
        (lambda _: _result("x" * (64 * 1024 + 1)), ClockFailureCode.OUTPUT_TOO_LARGE),
    ],
)
def test_command_failures_are_closed_sanitized_codes(
    runner: object,
    expected: ClockFailureCode,
) -> None:
    sampler = ChronyClockSampler(
        maximum_allowed_error_seconds=0.1,
        timeout_seconds=2,
        runner=runner,  # type: ignore[arg-type]
    )

    observation = sampler.sample(now=NOW)

    assert observation.state is ClockState.UNKNOWN
    assert observation.failure_code is expected
    assert "private" not in repr(observation)


def test_runner_uses_only_fixed_argv_without_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Marker(Exception):
        pass

    def fake_popen(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        raise Marker

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(Marker):
        _run_chronyc(1.5)

    assert captured["args"] == (("/usr/bin/chronyc", "-n", "tracking"),)
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == "/"
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_real_process_capture_is_stopped_at_the_byte_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        clock_module,
        "_CHRONYC_ARGV",
        (sys.executable, "-c", "import sys; sys.stdout.write('x' * 70000)"),
    )
    sampler = ChronyClockSampler(
        maximum_allowed_error_seconds=0.1,
        timeout_seconds=2,
    )

    observation = sampler.sample(now=NOW)

    assert observation.state is ClockState.UNKNOWN
    assert observation.failure_code is ClockFailureCode.OUTPUT_TOO_LARGE


def test_real_process_is_stopped_at_the_time_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        clock_module,
        "_CHRONYC_ARGV",
        (sys.executable, "-c", "import time; time.sleep(5)"),
    )
    sampler = ChronyClockSampler(
        maximum_allowed_error_seconds=0.1,
        timeout_seconds=0.05,
    )

    observation = sampler.sample(now=NOW)

    assert observation.state is ClockState.UNKNOWN
    assert observation.failure_code is ClockFailureCode.TIMED_OUT


@pytest.mark.asyncio
async def test_refresh_publishes_generation_counted_evidence() -> None:
    cache = ClockHealthCache(
        provider=ClockProvider.CHRONY,
        maximum_allowed_error_seconds=0.025,
    )
    sampler = _sampler(_output())

    first = await refresh_clock_health(cache, sampler)
    second = await refresh_clock_health(cache, sampler)

    assert first.generation == 1
    assert second.generation == 2
    assert cache.snapshot() == second


@pytest.mark.asyncio
async def test_periodic_monitor_refreshes_after_interval() -> None:
    calls = 0

    def runner(_: float) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return _result(_output())

    cache = ClockHealthCache(
        provider=ClockProvider.CHRONY,
        maximum_allowed_error_seconds=0.025,
    )
    sampler = ChronyClockSampler(
        maximum_allowed_error_seconds=0.025,
        timeout_seconds=2,
        runner=runner,
    )

    task = asyncio.create_task(clock_health_monitor(cache, sampler, poll_interval_seconds=0.001))
    try:
        for _ in range(100):
            if cache.snapshot().generation:
                break
            await asyncio.sleep(0.001)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert calls >= 1
    assert cache.snapshot().state is ClockState.SYNCHRONIZED
