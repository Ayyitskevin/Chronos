"""A boot that follows a restore comes up read-only and unreconciled (ADR-0054).

``chronos.orders.state_generation`` (R-66) closed one half of
``docs/VISION_COMPLETION_PLAN.md`` §6 finding 3: a state file missing *after this
installation wrote one* reads closed instead of reading as a fresh install. Its own
ADR named what it left open -- booting **read-only** and **unreconciled**, and the
mandate's auto-activation on that boot -- and disclosed the residual that a restore
bringing back nothing, marker included, still presents as a fresh install.

The marker cannot close either on its own, because it is one store: the absence of
the marker and the absence of everything are the same bytes. This module adds the
second witness. The marker's installation id is recorded in the Chronos database,
and the two are compared at every writer startup:

===========================  ==========================  ==========================
marker                       database identity row       reading
===========================  ==========================  ==========================
absent                       present                     the state directory is gone
present                      absent                      the database was replaced
present                      present, different id       two snapshots mixed
unreadable                   any                         proves nothing; fail closed
present                      present, same id            consistent
absent                       absent                      a fresh install
===========================  ==========================  ==========================

**What this cannot prove, and it matters.** ``live_kill_switch.json`` and
``chronos.db`` live in the same directory by default, so a self-consistent wholesale
restore of that directory carries both witnesses from one snapshot and is
byte-indistinguishable from a clean restart. Only something outside the directory can
tell, which is why ``chronos.recovery restore`` leaves ``recovery_pending.json``
behind and why the manual restore procedure asks the operator to create one. A
wholesale restore done by hand without that step is the disclosed residual (R-72);
the runbook's manual step remains its only control.

An observation is cleared by a typed operator act carrying a note, the shape the kill
switch already uses. The acknowledgement binds the **exact** observation -- reason,
both ids, and the witness token -- so it retires the restore it was written for and
never becomes a standing permission to ignore restores.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Final
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from chronos.orders.state_generation import CorruptStateGeneration, StateGenerationMarker
from chronos.persistence.schema import InstallationIdentityRow, RecoveryAcknowledgementRow

#: Left in a restored state directory by ``chronos.recovery restore``, and created
#: by hand in the manual procedure. Presence is the signal; the token inside only
#: distinguishes one restore from the next.
RECOVERY_PENDING_NAME: Final = "recovery_pending.json"

#: A witness file we cannot parse is still a witness. It gets a stable stand-in
#: token so an acknowledgement can bind it, rather than reading as absence.
_UNREADABLE_WITNESS: Final = "<unreadable>"

_BINDING_SEPARATOR: Final = "\x1f"


class RecoveryHoldReason(StrEnum):
    """Why this boot cannot prove it is not following a restore."""

    MARKER_UNREADABLE = "marker_unreadable"
    STATE_DIRECTORY_LOST = "state_directory_lost"
    DATABASE_REPLACED = "database_replaced"
    INSTALLATION_MISMATCH = "installation_mismatch"
    RESTORE_PENDING = "restore_pending"


@dataclass(frozen=True, slots=True)
class RecoveryHold:
    """One observation that this boot follows a restore, and its exact identity."""

    reason: RecoveryHoldReason
    marker_installation_id: str
    recorded_installation_id: str
    witness_token: str
    detail: str

    @property
    def binding(self) -> str:
        """The digest an acknowledgement covers.

        Every identity field is in it, the witness token included: a second
        restore mints a new token, so the acknowledgement written for the first
        one stops matching and the operator is asked again.
        """

        material = _BINDING_SEPARATOR.join(
            (
                self.reason.value,
                self.marker_installation_id,
                self.recorded_installation_id,
                self.witness_token,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def read_restore_pending_token(path: Path) -> str | None:
    """The restore witness beside the state files, ``None`` when there is none.

    Presence is the signal, so every failure to read a file that *is* there
    answers with the stand-in token rather than with absence.
    """

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return _UNREADABLE_WITNESS
    if not isinstance(payload, dict):
        return _UNREADABLE_WITNESS
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        return _UNREADABLE_WITNESS
    return token


def evaluate_recovery_hold(
    *,
    marker_installation_id: str | None,
    marker_unreadable: bool,
    recorded_installation_id: str | None,
    restore_pending_token: str | None,
) -> RecoveryHold | None:
    """Decide the module docstring's table over already-resolved inputs.

    Deliberately pure and I/O-free: every row is then a test that cannot pass by
    accident of file-system ordering, and the writes that legitimately seed a
    first witness live in :func:`resolve_installation` where they can be read
    against the rows they must not manufacture.
    """

    marker = marker_installation_id or ""
    recorded = recorded_installation_id or ""
    witness = restore_pending_token or ""
    if marker_unreadable:
        return RecoveryHold(
            reason=RecoveryHoldReason.MARKER_UNREADABLE,
            marker_installation_id=marker,
            recorded_installation_id=recorded,
            witness_token=witness,
            detail=(
                "the state-generation marker is present but unreadable, so it cannot "
                "show which installation this state directory belongs to"
            ),
        )
    if marker_installation_id is None and recorded_installation_id is not None:
        return RecoveryHold(
            reason=RecoveryHoldReason.STATE_DIRECTORY_LOST,
            marker_installation_id=marker,
            recorded_installation_id=recorded,
            witness_token=witness,
            detail=(
                "this database recorded an installation whose state directory is now "
                "gone; the live-safety files it described cannot be trusted"
            ),
        )
    if marker_installation_id is not None and recorded_installation_id is None:
        return RecoveryHold(
            reason=RecoveryHoldReason.DATABASE_REPLACED,
            marker_installation_id=marker,
            recorded_installation_id=recorded,
            witness_token=witness,
            detail=(
                "the state directory names an installation this database has never "
                "witnessed; the database was replaced beneath surviving state files"
            ),
        )
    if marker_installation_id != recorded_installation_id:
        return RecoveryHold(
            reason=RecoveryHoldReason.INSTALLATION_MISMATCH,
            marker_installation_id=marker,
            recorded_installation_id=recorded,
            witness_token=witness,
            detail=(
                "the state directory and the database name different installations; "
                "they were restored from different snapshots"
            ),
        )
    if restore_pending_token is not None:
        return RecoveryHold(
            reason=RecoveryHoldReason.RESTORE_PENDING,
            marker_installation_id=marker,
            recorded_installation_id=recorded,
            witness_token=witness,
            detail=(
                "a restore witness is present beside the state files; both stores "
                "agree because both were restored together"
            ),
        )
    return None


def resolve_installation(
    session: Session, marker: StateGenerationMarker, *, now: datetime
) -> tuple[str | None, str | None, bool]:
    """Report both witnesses, seeding a first one where that is legitimate.

    Returns ``(marker id, recorded id, marker unreadable)``.

    Two cases write, and both are a first witness rather than a repair:

    * **a fresh install** -- no identity row and no marker: mint an id and seed
      both, so the very next boot has something to disagree with;
    * **a database that predates this witness** -- migration 0012 leaves an
      adoption sentinel (a row whose id is ``NULL``), and a database carrying it
      adopts whatever marker is beside it.

    Neither can manufacture agreement for a directory that was actually lost. A
    row bearing a real id is never rewritten, a present marker is never reseeded,
    and -- the case that separates the two writes -- a database with *no row at
    all* has never run this code, because ``create_all`` leaves the table empty
    and only the migration leaves the sentinel. A marker beside such a database
    therefore belongs to an installation it never witnessed, and that is the
    replaced-database row of the table, not an adoption.
    """

    row = session.get(InstallationIdentityRow, 1)
    recorded = row.installation_id if row is not None else None
    try:
        generation = marker.read()
    except CorruptStateGeneration:
        return None, recorded, True
    marker_id = generation.installation_id if generation is not None else None
    if recorded:
        return marker_id, recorded, False
    if row is None and marker_id is not None:
        return marker_id, None, False
    installation_id = marker_id or uuid4().hex
    if marker_id is None:
        installation_id = marker.ensure_installation(installation_id, now=now)
    if row is None:
        session.add(
            InstallationIdentityRow(id=1, installation_id=installation_id, first_seen_at=now)
        )
    else:
        row.installation_id = installation_id
        row.first_seen_at = now
    return installation_id, installation_id, False


def acknowledgement_exists(session: Session, hold: RecoveryHold) -> bool:
    """Has an operator already typed a note for this exact observation?"""

    found = session.scalar(
        select(RecoveryAcknowledgementRow.id).where(
            RecoveryAcknowledgementRow.binding == hold.binding
        )
    )
    return found is not None


def record_acknowledgement(
    session: Session,
    hold: RecoveryHold,
    *,
    note: str,
    acknowledged_by: str,
    now: datetime,
) -> None:
    """Record the operator's typed act. Re-acknowledging is a no-op, not an error."""

    detail = note.strip()
    if not detail:
        raise ValueError("acknowledging a recovery hold requires a non-empty operator note")
    if acknowledgement_exists(session, hold):
        return
    session.add(
        RecoveryAcknowledgementRow(
            binding=hold.binding,
            reason=hold.reason.value,
            marker_installation_id=hold.marker_installation_id,
            recorded_installation_id=hold.recorded_installation_id,
            witness_token=hold.witness_token,
            note=detail,
            acknowledged_by=acknowledged_by,
            acknowledged_at=now,
        )
    )


def evaluate_startup_recovery_hold(
    sessions: sessionmaker[Session],
    marker: StateGenerationMarker,
    *,
    state_directory: Path,
    now: datetime,
) -> RecoveryHold | None:
    """The whole startup question, for the writer only.

    Only the writer evaluates, for two reasons that point the same way: a
    read-only backend must not write the first witness, and it already satisfies
    everything a hold enforces -- it refuses every mutating route, it never
    publishes readiness, and it builds no autonomy runtime.
    """

    token = read_restore_pending_token(state_directory / RECOVERY_PENDING_NAME)
    with sessions.begin() as session:
        marker_id, recorded_id, unreadable = resolve_installation(session, marker, now=now)
        hold = evaluate_recovery_hold(
            marker_installation_id=marker_id,
            marker_unreadable=unreadable,
            recorded_installation_id=recorded_id,
            restore_pending_token=token,
        )
        if hold is None or acknowledgement_exists(session, hold):
            return None
        return hold
