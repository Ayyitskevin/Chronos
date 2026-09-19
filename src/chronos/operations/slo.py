"""Offline SLO evaluator over the watchdog's evidence files (M3 ops plane, SLO-1).

An operator declares objectives ONCE in a typed document (``slo.json``); this module reads
the two files the watchdog writes — ``watchdog.jsonl`` and ``heartbeat.json`` — and says,
per objective, MET / BREACHED / UNKNOWN with the measured value and the window it was
measured over. It is an observation and nothing else: it starts nothing, alerts nobody,
changes no verdict, and imports nothing from the authority packages
(``tests/unit/test_ops_slo.py`` pins the import graph both ways). Nothing in this repository
starts it; an operator or a timer runs
``python -m chronos.operations.slo --evidence-dir data/ops --slo data/ops/slo.json``.

What an SLO here proves: compliance of the RECORDED evidence over the declared window. What
it cannot prove: anything about a host that died — it records nothing, and that silence is
the dead-man's boundary (``docs/ops/WATCHDOG.md``). A met objective is not a healthy trader.

Objectives (each optional; a document declaring none is refused):
  probe_latency_p95_ms        nearest-rank p95 of ``elapsed_ms`` over the lines within
                              ``window_s``
  readiness_availability_pct  percentage of lines within ``window_s`` whose probe ``state``
                              is HEALTHY (an UNKNOWN tick is not availability)
  watchdog_deadline_s         the longest monotonic stretch without a HEALTHY observation
                              within the window must stay under it, and no line may carry a
                              TRIPPED verdict; the declared value must be at least 3 x the
                              cadence the writer actually kept (the median spacing of
                              consecutive ``monotonic`` values) — a shorter deadline is
                              refused typed, exit 64
  deadman_max_age_s           the heartbeat's ``last_observed_at`` age; when both are
                              declared it must be at least 2 x ``watchdog_deadline_s``
  clock_max_error_s           the largest disagreement between the wall-clock and monotonic
                              deltas of consecutive lines within the window (a stepped clock)

UNKNOWN is the honest answer, never a guess: the log is absent, malformed or spans less
than the window; a measurement needs two lines and has one; the heartbeat is absent,
malformed or from the future; an entry is a symlink, FIFO, directory or device (refused
typed, never followed, never blocked on). The overall state is BREACHED if any objective
is, else UNKNOWN if any is, else MET; exit 0 / 2 / 3, 64 for a bad document.

Reads mirror the dead-man's discipline (the pattern is copied, the module is not imported):
the evidence directory is reached by an ``O_NOFOLLOW`` component walk; each entry is opened
relative to that directory with ``O_RDONLY|O_NOFOLLOW|O_NONBLOCK`` and must be a regular
file (``fstat``); reads are bounded — 64 KiB for the heartbeat, the document and the cache,
the newest 4 MiB of the log (a torn last line is where the writer died, not data). The cache
``slo-evaluation.json`` that ``/health`` shows as an observation is published the watchdog's
way: a unique ``O_EXCL`` 0600 temp, fsync, rename, directory fsync; a non-regular entry at
the name is refused, never replaced. The evaluator creates no directory.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import math
import os
import secrets
import stat
import statistics
import sys
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Literal, NamedTuple

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from chronos.utils.time import utc_now

EVIDENCE_LOG = "watchdog.jsonl"
HEARTBEAT = "heartbeat.json"
#: The last evaluation, published beside the evidence for ``/health`` to show as an observation.
EVALUATION_CACHE = "slo-evaluation.json"
EXIT_BAD_DOCUMENT = 64

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC | _NOFOLLOW
_ENTRY_FLAGS = os.O_RDONLY | _CLOEXEC | _NOFOLLOW | _NONBLOCK
_TEMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW | _NONBLOCK
_MAX_SMALL_BYTES = 64 * 1024
_MAX_LOG_BYTES = 4 * 1024 * 1024
#: A heartbeat or a line may sit a little ahead of the evaluator's clock (two NTP states).
FUTURE_TOLERANCE_S = 5.0
#: The shipped cadence rules the document is validated against.
DEADLINE_CADENCE_FACTOR = 3.0
MAX_AGE_DEADLINE_FACTOR = 2.0

OBJECTIVES: tuple[str, ...] = (
    "probe_latency_p95_ms",
    "readiness_availability_pct",
    "watchdog_deadline_s",
    "deadman_max_age_s",
    "clock_max_error_s",
)
ObjectiveName = Literal[
    "probe_latency_p95_ms",
    "readiness_availability_pct",
    "watchdog_deadline_s",
    "deadman_max_age_s",
    "clock_max_error_s",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SloState(StrEnum):
    MET = "MET"
    BREACHED = "BREACHED"
    UNKNOWN = "UNKNOWN"


EXIT_CODES = {SloState.MET: 0, SloState.BREACHED: 2, SloState.UNKNOWN: 3}


class SloDocumentError(ValueError):
    """A document that cannot describe objectives, or contradicts the shipped cadence rules."""


class SloCacheError(RuntimeError):
    """The evaluation could not be published where ``/health`` reads it."""


_Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class SloDocument(_StrictModel):
    """The operator's objectives. Numbers are owner-frozen thresholds (OWNER-ASKS 9)."""

    schema_version: Literal[1] = 1
    probe_latency_p95_ms: _Positive | None = None
    readiness_availability_pct: (
        Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] | None
    ) = None
    window_s: _Positive | None = None
    watchdog_deadline_s: _Positive | None = None
    deadman_max_age_s: _Positive | None = None
    clock_max_error_s: _Positive | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _numbers_are_not_booleans(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("a boolean is not a number")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> SloDocument:
        if all(getattr(self, name) is None for name in OBJECTIVES):
            raise ValueError("no objective declared")
        windowed = self.probe_latency_p95_ms is not None or (
            self.readiness_availability_pct is not None
        )
        if windowed and self.window_s is None:
            raise ValueError(
                "window_s is required with probe_latency_p95_ms or readiness_availability_pct"
            )
        if self.watchdog_deadline_s is not None and self.deadman_max_age_s is not None:
            floor = MAX_AGE_DEADLINE_FACTOR * self.watchdog_deadline_s
            if self.deadman_max_age_s < floor:
                raise ValueError(
                    f"deadman_max_age_s={self.deadman_max_age_s} must be at least "
                    f"{MAX_AGE_DEADLINE_FACTOR:g} x watchdog_deadline_s={self.watchdog_deadline_s} "
                    f"({floor:g} s): a missed tick is not a death"
                )
        return self


def parse_document(loaded: object) -> SloDocument:
    """Validate a decoded document; every refusal is a typed ``SloDocumentError``."""

    if not isinstance(loaded, dict):
        raise SloDocumentError("document malformed: not an object")
    try:
        return SloDocument.model_validate(loaded)
    except ValidationError as error:
        raise SloDocumentError(_first_problem(error)) from error


def _first_problem(error: ValidationError) -> str:
    problems = error.errors()
    if not problems:
        return "invalid document"
    first = problems[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "invalid"))
    if first.get("type") == "extra_forbidden":
        message = "extra field is not permitted"
    return f"{location}: {message}" if location else message


class ObjectiveReport(_Model):
    objective: ObjectiveName
    state: SloState
    target: float
    #: The value the evidence showed, in the objective's unit; None when nothing was measurable.
    measured: float | None
    unit: Literal["ms", "%", "s"]
    #: The window the measurement covered, in seconds; None when no window applied.
    window_s: float | None
    #: Lines that entered the measurement; 0 when nothing did.
    samples: int = Field(ge=0)
    reason: str


class SloEvaluation(_Model):
    schema_version: Literal[1] = 1
    evaluated_at: AwareDatetime
    evidence_dir: str
    state: SloState
    objectives: tuple[ObjectiveReport, ...]


class SloCacheReading(_Model):
    """What ``/health`` learns from the cache: the last evaluation's instant and state, or why."""

    evaluated_at: AwareDatetime | None
    state: SloState | None
    problem: str | None


# ------------------------------------------------------------------ bounded, no-follow reads


def _describe(error: OSError) -> str:
    return {
        errno.ELOOP: "is a symlink (refused, never followed)",
        errno.EISDIR: "is a directory",
        errno.ENXIO: "is a fifo (refused, never blocked on)",
        errno.ENOTDIR: "is not a directory",
    }.get(error.errno or 0, error.strerror or type(error).__name__)


def _component_problem(descriptor: int, component: str, error: OSError) -> str:
    try:
        found = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
    except OSError:
        return _describe(error)
    if stat.S_ISLNK(found.st_mode):
        return "is a symlink (refused, never followed)"
    return _describe(error)


def _open_directory(path: Path) -> tuple[int | None, str | None]:
    """Walk to ``path`` (a directory) component by component, O_NOFOLLOW at every step."""

    absolute = path if path.is_absolute() else Path.cwd() / path
    descriptor = os.open(os.sep, _DIR_FLAGS)
    try:
        for component in absolute.parts[1:]:
            try:
                child = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                os.close(descriptor)
                return None, "absent (the directory does not exist)"
            except OSError as error:
                problem = _component_problem(descriptor, component, error)
                os.close(descriptor)
                return None, f"unreachable: component {component!r} {problem}"
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, None


def _read_entry(
    parent_fd: int, name: str, *, limit: int, tail: bool
) -> tuple[bytes | None, str | None]:
    """The entry's bytes (at most ``limit``; the newest when ``tail``), or the typed reason."""

    try:
        fd = os.open(name, _ENTRY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None, "absent"
    except OSError as error:
        return None, f"unreadable: {_describe(error)}"
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            return None, ("not a regular file (a fifo, directory or device is refused, never read)")
        if tail:
            if metadata.st_size > limit:
                os.lseek(fd, metadata.st_size - limit, os.SEEK_SET)
            raw = os.read(fd, limit)
            if metadata.st_size > limit:
                cut = raw.find(b"\n")
                raw = raw[cut + 1 :] if cut >= 0 else b""
            return raw, None
        raw = os.read(fd, limit + 1)
    except OSError as error:
        return None, f"unreadable: {_describe(error)}"
    finally:
        os.close(fd)
    if len(raw) > limit:
        return None, f"malformed: larger than {limit // 1024} KiB"
    return raw, None


def _read_file(path: Path, *, limit: int) -> tuple[bytes | None, str | None]:
    parent_fd, problem = _open_directory(path.parent)
    if parent_fd is None:
        return None, problem
    try:
        return _read_entry(parent_fd, path.name, limit=limit, tail=False)
    finally:
        os.close(parent_fd)


def _aware(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


# ------------------------------------------------------------------ the document


def load_document(path: Path | str) -> SloDocument:
    """Read and validate the operator's document through the same bounded reader."""

    raw, problem = _read_file(Path(path), limit=_MAX_SMALL_BYTES)
    if raw is None:
        raise SloDocumentError(f"document {problem}")
    try:
        loaded = json.loads(raw)
    except ValueError as error:
        raise SloDocumentError("document malformed: not JSON") from error
    return parse_document(loaded)


# ------------------------------------------------------------------ the evidence


class _Line(NamedTuple):
    assessed_at: datetime
    monotonic: float
    state: str
    elapsed_ms: float
    verdict: str


def _parse_lines(raw: bytes) -> tuple[list[_Line], str | None]:
    """Every line of the log, typed; a torn LAST line is where the writer died and is dropped."""

    text = raw.decode("utf-8", errors="replace")
    pieces = text.split("\n")
    if pieces and pieces[-1] == "":
        pieces.pop()
    elif pieces:
        pieces.pop()  # torn tail: no newline, the writer died here
    lines: list[_Line] = []
    for number, piece in enumerate(pieces, start=1):
        try:
            document = json.loads(piece)
        except ValueError:
            return [], f"log malformed: line {number} is not JSON"
        if not isinstance(document, dict):
            return [], f"log malformed: line {number} is not an object"
        assessed_at = _aware(document.get("assessed_at"))
        monotonic = _finite(document.get("monotonic"))
        elapsed_ms = _finite(document.get("elapsed_ms"))
        state = document.get("state")
        verdict = document.get("verdict")
        if (
            assessed_at is None
            or monotonic is None
            or elapsed_ms is None
            or elapsed_ms < 0
            or not isinstance(state, str)
            or not isinstance(verdict, str)
        ):
            return [], f"log malformed: line {number} lacks a typed field"
        lines.append(_Line(assessed_at, monotonic, state, elapsed_ms, verdict))
    return lines, None


class _Evidence:
    """One read of the evidence directory: the log's lines (or why not) and the heartbeat's age."""

    def __init__(self, evidence_dir: Path, now: datetime) -> None:
        self.now = now
        self.lines: list[_Line] = []
        self.log_problem: str | None = None
        self.heartbeat_age_s: float | None = None
        self.heartbeat_problem: str | None = None
        parent_fd, problem = _open_directory(evidence_dir)
        if parent_fd is None:
            self.log_problem = f"evidence directory {problem}"
            self.heartbeat_problem = self.log_problem
            return
        try:
            raw, log_problem = _read_entry(parent_fd, EVIDENCE_LOG, limit=_MAX_LOG_BYTES, tail=True)
            if raw is None:
                self.log_problem = f"log {log_problem}"
            else:
                self.lines, self.log_problem = _parse_lines(raw)
            raw, heartbeat_problem = _read_entry(
                parent_fd, HEARTBEAT, limit=_MAX_SMALL_BYTES, tail=False
            )
        finally:
            os.close(parent_fd)
        if raw is None:
            self.heartbeat_problem = f"heartbeat {heartbeat_problem}"
            return
        self.heartbeat_age_s, self.heartbeat_problem = self._heartbeat_age(raw)

    def _heartbeat_age(self, raw: bytes) -> tuple[float | None, str | None]:
        try:
            document = json.loads(raw)
        except ValueError:
            return None, "heartbeat malformed: not JSON"
        if not isinstance(document, dict):
            return None, "heartbeat malformed: not an object"
        observed_at = _aware(document.get("last_observed_at"))
        if observed_at is None:
            return None, "heartbeat malformed: last_observed_at missing or not an aware timestamp"
        age = round((self.now - observed_at).total_seconds(), 3)
        if age < -FUTURE_TOLERANCE_S:
            return (
                None,
                f"heartbeat is {-age:.1f} s in the future: the clock cannot be reasoned about",
            )
        return max(age, 0.0), None

    def window(self, window_s: float | None) -> tuple[list[_Line], float | None, str | None]:
        """The lines within ``window_s`` of ``now`` (all of them when None) and the window used."""

        if self.log_problem is not None:
            return [], window_s, self.log_problem
        if not self.lines:
            return [], window_s, "log empty"
        span = round((self.now - self.lines[0].assessed_at).total_seconds(), 3)
        if window_s is None:
            selected = [
                line
                for line in self.lines
                if (self.now - line.assessed_at).total_seconds() >= -FUTURE_TOLERANCE_S
            ]
            return selected, max(span, 0.0), None
        if span < window_s:
            return (
                [],
                window_s,
                f"log covers {span:.1f} s of the {window_s:.1f} s window: shorter than the window",
            )
        selected = [
            line
            for line in self.lines
            if -FUTURE_TOLERANCE_S <= (self.now - line.assessed_at).total_seconds() <= window_s
        ]
        return selected, window_s, None


def _cadence_s(lines: Iterable[_Line]) -> float | None:
    """The cadence the writer kept: the median spacing of consecutive monotonic values."""

    values = [line.monotonic for line in lines]
    deltas = [later - earlier for earlier, later in pairwise(values)]
    positive = [delta for delta in deltas if delta > 0]
    if not positive:
        return None
    return round(statistics.median(positive), 3)


def _nearest_rank_p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _unknown(
    objective: ObjectiveName,
    target: float,
    unit: Literal["ms", "%", "s"],
    window_s: float | None,
    reason: str,
) -> ObjectiveReport:
    return ObjectiveReport(
        objective=objective,
        state=SloState.UNKNOWN,
        target=target,
        measured=None,
        unit=unit,
        window_s=window_s,
        samples=0,
        reason=reason,
    )


def _judged(
    objective: ObjectiveName,
    target: float,
    unit: Literal["ms", "%", "s"],
    window_s: float | None,
    samples: int,
    measured: float,
    *,
    met: bool,
    detail: str,
) -> ObjectiveReport:
    state = SloState.MET if met else SloState.BREACHED
    return ObjectiveReport(
        objective=objective,
        state=state,
        target=target,
        measured=measured,
        unit=unit,
        window_s=window_s,
        samples=samples,
        reason=detail,
    )


def _latency(evidence: _Evidence, target: float, window_s: float) -> ObjectiveReport:
    lines, used, problem = evidence.window(window_s)
    if problem is not None:
        return _unknown("probe_latency_p95_ms", target, "ms", used, problem)
    if not lines:
        return _unknown("probe_latency_p95_ms", target, "ms", used, "no line within the window")
    p95 = round(_nearest_rank_p95([line.elapsed_ms for line in lines]), 3)
    return _judged(
        "probe_latency_p95_ms",
        target,
        "ms",
        used,
        len(lines),
        p95,
        met=p95 <= target,
        detail=f"p95 {p95:.3f} ms over {len(lines)} lines (budget {target:g} ms)",
    )


def _availability(evidence: _Evidence, target: float, window_s: float) -> ObjectiveReport:
    lines, used, problem = evidence.window(window_s)
    if problem is not None:
        return _unknown("readiness_availability_pct", target, "%", used, problem)
    if not lines:
        return _unknown(
            "readiness_availability_pct", target, "%", used, "no line within the window"
        )
    healthy = sum(1 for line in lines if line.state == "HEALTHY")
    pct = round(100.0 * healthy / len(lines), 3)
    return _judged(
        "readiness_availability_pct",
        target,
        "%",
        used,
        len(lines),
        pct,
        met=pct >= target,
        detail=f"{healthy} of {len(lines)} lines HEALTHY = {pct:.3f} % (objective {target:g} %)",
    )


def _deadline(evidence: _Evidence, target: float, window_s: float | None) -> ObjectiveReport:
    lines, used, problem = evidence.window(window_s)
    if problem is not None:
        return _unknown("watchdog_deadline_s", target, "s", used, problem)
    cadence = _cadence_s(lines)
    if cadence is None:
        return _unknown(
            "watchdog_deadline_s",
            target,
            "s",
            used,
            "the cadence is unmeasurable: fewer than two lines within the window",
        )
    floor = DEADLINE_CADENCE_FACTOR * cadence
    if target < floor:
        raise SloDocumentError(
            f"watchdog_deadline_s={target} must be at least {DEADLINE_CADENCE_FACTOR:g} x the "
            f"cadence the evidence records ({cadence} s → {floor:g} s): a missed tick is not "
            "an outage"
        )
    longest = 0.0
    anchor = lines[0].monotonic
    tripped = any(line.verdict == "TRIPPED" for line in lines)
    for line in lines:
        longest = max(longest, line.monotonic - anchor)
        if line.state == "HEALTHY":
            anchor = line.monotonic
    longest = round(longest, 3)
    detail = (
        f"longest stretch without HEALTHY {longest:.3f} s over {len(lines)} lines "
        f"(deadline {target:g} s)"
    )
    if tripped:
        detail += "; a TRIPPED verdict is recorded within the window"
    return _judged(
        "watchdog_deadline_s",
        target,
        "s",
        used,
        len(lines),
        longest,
        met=longest < target and not tripped,
        detail=detail,
    )


def _deadman(evidence: _Evidence, target: float) -> ObjectiveReport:
    if evidence.heartbeat_problem is not None:
        return _unknown("deadman_max_age_s", target, "s", None, evidence.heartbeat_problem)
    age = evidence.heartbeat_age_s
    if age is None:
        return _unknown("deadman_max_age_s", target, "s", None, "heartbeat age unmeasurable")
    return _judged(
        "deadman_max_age_s",
        target,
        "s",
        None,
        1,
        age,
        met=age <= target,
        detail=f"heartbeat {age:.1f} s old (max {target:g} s)",
    )


def _clock(evidence: _Evidence, target: float, window_s: float | None) -> ObjectiveReport:
    lines, used, problem = evidence.window(window_s)
    if problem is not None:
        return _unknown("clock_max_error_s", target, "s", used, problem)
    if len(lines) < 2:
        return _unknown(
            "clock_max_error_s",
            target,
            "s",
            used,
            "a disagreement needs two lines within the window",
        )
    worst = 0.0
    for earlier, later in pairwise(lines):
        wall = (later.assessed_at - earlier.assessed_at).total_seconds()
        mono = later.monotonic - earlier.monotonic
        worst = max(worst, abs(wall - mono))
    worst = round(worst, 3)
    return _judged(
        "clock_max_error_s",
        target,
        "s",
        used,
        len(lines),
        worst,
        met=worst <= target,
        detail=f"largest wall-versus-monotonic disagreement {worst:.3f} s over {len(lines)} lines "
        f"(max {target:g} s)",
    )


def _overall(reports: Iterable[ObjectiveReport]) -> SloState:
    states = {report.state for report in reports}
    if SloState.BREACHED in states:
        return SloState.BREACHED
    if SloState.UNKNOWN in states:
        return SloState.UNKNOWN
    return SloState.MET


def evaluate(evidence_dir: Path | str, document: SloDocument, now: datetime) -> SloEvaluation:
    """Judge the document against one read of the evidence. Reads two files; writes nothing.

    Raises ``SloDocumentError`` when the evidence shows the document contradicts the cadence
    rule (a deadline under 3 x the recorded cadence) — the document, not the evidence, is wrong.
    """

    if now.tzinfo is None or now.utcoffset() is None:
        raise SloDocumentError("now must be timezone-aware")
    directory = Path(evidence_dir)
    evidence = _Evidence(directory, now)
    reports: list[ObjectiveReport] = []
    if document.probe_latency_p95_ms is not None and document.window_s is not None:
        reports.append(_latency(evidence, document.probe_latency_p95_ms, document.window_s))
    if document.readiness_availability_pct is not None and document.window_s is not None:
        reports.append(
            _availability(evidence, document.readiness_availability_pct, document.window_s)
        )
    if document.watchdog_deadline_s is not None:
        reports.append(_deadline(evidence, document.watchdog_deadline_s, document.window_s))
    if document.deadman_max_age_s is not None:
        reports.append(_deadman(evidence, document.deadman_max_age_s))
    if document.clock_max_error_s is not None:
        reports.append(_clock(evidence, document.clock_max_error_s, document.window_s))
    return SloEvaluation(
        evaluated_at=now,
        evidence_dir=str(directory),
        state=_overall(reports),
        objectives=tuple(reports),
    )


# ------------------------------------------------------------------ the cache


def write_evaluation_cache(evidence_dir: Path | str, evaluation: SloEvaluation) -> Path:
    """Publish the evaluation as ``slo-evaluation.json`` beside the evidence, atomically.

    A unique ``O_EXCL`` 0600 temp in the same directory, every byte written, fsync, rename
    over the name, directory fsync. An entry already at the name must be a regular file: a
    symlink, fifo or directory is refused typed and left in place. Creates no directory.
    """

    directory = Path(evidence_dir)
    target = directory / EVALUATION_CACHE
    parent_fd, problem = _open_directory(directory)
    if parent_fd is None:
        raise SloCacheError(f"{directory}: evidence directory {problem}")
    try:
        try:
            found: os.stat_result | None = os.stat(
                EVALUATION_CACHE, dir_fd=parent_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            found = None
        except OSError as error:
            raise SloCacheError(f"{target}: {_describe(error)}") from error
        if found is not None:
            if stat.S_ISLNK(found.st_mode):
                raise SloCacheError(f"{target}: is a symlink (refused, never followed or replaced)")
            if not stat.S_ISREG(found.st_mode):
                raise SloCacheError(
                    f"{target}: not a regular file (a fifo, directory or device is refused, "
                    "never replaced)"
                )
        temp_name = f".{EVALUATION_CACHE}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(temp_name, _TEMP_FLAGS, 0o600, dir_fd=parent_fd)
        except OSError as error:
            raise SloCacheError(f"{target}: could not create a temp: {_describe(error)}") from error
        published = False
        try:
            content = evaluation.model_dump_json().encode("utf-8")
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
            os.rename(temp_name, EVALUATION_CACHE, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            published = True
            os.fsync(parent_fd)
        except OSError as error:
            raise SloCacheError(f"{target}: could not publish: {_describe(error)}") from error
        finally:
            os.close(fd)
            if not published:
                with contextlib.suppress(OSError):
                    os.unlink(temp_name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    return target


def read_evaluation_cache(path: Path | str) -> SloCacheReading:
    """What the cache says, for ``/health``: never raises; a problem is typed in ``problem``."""

    raw, problem = _read_file(Path(path), limit=_MAX_SMALL_BYTES)
    if raw is None:
        return SloCacheReading(evaluated_at=None, state=None, problem=f"cache {problem}")
    try:
        evaluation = SloEvaluation.model_validate_json(raw)
    except ValidationError:
        return SloCacheReading(
            evaluated_at=None, state=None, problem="cache malformed: not an evaluation"
        )
    return SloCacheReading(
        evaluated_at=evaluation.evaluated_at, state=evaluation.state, problem=None
    )


# ------------------------------------------------------------------ the CLI


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronos-slo",
        description=(
            "Offline SLO evaluator over the watchdog's evidence files: MET (0), BREACHED (2), "
            "UNKNOWN (3), bad document (64). Reads two files, publishes one cache, acts on nothing."
        ),
    )
    parser.add_argument(
        "--evidence-dir", type=Path, required=True, help="the watchdog's --evidence-dir"
    )
    parser.add_argument("--slo", type=Path, required=True, help="path to the SLO document (JSON)")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        document = load_document(args.slo)
        evaluation = evaluate(args.evidence_dir, document, utc_now())
    except SloDocumentError as error:
        print(f"slo document error: {error}", file=sys.stderr)
        return EXIT_BAD_DOCUMENT
    exit_code = EXIT_CODES[evaluation.state]
    try:
        write_evaluation_cache(args.evidence_dir, evaluation)
    except SloCacheError as error:
        print(f"slo cache not published: {error}", file=sys.stderr)
        if exit_code == EXIT_CODES[SloState.MET]:
            exit_code = EXIT_CODES[SloState.UNKNOWN]  # unpublished: UNKNOWN to /health
    print(evaluation.model_dump_json(indent=2 if args.pretty else None))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
