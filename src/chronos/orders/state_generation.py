"""The installation marker that tells a lost safety file from a fresh install.

Both durable live-safety files answer a missing file with the permissive reading,
and on a genuinely fresh install that is the correct one: the kill switch has
never been engaged, the session baseline has never been established, and there is
nothing to have lost. The two cases are indistinguishable from the file's absence
alone — which is the whole defect. A restore that omits the state directory, a
container that lost its sidecar volume, or an operator who deleted a file to
"clear" it all present exactly as a fresh install, and the permissive reading
resumes trading with no kill switch and a baseline re-established at whatever the
account is worth after the loss.

This marker separates them. It is written once, beside the state files, and each
component records in it the moment it first materialises its own file. From then
on the component's missing file is not absence, it is loss, and loss reads closed:

* kill switch — a materialised-then-missing file reads ENGAGED;
* session drawdown — a materialised-then-missing baseline refuses rather than
  re-baselining at the post-loss net liquidation value.

The marker is deliberately dumb: an installation id, when it was created, and the
set of components that have materialised state under it. It grants nothing and
gates nothing by itself; the components read it and fail closed. A corrupt or
unreadable marker is treated as "everything was materialised", because a marker
we cannot read is not evidence that a missing file was never written.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Final
from uuid import uuid4

from chronos.domain.models import ChronosModel

_SCHEMA_VERSION: Final = 1

#: The components that materialise durable live-safety state under one
#: installation. Closed on purpose: a new component must be added here and to the
#: read path that fails closed for it, so it cannot be forgotten silently.
KILL_SWITCH: Final = "kill_switch"
SESSION_DRAWDOWN: Final = "session_drawdown"
_COMPONENTS: Final = frozenset({KILL_SWITCH, SESSION_DRAWDOWN})

DEFAULT_MARKER_NAME: Final = "state_generation.json"


class StateGeneration(ChronosModel):
    """What one installation of the live-safety state directory knows about itself."""

    installation_id: str
    created_at: datetime
    materialized: tuple[str, ...] = ()


class StateGenerationMarker:
    """Reader/writer for one state directory's installation marker."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def beside(cls, state_file: Path) -> StateGenerationMarker:
        """The marker that governs a state file living in the same directory."""

        return cls(state_file.parent / DEFAULT_MARKER_NAME)

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> StateGeneration | None:
        """The marker, ``None`` if this installation has never written one.

        A present-but-unreadable marker raises: the caller's fail-closed branch
        owns that decision, and silently treating corruption as absence would
        restore exactly the permissive reading this exists to remove.
        """

        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise CorruptStateGeneration(f"state-generation marker unreadable: {error}") from error
        if not isinstance(payload, dict):
            raise CorruptStateGeneration("state-generation marker is not a JSON object")
        if payload.get("schema") != _SCHEMA_VERSION:
            raise CorruptStateGeneration(
                f"unsupported state-generation schema {payload.get('schema')!r}"
            )
        installation_id = payload.get("installation_id")
        created_raw = payload.get("created_at")
        if not isinstance(installation_id, str) or not installation_id:
            raise CorruptStateGeneration("state-generation marker has no installation id")
        if not isinstance(created_raw, str):
            raise CorruptStateGeneration("state-generation marker has no creation timestamp")
        try:
            created_at = datetime.fromisoformat(created_raw)
        except ValueError as error:
            raise CorruptStateGeneration(
                f"state-generation timestamp is invalid: {error}"
            ) from error
        if created_at.tzinfo is None:
            raise CorruptStateGeneration("state-generation timestamp must be timezone-aware")
        raw_components = payload.get("materialized", [])
        if not isinstance(raw_components, list) or not all(
            isinstance(entry, str) for entry in raw_components
        ):
            raise CorruptStateGeneration("state-generation materialized list is malformed")
        return StateGeneration(
            installation_id=installation_id,
            created_at=created_at,
            materialized=tuple(sorted(set(raw_components) & _COMPONENTS)),
        )

    def was_materialized(self, component: str) -> bool:
        """Has ``component`` ever written its state file under this installation?

        ``True`` when the marker says so and when the marker itself cannot be
        read — an unreadable marker is not evidence of a fresh install.
        """

        if component not in _COMPONENTS:
            raise ValueError(f"unknown live-safety state component {component!r}")
        try:
            generation = self.read()
        except CorruptStateGeneration:
            return True
        if generation is None:
            return False
        return component in generation.materialized

    def ensure_installation(self, installation_id: str, *, now: datetime) -> str:
        """Name this state directory's installation, once, without claiming a write.

        The marker is otherwise created lazily by whichever component first
        materialises its own file, which leaves a window where the directory has
        an identity nobody has written down. ADR-0054's cross-store witness needs
        that identity to exist from the first boot, so the backend seeds it here
        with an **empty** ``materialized`` set: seeding says "this installation
        exists", never "a file was written", so every R-66 reading is unchanged.

        An existing marker is never rewritten and its id is returned instead --
        that id is the evidence the cross-store comparison is about, and a
        reseed would manufacture the agreement the comparison exists to test.
        """

        if not installation_id:
            raise ValueError("an installation id must not be blank")
        if now.tzinfo is None:
            raise ValueError("seeding an installation requires a timezone-aware timestamp")
        existing = self.read()
        if existing is not None:
            return existing.installation_id
        self._write(
            StateGeneration(installation_id=installation_id, created_at=now, materialized=())
        )
        return installation_id

    def record_materialized(self, component: str, *, now: datetime) -> None:
        """Record that ``component`` has just written its state file.

        Called by the component immediately after a successful durable write, so
        the marker never claims a materialisation that did not happen. The write
        is atomic in the same sense as the state files beside it -- temp file,
        fsync, rename, fsync the directory -- and read-modify-write, so a second
        component recording concurrently merges rather than replaces. Two writers
        racing the very first creation can still lose one component's flag; the
        losing component records again on its next durable write, and the failure
        direction of a missing flag is the permissive one only until then.
        """

        if component not in _COMPONENTS:
            raise ValueError(f"unknown live-safety state component {component!r}")
        if now.tzinfo is None:
            raise ValueError("recording a materialisation requires a timezone-aware timestamp")
        try:
            existing = self.read()
        except CorruptStateGeneration:
            # Refuse to overwrite a marker we cannot read: a rewrite here would
            # discard another component's recorded materialisation and hand back
            # the permissive reading for it.
            return
        if existing is None:
            installation_id = uuid4().hex
            created_at = now
            components = {component}
        else:
            installation_id = existing.installation_id
            created_at = existing.created_at
            components = set(existing.materialized) | {component}
            if components == set(existing.materialized):
                return
        self._write(
            StateGeneration(
                installation_id=installation_id,
                created_at=created_at,
                materialized=tuple(sorted(components)),
            )
        )

    def _write(self, generation: StateGeneration) -> None:
        payload = {
            "schema": _SCHEMA_VERSION,
            "installation_id": generation.installation_id,
            "created_at": generation.created_at.isoformat(),
            "materialized": list(generation.materialized),
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
        # fsync the directory too: a marker that does not survive power loss
        # would hand back the fresh-install reading for a file that did survive.
        dir_fd = os.open(str(self._path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


class CorruptStateGeneration(Exception):
    """The marker is present but cannot be read as this schema."""
