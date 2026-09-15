"""Dead-man check over the watchdog's heartbeat (M3 ops plane, second layer).

``check_deadman`` reads ``heartbeat.json`` — the file ``chronos.operations.watchdog``
replaces on every tick — and says whether that watchdog is still alive. It keeps no state,
writes nothing, contacts nothing, and cannot act: a DEAD verdict is evidence for the
operator, whose response is the runbook's (``docs/ops/WATCHDOG.md``).

The verdict rule. DEAD when the heartbeat is absent, unreadable, malformed, not a regular
file, or older than ``max_age_s`` by BOTH the wall clock and the monotonic timer. ALIVE when
both say it is within ``max_age_s``. UNKNOWN only when the clock evidence cannot be
reasoned about: the two clocks disagree (one of them moved), the heartbeat's monotonic
value precedes this process's timer (another boot, or another host — CLOCK_MONOTONIC is
comparable only within one boot of one host), or the heartbeat is from the future beyond a
small tolerance. UNKNOWN is not ALIVE; it exits 3 so an operator looks.

The heartbeat is opened by descriptor with ``O_NOFOLLOW|O_NONBLOCK`` and must be a regular
file (``fstat``): a planted symlink is refused (never followed, even to a fresh file), a
planted FIFO is refused instead of blocking the check, a directory is refused. Reads are
bounded to 64 KiB.
"""

from __future__ import annotations

import argparse
import errno
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

from chronos.utils.time import utc_now

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_ENTRY_FLAGS = os.O_RDONLY | _CLOEXEC | _NOFOLLOW | _NONBLOCK
_MAX_HEARTBEAT_BYTES = 64 * 1024
#: A heartbeat may be a little ahead of this process's wall clock (two hosts, two NTP
#: states); beyond this it is not evidence this check can reason about.
FUTURE_TOLERANCE_S = 5.0


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DeadmanState(StrEnum):
    ALIVE = "ALIVE"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"


EXIT_CODES = {DeadmanState.ALIVE: 0, DeadmanState.DEAD: 2, DeadmanState.UNKNOWN: 3}


class DeadmanVerdict(_Model):
    state: DeadmanState
    reason: str
    #: Wall-clock age of the heartbeat in seconds; None when no age could be read.
    heartbeat_age_s: float | None
    assessed_at: AwareDatetime


class DeadmanConfigurationError(ValueError):
    """A parameter that cannot describe a check."""


def _describe(error: OSError) -> str:
    return {
        errno.ELOOP: "is a symlink (refused, never followed)",
        errno.EISDIR: "is a directory",
        errno.ENXIO: "is a fifo (refused, never blocked on)",
    }.get(error.errno or 0, error.strerror or type(error).__name__)


def _read_heartbeat(path: Path) -> tuple[bytes | None, str | None]:
    """The heartbeat's bytes, or the typed reason they could not be read."""

    try:
        fd = os.open(path, _ENTRY_FLAGS)
    except FileNotFoundError:
        return None, "heartbeat absent"
    except OSError as error:
        return None, f"heartbeat unreadable: {_describe(error)}"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, (
                "heartbeat is not a regular file (a fifo, directory or device is refused, "
                "never read)"
            )
        raw = os.read(fd, _MAX_HEARTBEAT_BYTES + 1)
    except OSError as error:
        return None, f"heartbeat unreadable: {_describe(error)}"
    finally:
        os.close(fd)
    if len(raw) > _MAX_HEARTBEAT_BYTES:
        return None, "heartbeat malformed: larger than 64 KiB"
    return raw, None


def _parse_heartbeat(raw: bytes) -> tuple[datetime | None, float | None, str | None]:
    """(last_observed_at, monotonic) from the heartbeat, or the typed malformation."""

    try:
        document = json.loads(raw)
    except ValueError:
        return None, None, "heartbeat malformed: not JSON"
    if not isinstance(document, dict):
        return None, None, "heartbeat malformed: not an object"
    observed = document.get("last_observed_at")
    if not isinstance(observed, str):
        return None, None, "heartbeat malformed: last_observed_at missing or not a string"
    try:
        observed_at = datetime.fromisoformat(observed)
    except ValueError:
        return None, None, "heartbeat malformed: last_observed_at is not an ISO-8601 timestamp"
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        return None, None, "heartbeat malformed: last_observed_at carries no timezone"
    monotonic = document.get("monotonic")
    if (
        isinstance(monotonic, bool)
        or not isinstance(monotonic, int | float)
        or not math.isfinite(monotonic)
    ):
        return None, None, "heartbeat malformed: monotonic missing or not a finite number"
    return observed_at, float(monotonic), None


def check_deadman(
    heartbeat_path: Path | str,
    max_age_s: float,
    *,
    clock: Callable[[], datetime] = utc_now,
    timer: Callable[[], float] = time.monotonic,
) -> DeadmanVerdict:
    """Judge the watchdog's heartbeat once. Reads one file; retains and writes nothing."""

    if (
        isinstance(max_age_s, bool)
        or not isinstance(max_age_s, int | float)
        or not math.isfinite(max_age_s)
        or max_age_s <= 0
    ):
        raise DeadmanConfigurationError("max_age_s must be a positive finite number")
    now = clock()
    now_monotonic = timer()

    def verdict(state: DeadmanState, reason: str, age: float | None = None) -> DeadmanVerdict:
        return DeadmanVerdict(state=state, reason=reason, heartbeat_age_s=age, assessed_at=now)

    raw, failure = _read_heartbeat(Path(heartbeat_path))
    if raw is None:
        return verdict(DeadmanState.DEAD, failure or "heartbeat unreadable")
    observed_at, monotonic, malformed = _parse_heartbeat(raw)
    if observed_at is None or monotonic is None:
        return verdict(DeadmanState.DEAD, malformed or "heartbeat malformed")

    wall_age = round((now - observed_at).total_seconds(), 3)
    monotonic_age = round(now_monotonic - monotonic, 3)
    limit = f"max {max_age_s:.1f} s"
    if wall_age < -FUTURE_TOLERANCE_S:
        return verdict(
            DeadmanState.UNKNOWN,
            f"heartbeat is {-wall_age:.1f} s in the future: "
            "the wall clock cannot be reasoned about",
            wall_age,
        )
    if monotonic_age < 0:
        return verdict(
            DeadmanState.UNKNOWN,
            f"heartbeat monotonic {monotonic:.3f} is ahead of this process's timer "
            f"{now_monotonic:.3f}: written under another boot or on another host",
            wall_age,
        )
    wall_stale = wall_age > max_age_s
    monotonic_stale = monotonic_age > max_age_s
    if wall_stale and monotonic_stale:
        return verdict(
            DeadmanState.DEAD,
            f"no heartbeat for {wall_age:.1f} s wall / {monotonic_age:.1f} s monotonic ({limit})",
            wall_age,
        )
    if not wall_stale and not monotonic_stale:
        return verdict(
            DeadmanState.ALIVE,
            f"heartbeat {wall_age:.1f} s wall / {monotonic_age:.1f} s monotonic old ({limit})",
            wall_age,
        )
    return verdict(
        DeadmanState.UNKNOWN,
        f"wall and monotonic evidence disagree ({wall_age:.1f} s wall, {monotonic_age:.1f} s "
        f"monotonic, {limit}): a clock moved",
        wall_age,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronos-deadman",
        description=(
            "Dead-man check over the watchdog's heartbeat.json: ALIVE (0), DEAD (2) or "
            "UNKNOWN (3). Reads one file, writes nothing, acts on nothing."
        ),
    )
    parser.add_argument("--heartbeat", type=Path, required=True, help="path to heartbeat.json")
    parser.add_argument(
        "--max-age", type=float, default=180.0, help="seconds before a heartbeat is stale"
    )
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = check_deadman(args.heartbeat, args.max_age)
    except DeadmanConfigurationError as error:
        print(f"deadman configuration error: {error}", file=sys.stderr)
        return 64
    print(result.model_dump_json(indent=2 if args.pretty else None))
    return EXIT_CODES[result.state]


if __name__ == "__main__":
    sys.exit(main())
