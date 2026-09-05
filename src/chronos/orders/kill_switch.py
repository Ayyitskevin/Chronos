"""Live-Wheel kill switch: a durable, fail-closed emergency stop.

Distinct from the autonomous platform's ``chronos.control.halt.HaltStore``
(whose fresh-deploy default is HALTED): this kill switch defaults DISENGAGED on
a *fresh install* so a new backend trades subject to the *other* gates, but it
fails CLOSED (reads as ENGAGED) on a corrupt or unreadable file — and, since
R-66, on a file that is missing after this installation wrote one, which is a
restore or a lost volume rather than a fresh install
(``chronos.orders.state_generation``). Any component may engage it. Writes are
atomic (temp file + fsync + rename) so a crash cannot leave a torn flag, and an
engaged switch survives a restart.

Only an explicit operator ``disengage`` (with a non-empty note) clears it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Protocol

from chronos.domain.models import ChronosModel
from chronos.orders.state_generation import KILL_SWITCH, StateGenerationMarker

_SCHEMA_VERSION = 1


class KillSwitchState(ChronosModel):
    engaged: bool
    reason: str = ""
    initiated_by: str = ""
    engaged_at: datetime | None = None
    note: str = ""


class KillSwitchAuditSink(Protocol):
    def record(
        self, *, action: str, initiated_by: str, detail: dict[str, object], occurred_at: datetime
    ) -> None: ...


class NullKillSwitchAuditSink:
    def record(
        self, *, action: str, initiated_by: str, detail: dict[str, object], occurred_at: datetime
    ) -> None:
        del action, initiated_by, detail, occurred_at


def _write_moment(state: KillSwitchState) -> datetime:
    """The timestamp to stamp a materialisation with.

    An engagement carries its own moment; a disengagement does not, and the
    marker only needs a creation time for the first write of an installation.
    """

    return state.engaged_at or datetime.now(tz=UTC)


class LiveKillSwitch:
    """Durable emergency stop with a DISENGAGED default and fail-closed reads."""

    def __init__(
        self,
        path: Path,
        *,
        audit: KillSwitchAuditSink | None = None,
        generation: StateGenerationMarker | None = None,
    ) -> None:
        self._path = path
        self._audit = audit or NullKillSwitchAuditSink()
        self._lock = Lock()
        # The installation marker that separates "never written" from "written
        # and now gone" (R-66). It lives beside the state file, so a restore or a
        # lost volume that takes one takes both -- and a marker missing with the
        # state file missing is the fresh-install case, which stays DISENGAGED.
        self._generation = generation or StateGenerationMarker.beside(path)

    def read(self) -> KillSwitchState:
        with self._lock:
            return self._read_unlocked()

    def _read_unlocked(self) -> KillSwitchState:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"kill-switch file must be a JSON object, got {type(payload)}")
            if payload.get("schema") != _SCHEMA_VERSION:
                raise ValueError(f"unsupported kill-switch schema {payload.get('schema')!r}")
            engaged = bool(payload["engaged"])
            engaged_raw = payload.get("engaged_at")
            engaged_at = datetime.fromisoformat(engaged_raw) if engaged_raw else None
            if engaged_at is not None and engaged_at.tzinfo is None:
                raise ValueError("kill-switch timestamp must be timezone-aware")
            if engaged and engaged_at is None:
                raise ValueError("an engaged kill switch requires an engaged_at timestamp")
            return KillSwitchState(
                engaged=engaged,
                reason=str(payload.get("reason", "")),
                initiated_by=str(payload.get("initiated_by", "")),
                engaged_at=engaged_at,
                note=str(payload.get("note", "")),
            )
        except FileNotFoundError:
            if self._generation.was_materialized(KILL_SWITCH):
                # This installation wrote a kill-switch file and no longer has
                # one: a restore that omitted it, a lost sidecar volume, or a
                # deletion. The state it recorded is unknown, and an unknown
                # emergency stop is an engaged one (R-66).
                return KillSwitchState(
                    engaged=True,
                    reason=(
                        "kill-switch file is missing but this installation wrote one; "
                        "failing closed until an operator disengages with a note"
                    ),
                    initiated_by="system",
                )
            # No file and none was ever written: a fresh deploy is DISENGAGED
            # (trades subject to the other gates).
            return KillSwitchState(engaged=False)
        except (OSError, ValueError, KeyError, TypeError) as error:
            # Unreadable/corrupt: fail closed — assume ENGAGED.
            return KillSwitchState(
                engaged=True,
                reason=f"kill-switch file unreadable; failing closed: {error}",
                initiated_by="system",
            )

    def is_engaged(self) -> bool:
        return self.read().engaged

    @contextmanager
    def at_send(self) -> Iterator[bool]:
        """Serialize engagement against one synchronous broker send primitive."""

        with self._lock:
            yield not self._read_unlocked().engaged

    def engage(self, *, reason: str, initiated_by: str, now: datetime) -> KillSwitchState:
        detail = reason.strip()
        if not detail:
            raise ValueError("engaging the kill switch requires a non-empty reason")
        state = KillSwitchState(
            engaged=True, reason=detail, initiated_by=initiated_by, engaged_at=now
        )
        with self._lock:
            self._write(state)
        self._audit.record(
            action="engage",
            initiated_by=initiated_by,
            detail={"reason": detail},
            occurred_at=now,
        )
        return state

    def disengage(self, *, operator_note: str, initiated_by: str, now: datetime) -> KillSwitchState:
        note = operator_note.strip()
        if not note:
            raise ValueError("disengaging the kill switch requires a non-empty operator note")
        state = KillSwitchState(engaged=False, note=note, initiated_by=initiated_by)
        with self._lock:
            self._audit.record(
                action="disengage",
                initiated_by=initiated_by,
                detail={"note": note},
                occurred_at=now,
            )
            self._write(state)
        return state

    def _write(self, state: KillSwitchState) -> None:
        payload = {
            "schema": _SCHEMA_VERSION,
            "engaged": state.engaged,
            "reason": state.reason,
            "initiated_by": state.initiated_by,
            "engaged_at": state.engaged_at.isoformat() if state.engaged_at is not None else None,
            "note": state.note,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(".tmp")
        descriptor = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, json.dumps(payload, indent=2).encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temp, self._path)
        dir_fd = os.open(str(self._path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        # Record the materialisation only after the durable write succeeded, so
        # the marker never claims a file that was never there.
        self._generation.record_materialized(KILL_SWITCH, now=_write_moment(state))
