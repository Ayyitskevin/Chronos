"""Killing a proposer credential without a restart (A3).

The proposer registry is a **boot-time snapshot**: the route and the drain each
read ``AUTONOMY_PROPOSERS_FILE`` once at startup, the mandate file's precedent.
That is right for a grant — the owner's authored document should not change
under a running process — and wrong for exactly one event, which is the event
this module exists for: a credential has leaked and must stop working *now*.

R-48 residual (c) disclosed the gap in its own words: disabling or deleting a
registration lands at the next restart. The live stand-downs were the kill
switch, mandate revocation, and bouncing the process that holds the broker
connection. None of those is "stop this one proposer".

## The shape, borrowed from mandate revocation

A durable act the running process honors. ``revoke`` writes one row and appends
one hash-chained record; the route and the drain-time resolver both consult the
table on every use. Nothing is cached, because a cache is the thing that made
the file a snapshot in the first place.

## Keyed by the credential, not by the name

The row records ``proposer_id`` so the audit trail is legible, but the check is
on ``secret_sha256``. What leaks is a credential, not a name, and this is the
difference between two futures after an incident:

- keyed by id: the proposer's name is burned forever, and the owner must invent
  ``claude-worker-2`` to keep operating;
- keyed by hash: the owner mints a **new** credential for the same proposer and
  it works at the next restart, while the leaked one is dead permanently.

The second is what an operator actually needs, and it is strictly narrower: it
revokes precisely the secret that escaped.

## Not account-scoped

Everything else durable here is keyed by account fingerprint. This is not,
deliberately: a credential is global to the registry document, so an
account-scoped revocation would leave a state in which a revoked credential
still proposes somewhere. The hash-chain stream is likewise un-scoped.

## What revocation is not

It is not a way to *grant*. There is no un-revoke: re-granting is a new
credential in the owner's file plus a restart, the same rule ADR-0017 applies to
a revoked mandate ("a restart is not permission to undo the owner standing the
system down"). It is also not a deletion — the row stays, because an audit trail
that forgets a revocation cannot answer when authority ended.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from chronos.persistence import hash_chain
from chronos.persistence.schema import AutonomyProposerRevocationRow

#: Hash-chain stream for revocations. Un-scoped by account, like the act itself.
PROPOSER_STREAM = "autonomy.proposers"


@dataclass(frozen=True, slots=True)
class Revocation:
    """One credential the owner killed, and when."""

    proposer_id: str
    secret_sha256: str
    reason: str
    revoked_at: datetime


def revoke(
    session: Session,
    *,
    proposer_id: str,
    secret_sha256: str,
    reason: str,
    now: datetime,
) -> bool:
    """Kill one credential. Returns False when it was already revoked.

    Idempotent on purpose: an operator who runs this twice during an incident —
    or who cannot remember whether the first invocation landed — must not be
    told something went wrong, and must not produce a second chain record for
    one act. The first call is the act; the second is a no-op that says so.

    A reason is required for the same purpose an acknowledgement note is: the
    record has to be reviewable later by someone who was not there.
    """

    if not reason.strip():
        raise ValueError(
            "revoking a proposer requires a reason; a credential killed for no stated "
            "cause cannot be reviewed afterwards"
        )
    existing = session.scalar(
        select(AutonomyProposerRevocationRow).where(
            AutonomyProposerRevocationRow.secret_sha256 == secret_sha256
        )
    )
    if existing is not None:
        return False
    session.add(
        AutonomyProposerRevocationRow(
            proposer_id=proposer_id,
            secret_sha256=secret_sha256,
            reason=reason,
            revoked_at=now,
        )
    )
    session.flush()
    hash_chain.append(
        session,
        stream=PROPOSER_STREAM,
        kind="proposer_revoked",
        payload={
            "proposer_id": proposer_id,
            # The hash, never the credential. The registry stores the same
            # value, so nothing here holds anything presentable.
            "secret_sha256": secret_sha256,
            "reason": reason,
        },
        recorded_at=now,
    )
    return True


def is_revoked(session: Session, *, secret_sha256: str) -> bool:
    """Whether this credential has been killed. Read fresh, never cached.

    Consulted on every route authentication and every drain-time resolution. A
    cached answer would reintroduce exactly the staleness that makes the
    registry file a restart-scoped grant, which is the defect this closes.
    """

    return (
        session.scalar(
            select(AutonomyProposerRevocationRow.id).where(
                AutonomyProposerRevocationRow.secret_sha256 == secret_sha256
            )
        )
        is not None
    )


def revoked_credentials(session: Session) -> dict[str, Revocation]:
    """Every revocation, keyed by credential hash. For operator reporting only.

    ``proposer check`` uses this to label entries; nothing on the trade path
    reads it, because a bulk read is a snapshot and the trade path must not hold
    one.
    """

    rows = session.scalars(
        select(AutonomyProposerRevocationRow).order_by(AutonomyProposerRevocationRow.id)
    ).all()
    return {
        row.secret_sha256: Revocation(
            proposer_id=row.proposer_id,
            secret_sha256=row.secret_sha256,
            reason=row.reason,
            revoked_at=row.revoked_at,
        )
        for row in rows
    }
