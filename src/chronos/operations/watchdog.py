"""Evidence-only watchdog over the external health probe (M3 ops plane, first layer).

What this module IS: a long-lived loop that calls ``probe_external_health`` once per tick
and records what it saw — one JSON line per observation appended to
``<evidence_dir>/watchdog.jsonl`` and a fresh ``<evidence_dir>/heartbeat.json`` replaced
atomically, carrying the latest typed ``WatchdogVerdict``. It is the first of three watch
layers: this loop; ``chronos.operations.deadman``, which judges this loop's heartbeat; and
an off-host, receive-only sidecar the host pushes to (design only — ``docs/ops/WATCHDOG.md``
and ``docs/ops/DESIGN-alert-sidecar.md``).

What it is NOT. It is not an actor: it never halts, kills or restarts anything and imports
nothing from the authority packages (``tests/unit/test_ops_watchdog.py`` pins the import
graph in both directions). It is not a clock oracle: the trip decision reads only the
monotonic timer, so a wall clock that steps backwards cannot un-trip a verdict; the wall
clock is recorded as evidence. It is not durable across the host: a watchdog on the same
host dies with the host, which is what the dead-man layer and the sidecar exist for.

The verdict rule, fail closed (round 1). HEALTHY is published only while a HEALTHY
observation is within ``deadline_s`` (monotonic). A watchdog that has never seen HEALTHY —
a fresh evidence directory, or a heartbeat that never recorded one — publishes TRIPPED on
a non-HEALTHY tick: it has nothing to certify. On construction the prior ``heartbeat.json``
is read: a recorded TRIPPED, or a ``last_healthy_at`` already ``deadline_s`` behind the
prior ``last_observed_at``, starts this instance TRIPPED until a real HEALTHY observation;
a recent prior HEALTHY is carried over as the deadline's anchor (the interval since it is
restored once, by the wall clock, at construction — never shorter than the prior evidence
itself shows). A restart can therefore never turn an outage HEALTHY. Continuity (round 2):
the heartbeat records the kernel's boot_id and the monotonic value of the last HEALTHY
observation; a restart under the same boot whose prior monotonic values do not lie
ahead of this timer anchors the deadline on THAT value (the longest of the monotonic,
wall and prior-file accounts wins — no clock may shorten an outage); a different boot,
a missing boot_id or non-comparable values leave continuity unproven and the instance
starts TRIPPED until a real HEALTHY observation. The deadline is never rebuilt from
wall time alone. The compare is ``>=``: the deadline itself trips, one tick before does
not.

Evidence files are capability entries (round 1), written the way the audit log writes its
anchor — the pattern is copied, the module is not imported. The evidence directory is
reached by an ``O_NOFOLLOW`` component walk from the path the operator names (a symlinked
ancestor is refused; missing components are created 0700), and its descriptor is RETAINED:
every open, create, replace and fsync is descriptor-relative. An existing entry must be a
regular file owned by this process's effective user with exactly one link and mode 0600 —
a hardlink, a symlink, a FIFO, a looser mode or a foreign owner is refused, typed, and
nothing is written. Every write loops until all bytes are accepted or raises. Publication
(round 2) is an atomic envelope: an absent name is filled with ``renameat2(RENAME_NOREPLACE)``
(an entry that appeared in the window is refused, left in place); a present name is swapped
with ``RENAME_EXCHANGE`` and the DISPLACED entry is judged before it is dropped — only the
inode validated before the write is unlinked; anything else is swapped back and refused.
The watchdog never deletes an entry it did not validate. Every branch after the exchange is
settled before a raise (round 3): a displaced entry that vanishes makes the watchdog
withdraw its own fresh record — identity-bound, so a foreign entry at the name is left in
place — leaving no heartbeat rather than one the dead-man would read as proof of life.
After a publication the name is re-checked against the inode that was written. When
evidence cannot be written the tick raises
``WatchdogEvidenceError`` and the process exits 3: a watchdog that cannot record is not a
watchdog, and its stale heartbeat is exactly what the dead-man layer trips on.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import functools
import json
import math
import os
import re
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
# Directory components of the walk: real directories only, never through a link.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC | _NOFOLLOW
# The evidence log: append-only, never through a link; O_NONBLOCK turns a planted FIFO
# (no reader) into an immediate ENXIO instead of a hang. Creation is handled separately
# (O_CREAT|O_EXCL first, so a created inode is known to be ours).
_LOG_FLAGS = os.O_WRONLY | os.O_APPEND | _CLOEXEC | _NOFOLLOW | _NONBLOCK
# The heartbeat's unique temp: created exclusively, never through a link.
_TEMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW | _NONBLOCK
# A prior heartbeat read at construction: no-follow, non-blocking, regular files only.
_ENTRY_FLAGS = os.O_RDONLY | _CLOEXEC | _NOFOLLOW | _NONBLOCK
_MAX_HEARTBEAT_BYTES = 64 * 1024
#: The kernel's boot identity: a heartbeat's monotonic value is comparable with this
#: process's timer only when both were taken under the same boot (r2, continuity).
_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
_BOOT_ID_FORM = re.compile(r"[0-9A-Za-z-]{1,64}")
#: renameat2(2) flags: publish only where nothing is (NOREPLACE), or exchange two names
#: atomically (EXCHANGE) so the displaced entry can be judged before it is dropped.
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2


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
    """The evidence directory refused a write; the observation was NOT recorded there.

    ``temp_holds_foreign`` is set when the refusal left an entry that is NOT ours at the
    temp name (a failed swap back, or a withdrawal that could not restore a foreign entry):
    the caller must then not unlink the temp name — nothing foreign is ever deleted.
    """

    def __init__(self, path: Path, reason: str, *, temp_holds_foreign: bool = False) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason
        self.temp_holds_foreign = temp_holds_foreign


def _describe(error: OSError) -> str:
    return {
        errno.ELOOP: "is a symlink (refused, never followed)",
        errno.EISDIR: "is a directory",
        errno.ENOTDIR: "is not a directory",
        errno.ENXIO: "is a fifo with no reader (refused, never blocked on)",
        errno.EEXIST: "already exists",
    }.get(error.errno or 0, error.strerror or type(error).__name__)


def _component_problem(descriptor: int, component: str, error: OSError) -> str:
    """Name the refused component honestly: a link is reported as a link, not as ENOTDIR."""

    try:
        found = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
    except OSError:
        return _describe(error)
    if stat.S_ISLNK(found.st_mode):
        return "is a symlink (refused, never followed)"
    return _describe(error)


def _positive_finite(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise WatchdogConfigurationError(f"{name} must be a positive finite number")
    if value <= 0:
        raise WatchdogConfigurationError(f"{name} must be a positive finite number")
    return float(value)


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return (metadata.st_dev, metadata.st_ino)


def _read_boot_id() -> str:
    """The running kernel's boot_id, read once; unreadable or malformed is a typed refusal."""

    try:
        raw = _BOOT_ID_PATH.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise WatchdogEvidenceError(
            _BOOT_ID_PATH, f"boot identity unreadable: {type(error).__name__}: {error}"
        ) from error
    if not _BOOT_ID_FORM.fullmatch(raw):
        raise WatchdogEvidenceError(_BOOT_ID_PATH, "boot identity malformed")
    return raw


def _renameat2(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str, flags: int) -> None:
    """renameat2(2) through libc (Linux). Raises OSError with the syscall's errno."""

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        call = libc.renameat2
    except (OSError, AttributeError) as error:
        raise OSError(errno.ENOSYS, "renameat2 is not available in this libc") from error
    call.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    call.restype = ctypes.c_int
    if call(src_dir_fd, os.fsencode(src), dst_dir_fd, os.fsencode(dst), flags) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _require_owned_regular(metadata: os.stat_result, subject: Path) -> None:
    """An evidence entry is a capability: regular, ours, one link, private."""

    if not stat.S_ISREG(metadata.st_mode):
        raise WatchdogEvidenceError(
            subject, "is not a regular file (a symlink, fifo or directory is refused, never used)"
        )
    if metadata.st_uid != os.geteuid():
        raise WatchdogEvidenceError(
            subject, f"is owned by uid {metadata.st_uid}, not this process's effective user"
        )
    if metadata.st_nlink != 1:
        raise WatchdogEvidenceError(
            subject, f"has {metadata.st_nlink} links; an evidence entry must have exactly one"
        )
    mode = stat.S_IMODE(metadata.st_mode)
    if mode != 0o600:
        raise WatchdogEvidenceError(subject, f"has mode {oct(mode)}, not 0o600")


def _write_all(descriptor: int, content: bytes, subject: Path) -> None:
    """Write every byte or raise typed: a short write is never published as success."""

    view = memoryview(content)
    written = 0
    while written < len(content):
        try:
            accepted = os.write(descriptor, view[written:])
        except OSError as error:
            raise WatchdogEvidenceError(subject, _describe(error)) from error
        if accepted <= 0:
            raise WatchdogEvidenceError(
                subject, f"short write: {written} of {len(content)} bytes accepted"
            )
        written += accepted


def _open_evidence_directory(named: Path) -> tuple[int, Path]:
    """Walk to the evidence directory component by component, O_NOFOLLOW at every step.

    Missing components are created (0700). A symlinked ancestor, or a component that is
    not a directory, is refused before anything is created past it. Returns the retained
    descriptor and the absolute path it designates (for messages only — every operation
    from here on is descriptor-relative).
    """

    absolute = named if named.is_absolute() else Path.cwd() / named
    descriptor = os.open(os.sep, _DIR_FLAGS)
    try:
        for component in absolute.parts[1:]:
            try:
                child = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    child = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
                except OSError as error:
                    raise WatchdogEvidenceError(
                        absolute, f"component {component!r} {_describe(error)}"
                    ) from error
            except OSError as error:
                raise WatchdogEvidenceError(
                    absolute,
                    f"component {component!r} {_component_problem(descriptor, component, error)}",
                ) from error
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, absolute


class _PriorHeartbeat:
    """What the prior heartbeat proves, as read at construction."""

    __slots__ = (
        "boot_id",
        "last_healthy_at",
        "last_healthy_monotonic",
        "last_observed_at",
        "monotonic",
        "tripped",
    )

    def __init__(
        self,
        *,
        last_healthy_at: datetime | None,
        last_observed_at: datetime,
        tripped: bool,
        boot_id: str | None,
        monotonic: float | None,
        last_healthy_monotonic: float | None,
    ) -> None:
        self.last_healthy_at = last_healthy_at
        self.last_observed_at = last_observed_at
        self.tripped = tripped
        self.boot_id = boot_id
        self.monotonic = monotonic
        self.last_healthy_monotonic = last_healthy_monotonic


def _finite_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


class Watchdog:
    """One watch: a probe, an interval, an outer deadline, an evidence directory.

    In memory the loop keeps only the monotonic anchor of the last HEALTHY observation and
    what the prior heartbeat said at construction; the files carry everything else.
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
        boot_id: str | None = None,
    ) -> None:
        self._interval_s = _positive_finite("interval_s", interval_s)
        self._deadline_s = _positive_finite("deadline_s", deadline_s)
        self._boot_id = _read_boot_id() if boot_id is None else boot_id
        self._probe = probe
        self._clock = clock
        self._timer = timer
        self._sleep = sleep
        self._pid = os.getpid() if pid is None else pid
        self._dir_fd, self._evidence_dir = _open_evidence_directory(Path(evidence_dir))
        self._log_path = self._evidence_dir / EVIDENCE_LOG
        self._heartbeat_path = self._evidence_dir / HEARTBEAT
        self._last_healthy_monotonic: float | None = None
        self._last_healthy_at: datetime | None = None
        self._prior_tripped = False
        self._continuity_unproven: str | None = None
        self._temp_counter = 0
        self.last_observation: ExternalProbeState | None = None
        try:
            self._restore_prior_state()
        except BaseException:
            self.close()
            raise

    # ------------------------------------------------------------------ lifecycle

    @property
    def evidence_dir(self) -> Path:
        return self._evidence_dir

    @property
    def directory_fd(self) -> int:
        return self._dir_fd

    def close(self) -> None:
        """Release the retained directory descriptor; the instance is finished."""

        if self._dir_fd >= 0:
            os.close(self._dir_fd)
            self._dir_fd = -1

    def __enter__(self) -> Watchdog:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()

    def _require_open(self) -> int:
        if self._dir_fd < 0:
            raise WatchdogEvidenceError(self._evidence_dir, "the watchdog is closed")
        return self._dir_fd

    # ------------------------------------------------------------------ the tick

    def tick(self) -> WatchdogVerdict:
        """Observe once, record the observation and the verdict, return the verdict."""

        self._require_open()
        started = self._timer()
        report = self._probe()
        observed_monotonic = self._timer()
        elapsed_ms = round(max(0.0, (observed_monotonic - started) * 1000.0), 3)
        observed_at = self._clock()
        self.last_observation = report.state
        if report.state is ExternalProbeState.HEALTHY:
            self._last_healthy_monotonic = observed_monotonic
            self._last_healthy_at = report.assessed_at
            self._prior_tripped = False
            self._continuity_unproven = None
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
        observation was not HEALTHY but a HEALTHY observation is still within the deadline;
        2 = TRIPPED.
        """

        ticks = 0
        try:
            while True:
                verdict = self.tick()
                ticks += 1
                print(verdict.model_dump_json(), flush=True)
                if once or (max_ticks is not None and ticks >= max_ticks):
                    if verdict.state is WatchdogState.TRIPPED:
                        return 2
                    return 0 if self.last_observation is ExternalProbeState.HEALTHY else 1
                self._sleep(self._interval_s)
        finally:
            self.close()

    # ------------------------------------------------------------------ the judgement

    def _judge(self, now_monotonic: float) -> WatchdogVerdict:
        deadline = f"deadline {self._deadline_s:.3f} s"
        if self._last_healthy_monotonic is None:
            # No HEALTHY proof is available to this instance: nothing to certify.
            if self._prior_tripped:
                reason = (
                    "tripped before this process started (prior heartbeat) and no HEALTHY "
                    f"observation since ({deadline})"
                )
            elif self._continuity_unproven is not None:
                reason = (
                    f"continuity across restart unproven ({self._continuity_unproven}); the "
                    f"prior HEALTHY cannot anchor this deadline ({deadline})"
                )
            else:
                reason = f"no HEALTHY observation has ever been recorded ({deadline})"
            return WatchdogVerdict(
                state=WatchdogState.TRIPPED,
                reason=reason,
                since=self._last_healthy_at,
                evidence_path=str(self._log_path),
            )
        silent_s = max(0.0, now_monotonic - self._last_healthy_monotonic)
        last = (
            "never" if self._last_healthy_at is None else f"at {self._last_healthy_at.isoformat()}"
        )
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

    # ------------------------------------------------------------------ restart (r1 P1)

    def _restore_prior_state(self) -> None:
        """Read the prior heartbeat, if any, and start from what it proves — never better."""

        dir_fd = self._require_open()
        try:
            descriptor = os.open(HEARTBEAT, _ENTRY_FLAGS, dir_fd=dir_fd)
        except FileNotFoundError:
            return  # a fresh directory: nothing has ever been proven
        except OSError as error:
            raise WatchdogEvidenceError(
                self._heartbeat_path, f"prior heartbeat {_describe(error)}"
            ) from error
        try:
            _require_owned_regular(os.fstat(descriptor), self._heartbeat_path)
            raw = os.read(descriptor, _MAX_HEARTBEAT_BYTES + 1)
        except OSError as error:
            raise WatchdogEvidenceError(
                self._heartbeat_path, f"prior heartbeat unreadable: {_describe(error)}"
            ) from error
        finally:
            os.close(descriptor)
        prior = self._parse_prior(raw)
        if prior.tripped:
            self._prior_tripped = True
            self._last_healthy_at = prior.last_healthy_at
            return
        if prior.last_healthy_at is None:
            return  # a prior watchdog that never saw HEALTHY: still nothing to certify
        prior_silence_s = (prior.last_observed_at - prior.last_healthy_at).total_seconds()
        if prior_silence_s >= self._deadline_s:
            self._prior_tripped = True
            self._last_healthy_at = prior.last_healthy_at
            return
        # Continuity (r2): the prior monotonic values are comparable with this timer only
        # under the same boot and only if they do not lie ahead of it. Without that proof
        # the deadline cannot be reconstructed — not from the wall clock alone — and the
        # instance starts TRIPPED until a real HEALTHY observation.
        now_monotonic = self._timer()
        same_boot = prior.boot_id is not None and prior.boot_id == self._boot_id
        comparable = (
            prior.last_healthy_monotonic is not None
            and prior.monotonic is not None
            and prior.last_healthy_monotonic <= prior.monotonic <= now_monotonic
        )
        self._last_healthy_at = prior.last_healthy_at
        if not (same_boot and comparable):
            self._continuity_unproven = (
                "boot changed" if not same_boot else "prior monotonic evidence not comparable"
            )
            return
        assert prior.last_healthy_monotonic is not None  # narrowed by `comparable`
        monotonic_silence_s = now_monotonic - prior.last_healthy_monotonic
        wall_silence_s = (self._clock() - prior.last_healthy_at).total_seconds()
        # Neither clock may shorten an outage: the longest account wins.
        since_healthy_s = max(monotonic_silence_s, wall_silence_s, prior_silence_s)
        self._last_healthy_monotonic = now_monotonic - since_healthy_s

    def _parse_prior(self, raw: bytes) -> _PriorHeartbeat:
        def malformed(detail: str) -> WatchdogEvidenceError:
            return WatchdogEvidenceError(
                self._heartbeat_path, f"prior heartbeat is malformed: {detail}"
            )

        if len(raw) > _MAX_HEARTBEAT_BYTES:
            raise malformed("larger than 64 KiB")
        try:
            document = json.loads(raw)
        except ValueError as error:
            raise malformed("not JSON") from error
        if not isinstance(document, dict):
            raise malformed("not an object")
        verdict = document.get("verdict")
        if not isinstance(verdict, dict) or verdict.get("state") not in {
            WatchdogState.HEALTHY.value,
            WatchdogState.TRIPPED.value,
        }:
            raise malformed("verdict.state is not HEALTHY or TRIPPED")
        observed = document.get("last_observed_at")
        healthy = document.get("last_healthy_at")
        if not isinstance(observed, str) or not (healthy is None or isinstance(healthy, str)):
            raise malformed("last_observed_at / last_healthy_at are not timestamps")
        try:
            last_observed_at = datetime.fromisoformat(observed)
            last_healthy_at = None if healthy is None else datetime.fromisoformat(healthy)
        except ValueError as error:
            raise malformed("a timestamp is not ISO-8601") from error
        for value in (last_observed_at, last_healthy_at):
            if value is not None and value.utcoffset() is None:
                raise malformed("a timestamp carries no timezone")
        boot_id = document.get("boot_id")
        if not (boot_id is None or (isinstance(boot_id, str) and _BOOT_ID_FORM.fullmatch(boot_id))):
            raise malformed("boot_id is not a boot identity")
        return _PriorHeartbeat(
            last_healthy_at=last_healthy_at,
            last_observed_at=last_observed_at,
            tripped=verdict["state"] == WatchdogState.TRIPPED.value,
            boot_id=boot_id,
            monotonic=_finite_or_none(document.get("monotonic")),
            last_healthy_monotonic=_finite_or_none(document.get("last_healthy_monotonic")),
        )

    # ------------------------------------------------------------------ the evidence files

    def _create_or_open(self, name: str, flags: int, subject: Path) -> int:
        """O_CREAT|O_EXCL first (a created inode is ours: fchmod 0600), else open as found."""

        dir_fd = self._require_open()
        try:
            descriptor = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
        except FileExistsError:
            try:
                return os.open(name, flags, dir_fd=dir_fd)
            except OSError as error:
                raise WatchdogEvidenceError(subject, _describe(error)) from error
        except OSError as error:
            raise WatchdogEvidenceError(subject, _describe(error)) from error
        try:
            os.fchmod(descriptor, 0o600)
        except OSError as error:
            os.close(descriptor)
            raise WatchdogEvidenceError(subject, _describe(error)) from error
        return descriptor

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
        dir_fd = self._require_open()
        descriptor = self._create_or_open(EVIDENCE_LOG, _LOG_FLAGS, self._log_path)
        try:
            opened = os.fstat(descriptor)
            _require_owned_regular(opened, self._log_path)
            _write_all(descriptor, (line + "\n").encode("utf-8"), self._log_path)
            try:
                os.fsync(descriptor)
                named = os.stat(EVIDENCE_LOG, dir_fd=dir_fd, follow_symlinks=False)
            except OSError as error:
                raise WatchdogEvidenceError(self._log_path, _describe(error)) from error
            if _identity(named) != _identity(opened):
                raise WatchdogEvidenceError(
                    self._log_path,
                    "was replaced during the write; the bytes went to the inode that was "
                    "checked, not to whatever the name designates now",
                )
        finally:
            os.close(descriptor)

    def _replace_heartbeat(
        self, *, observed_at: datetime, monotonic: float, verdict: WatchdogVerdict
    ) -> None:
        dir_fd = self._require_open()
        # An entry already at the heartbeat name must be a capability entry; a symlink,
        # fifo, directory, hardlink or loose file is refused, never replaced.
        try:
            found: os.stat_result | None = os.stat(HEARTBEAT, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            found = None
        except OSError as error:
            raise WatchdogEvidenceError(self._heartbeat_path, _describe(error)) from error
        if found is not None:
            _require_owned_regular(found, self._heartbeat_path)
        document = {
            "boot_id": self._boot_id,
            "last_healthy_at": (
                None if self._last_healthy_at is None else self._last_healthy_at.isoformat()
            ),
            "last_healthy_monotonic": self._last_healthy_monotonic,
            "last_observed_at": observed_at.isoformat(),
            "monotonic": monotonic,
            "pid": self._pid,
            "version": __version__,
            "verdict": verdict.model_dump(mode="json"),
        }
        data = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")
        self._temp_counter += 1
        temp_name = f".{HEARTBEAT}.{self._pid}.{self._temp_counter}.tmp"
        temp_path = self._evidence_dir / temp_name
        try:
            descriptor = os.open(temp_name, _TEMP_FLAGS, 0o600, dir_fd=dir_fd)
        except OSError as error:
            raise WatchdogEvidenceError(temp_path, _describe(error)) from error
        try:
            try:
                os.fchmod(descriptor, 0o600)
                _write_all(descriptor, data, temp_path)
                os.fsync(descriptor)
                written = _identity(os.fstat(descriptor))
            finally:
                os.close(descriptor)
            self._publish(
                temp_name, validated=None if found is None else _identity(found), written=written
            )
            os.fsync(dir_fd)
            named = os.stat(HEARTBEAT, dir_fd=dir_fd, follow_symlinks=False)
        except WatchdogEvidenceError as refusal:
            if not refusal.temp_holds_foreign:  # then the temp name holds OUR bytes, or nothing
                with contextlib.suppress(OSError):
                    os.unlink(temp_name, dir_fd=dir_fd)
            raise
        except OSError as error:
            with contextlib.suppress(OSError):
                os.unlink(temp_name, dir_fd=dir_fd)
            raise WatchdogEvidenceError(self._heartbeat_path, _describe(error)) from error
        if _identity(named) != written:
            raise WatchdogEvidenceError(
                self._heartbeat_path,
                "was replaced during publication; the name no longer designates the inode "
                "that was written",
            )

    def _publish(
        self, temp_name: str, *, validated: tuple[int, int] | None, written: tuple[int, int]
    ) -> None:
        """Move the temp to the heartbeat name without ever displacing an entry that was
        not validated (r2, Daybreak P1), and settle every branch after the exchange (r3).

        Absent name: RENAME_NOREPLACE — an entry that appeared in the window makes the
        rename fail EEXIST and is refused, left in place. Present name: RENAME_EXCHANGE
        swaps the two names atomically, then the DISPLACED entry, now at the temp name, has
        exactly three outcomes, each settled before any raise: (i) the identity validated
        before the write → it is unlinked (the old heartbeat), success; (ii) anything else
        → swapped back, the temp unlinked, typed refusal — the planted entry survives at
        the name and nothing is published; (iii) VANISHED → our fresh record must not stay
        readable as proof of life, so it is withdrawn (``_withdraw``: identity-bound, no
        check/unlink window) leaving NO heartbeat — the dead-man's absent state, DEAD — and
        the typed refusal is raised.
        """

        dir_fd = self._require_open()
        if validated is None:
            try:
                _renameat2(dir_fd, temp_name, dir_fd, HEARTBEAT, _RENAME_NOREPLACE)
            except FileExistsError as error:
                raise WatchdogEvidenceError(
                    self._heartbeat_path,
                    "an entry appeared during publication where none was validated; refused "
                    "and left in place",
                ) from error
            except OSError as error:
                raise WatchdogEvidenceError(
                    self._heartbeat_path, f"atomic publication unavailable: {_describe(error)}"
                ) from error
            return
        try:
            _renameat2(dir_fd, temp_name, dir_fd, HEARTBEAT, _RENAME_EXCHANGE)
        except OSError as error:
            raise WatchdogEvidenceError(
                self._heartbeat_path, f"atomic exchange unavailable: {_describe(error)}"
            ) from error
        try:
            displaced = os.stat(temp_name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError as error:
            self._withdraw(temp_name, written)
            raise WatchdogEvidenceError(
                self._heartbeat_path,
                "displaced entry vanished mid-publication; our record was withdrawn and "
                "nothing is published (the dead-man now reads an absent heartbeat)",
            ) from error
        except OSError as error:
            raise WatchdogEvidenceError(
                self._heartbeat_path,
                f"displaced entry unreadable mid-publication: {_describe(error)}",
            ) from error
        if _identity(displaced) == validated:
            os.unlink(temp_name, dir_fd=dir_fd)  # the old, validated heartbeat: dropped
            return
        try:
            _renameat2(dir_fd, temp_name, dir_fd, HEARTBEAT, _RENAME_EXCHANGE)  # swap back
        except OSError as error:
            raise WatchdogEvidenceError(
                self._heartbeat_path,
                "replaced during publication AND the swap back failed: the new heartbeat "
                f"sits at the name and the foreign entry at {temp_name} ({_describe(error)}); "
                "nothing was deleted — operator attention",
                temp_holds_foreign=True,
            ) from error
        raise WatchdogEvidenceError(
            self._heartbeat_path,
            "replaced during publication: the entry at the name was not the one validated; "
            "it was left in place and nothing was published",
        )

    def _withdraw(self, temp_name: str, written: tuple[int, int]) -> None:
        """Take our fresh record off the heartbeat name, and only ours (r3).

        There is no unlink-by-inode, so the withdrawal is a NOREPLACE rename of whatever
        the name holds to the temp name (free after the displaced entry vanished), a
        no-follow stat of THAT, and then: our inode → unlinked; anything else → moved back
        with NOREPLACE and named in the refusal. Nothing foreign is ever deleted, and no
        check precedes an unlink on a pathname.
        """

        dir_fd = self._require_open()
        try:
            _renameat2(dir_fd, HEARTBEAT, dir_fd, temp_name, _RENAME_NOREPLACE)
        except FileNotFoundError:
            return  # the name is already empty: nothing readable as proof of life remains
        except FileExistsError as error:
            raise WatchdogEvidenceError(
                self._heartbeat_path,
                f"withdrawal refused: an entry reappeared at {temp_name}; our record stays at "
                "the name and nothing was deleted — operator attention",
                temp_holds_foreign=True,
            ) from error
        except OSError as error:
            raise WatchdogEvidenceError(
                self._heartbeat_path, f"withdrawal failed: {_describe(error)}"
            ) from error
        try:
            moved = os.stat(temp_name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as error:
            raise WatchdogEvidenceError(
                self._heartbeat_path, f"withdrawal: the moved entry vanished ({_describe(error)})"
            ) from error
        if _identity(moved) == written:
            os.unlink(temp_name, dir_fd=dir_fd)  # ours, and only ours
            return
        try:
            _renameat2(dir_fd, temp_name, dir_fd, HEARTBEAT, _RENAME_NOREPLACE)
        except OSError as error:
            raise WatchdogEvidenceError(
                self._heartbeat_path,
                f"withdrawal: a foreign entry at the name was moved to {temp_name} and could "
                f"not be moved back ({_describe(error)}); nothing was deleted — operator attention",
                temp_holds_foreign=True,
            ) from error
        raise WatchdogEvidenceError(
            self._heartbeat_path,
            "withdrawal: the entry at the name was not the record this process wrote; it was "
            "left in place and nothing was deleted",
        )


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
