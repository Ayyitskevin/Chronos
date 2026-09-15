"""Evidence-only watchdog over the external health probe (M3 ops plane, first layer).

What this module IS: a long-lived loop that calls ``probe_external_health`` once per tick
and records what it saw — one JSON line per observation appended to
``<evidence_dir>/watchdog.jsonl`` and a fresh ``<evidence_dir>/heartbeat.json`` replaced
atomically, carrying the latest typed ``WatchdogVerdict``. It is the first of three watch
layers: this loop; ``chronos.operations.deadman``, which judges this loop's heartbeat; and
an off-host sidecar (design only — ``docs/ops/WATCHDOG.md``).

What it is NOT. It is not an actor: it never halts, kills or restarts anything and imports
nothing from the authority packages (``tests/unit/test_ops_watchdog.py`` pins the import
graph in both directions). It is not a clock oracle: the trip decision reads only the
monotonic timer, so a wall clock that steps backwards cannot un-trip a verdict; the wall
clock is recorded as evidence. It is not durable across the host: a watchdog on the same
host dies with the host, which is what the dead-man layer and the sidecar exist for.

The verdict rule: TRIPPED once the monotonic seconds since the last HEALTHY observation
(or since the first tick, when none has been HEALTHY yet) reach ``deadline_s``; a HEALTHY
observation resets the deadline. The compare is ``>=`` — the deadline itself trips, one
tick before it does not.

Evidence files are capability entries, written the way the audit log writes its anchor
(the pattern is copied, the module is not imported): the log is opened by descriptor with
``O_APPEND|O_NOFOLLOW|O_NONBLOCK`` and must be a regular file, so a planted symlink is
refused and a planted FIFO cannot hang a tick; the heartbeat goes through a unique
same-directory temp opened ``O_EXCL`` at 0600, fsynced, renamed over the entry, then a
directory fsync. A non-regular entry already at the heartbeat path is refused, not
replaced. When evidence cannot be written the tick raises ``WatchdogEvidenceError`` and
the process exits 3: a watchdog that cannot record is not a watchdog, and its stale
heartbeat is exactly what the dead-man layer trips on.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import functools
import json
import math
import os
import stat
import sys
import time
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict

from chronos import __version__
from chronos.operations.external_probe import (
    ExternalHealthReport,
    ExternalProbeState,
    ProbeConfigurationError,
    probe_external_health,
)
from chronos.utils.time import utc_now

EVIDENCE_LOG = "watchdog.jsonl"
HEARTBEAT = "heartbeat.json"

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# The evidence log: append-only, never through a link; O_NONBLOCK turns a planted FIFO
# (no reader) into an immediate ENXIO instead of a hang, and the S_ISREG check below
# refuses anything that is not a regular file.
_LOG_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _CLOEXEC | _NOFOLLOW | _NONBLOCK
# The heartbeat's unique temp: created exclusively, never through a link.
_TEMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW | _NONBLOCK
# The evidence directory itself: a real directory, never reached through a link.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC | _NOFOLLOW


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WatchdogState(StrEnum):
    HEALTHY = "HEALTHY"
    TRIPPED = "TRIPPED"


class WatchdogVerdict(_Model):
    """The watch's judgement after one tick. ``since`` is the last HEALTHY observation."""

    state: WatchdogState
    reason: str
    since: AwareDatetime | None
    evidence_path: str


class WatchdogConfigurationError(ValueError):
    """A parameter that cannot describe a watch."""


class WatchdogEvidenceError(RuntimeError):
    """The evidence directory refused a write; the observation was NOT recorded there."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def _describe(error: OSError) -> str:
    return {
        errno.ELOOP: "is a symlink (refused, never followed)",
        errno.EISDIR: "is a directory",
        errno.ENXIO: "is a fifo with no reader (refused, never blocked on)",
        errno.EEXIST: "already exists",
    }.get(error.errno or 0, error.strerror or type(error).__name__)


def _positive_finite(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise WatchdogConfigurationError(f"{name} must be a positive finite number")
    if value <= 0:
        raise WatchdogConfigurationError(f"{name} must be a positive finite number")
    return float(value)


class Watchdog:
    """One watch: a probe, an interval, an outer deadline, an evidence directory.

    Per-tick state is what the files carry; in memory the loop keeps only the monotonic
    anchor of the last HEALTHY observation (and of the first tick), which is exactly the
    evidence the deadline is judged against.
    """

    def __init__(
        self,
        *,
        probe: Callable[[], ExternalHealthReport],
        interval_s: float,
        deadline_s: float,
        evidence_dir: Path | str,
        clock: Callable[[], datetime] = utc_now,
        timer: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        pid: int | None = None,
    ) -> None:
        self._interval_s = _positive_finite("interval_s", interval_s)
        self._deadline_s = _positive_finite("deadline_s", deadline_s)
        self._probe = probe
        self._clock = clock
        self._timer = timer
        self._sleep = sleep
        self._pid = os.getpid() if pid is None else pid
        self._evidence_dir = Path(evidence_dir)
        self._log_path = self._evidence_dir / EVIDENCE_LOG
        self._heartbeat_path = self._evidence_dir / HEARTBEAT
        self._started_monotonic: float | None = None
        self._last_healthy_monotonic: float | None = None
        self._last_healthy_at: datetime | None = None
        self._temp_counter = 0
        self.last_observation: ExternalProbeState | None = None
        self._ensure_evidence_dir()

    @property
    def evidence_dir(self) -> Path:
        return self._evidence_dir

    # ------------------------------------------------------------------ the tick

    def tick(self) -> WatchdogVerdict:
        """Observe once, record the observation and the verdict, return the verdict."""

        if self._started_monotonic is None:
            self._started_monotonic = self._timer()
        started = self._timer()
        report = self._probe()
        observed_monotonic = self._timer()
        elapsed_ms = round(max(0.0, (observed_monotonic - started) * 1000.0), 3)
        observed_at = self._clock()
        self.last_observation = report.state
        if report.state is ExternalProbeState.HEALTHY:
            self._last_healthy_monotonic = observed_monotonic
            self._last_healthy_at = report.assessed_at
        verdict = self._judge(observed_monotonic)
        self._append_observation(
            report, elapsed_ms=elapsed_ms, monotonic=observed_monotonic, verdict=verdict
        )
        self._replace_heartbeat(
            observed_at=observed_at, monotonic=observed_monotonic, verdict=verdict
        )
        return verdict

    def run(self, *, once: bool = False, max_ticks: int | None = None) -> int:
        """Tick until ``once``/``max_ticks`` says stop; print each verdict as one JSON line.

        Exit code of the last tick: 0 = observation HEALTHY and verdict HEALTHY; 1 = the
        observation was not HEALTHY but the deadline has not passed; 2 = TRIPPED.
        """

        ticks = 0
        while True:
            verdict = self.tick()
            ticks += 1
            print(verdict.model_dump_json(), flush=True)
            if once or (max_ticks is not None and ticks >= max_ticks):
                if verdict.state is WatchdogState.TRIPPED:
                    return 2
                return 0 if self.last_observation is ExternalProbeState.HEALTHY else 1
            self._sleep(self._interval_s)

    # ------------------------------------------------------------------ the judgement

    def _judge(self, now_monotonic: float) -> WatchdogVerdict:
        anchor = self._last_healthy_monotonic
        if anchor is None:
            anchor = (
                self._started_monotonic if self._started_monotonic is not None else now_monotonic
            )
        silent_s = max(0.0, now_monotonic - anchor)
        last = (
            "never" if self._last_healthy_at is None else f"at {self._last_healthy_at.isoformat()}"
        )
        deadline = f"deadline {self._deadline_s:.3f} s"
        if silent_s >= self._deadline_s:
            return WatchdogVerdict(
                state=WatchdogState.TRIPPED,
                reason=(
                    f"no HEALTHY observation for {silent_s:.3f} s ({deadline}); last HEALTHY {last}"
                ),
                since=self._last_healthy_at,
                evidence_path=str(self._log_path),
            )
        return WatchdogVerdict(
            state=WatchdogState.HEALTHY,
            reason=(
                f"{silent_s:.3f} s without a HEALTHY observation, under the {deadline}; "
                f"last HEALTHY {last}"
            ),
            since=self._last_healthy_at,
            evidence_path=str(self._log_path),
        )

    # ------------------------------------------------------------------ the evidence files

    def _ensure_evidence_dir(self) -> None:
        try:
            os.makedirs(self._evidence_dir, exist_ok=True)
            fd = os.open(self._evidence_dir, _DIR_FLAGS)
        except OSError as error:
            raise WatchdogEvidenceError(self._evidence_dir, _describe(error)) from error
        os.close(fd)

    def _append_observation(
        self,
        report: ExternalHealthReport,
        *,
        elapsed_ms: float,
        monotonic: float,
        verdict: WatchdogVerdict,
    ) -> None:
        failure_code = next(
            (
                probe.failure_code.value
                for probe in (report.liveness, report.readiness)
                if probe.failure_code is not None
            ),
            None,
        )
        line = json.dumps(
            {
                "assessed_at": report.assessed_at.isoformat(),
                "state": report.state.value,
                "failure_code": failure_code,
                "elapsed_ms": elapsed_ms,
                "monotonic": monotonic,
                "liveness_status": report.liveness.status_code,
                "readiness_status": report.readiness.status_code,
                "target_origin": report.target_origin,
                "verdict": verdict.state.value,
            },
            sort_keys=True,
        )
        try:
            fd = os.open(self._log_path, _LOG_FLAGS, 0o600)
        except OSError as error:
            raise WatchdogEvidenceError(self._log_path, _describe(error)) from error
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise WatchdogEvidenceError(self._log_path, "is not a regular file")
            os.write(fd, (line + "\n").encode("utf-8"))
            os.fsync(fd)
        except OSError as error:
            raise WatchdogEvidenceError(self._log_path, _describe(error)) from error
        finally:
            os.close(fd)

    def _replace_heartbeat(
        self, *, observed_at: datetime, monotonic: float, verdict: WatchdogVerdict
    ) -> None:
        # Refuse a non-regular entry already at the heartbeat path. os.replace would swap
        # the link itself (never its target), but an operator reading evidence deserves a
        # loud refusal over a silently replaced link.
        try:
            found = os.lstat(self._heartbeat_path)
        except FileNotFoundError:
            found = None
        except OSError as error:
            raise WatchdogEvidenceError(self._heartbeat_path, _describe(error)) from error
        if found is not None and not stat.S_ISREG(found.st_mode):
            raise WatchdogEvidenceError(
                self._heartbeat_path,
                "is not a regular file (a symlink, fifo or directory is refused, never replaced)",
            )
        document = {
            "last_healthy_at": (
                None if self._last_healthy_at is None else self._last_healthy_at.isoformat()
            ),
            "last_observed_at": observed_at.isoformat(),
            "monotonic": monotonic,
            "pid": self._pid,
            "version": __version__,
            "verdict": verdict.model_dump(mode="json"),
        }
        data = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")
        self._temp_counter += 1
        temp = self._evidence_dir / f".{HEARTBEAT}.{self._pid}.{self._temp_counter}.tmp"
        try:
            fd = os.open(temp, _TEMP_FLAGS, 0o600)
        except OSError as error:
            raise WatchdogEvidenceError(temp, _describe(error)) from error
        try:
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp, self._heartbeat_path)
            dir_fd = os.open(self._evidence_dir, _DIR_FLAGS)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as error:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise WatchdogEvidenceError(self._heartbeat_path, _describe(error)) from error


# ---------------------------------------------------------------------- the CLI


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronos-watchdog",
        description=(
            "Evidence-only watchdog: probe /health/live and /health/ready every --interval "
            "seconds, record every observation, trip a typed verdict after --deadline seconds "
            "without a HEALTHY observation. Never halts, kills or restarts anything."
        ),
    )
    parser.add_argument("--base-url", required=True, help="plain HTTP(S) origin of the backend")
    parser.add_argument("--interval", type=float, default=10.0, help="seconds between ticks")
    parser.add_argument(
        "--deadline",
        type=float,
        default=90.0,
        help="seconds without a HEALTHY observation before the verdict trips",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        required=True,
        help="where watchdog.jsonl and heartbeat.json live",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=3.0,
        help="HTTPX network-inactivity timeout per endpoint (passed to the probe)",
    )
    parser.add_argument("--once", action="store_true", help="one tick, then exit by what it saw")
    return parser


def main(argv: list[str] | None = None, *, transport: object = None) -> int:
    """Entry point. ``transport`` exists so tests can hand in an httpx.MockTransport."""

    args = _parser().parse_args(argv)
    probe = functools.partial(
        probe_external_health,
        args.base_url,
        timeout_seconds=args.timeout_seconds,
        transport=transport,  # type: ignore[arg-type]
    )
    try:
        watchdog = Watchdog(
            probe=probe,
            interval_s=args.interval,
            deadline_s=args.deadline,
            evidence_dir=args.evidence_dir,
        )
        return watchdog.run(once=args.once)
    except (WatchdogConfigurationError, ProbeConfigurationError) as error:
        print(f"watchdog configuration error: {error}", file=sys.stderr)
        return 2
    except WatchdogEvidenceError as error:
        print(f"watchdog evidence error: {error}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("watchdog stopped by the operator", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
