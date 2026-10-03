"""The durable queue between receiving a proposal and judging one (M7).

M6 gave an external worker a way to reach Chronos and deliberately stopped
short of running a cycle in the request. This is why: if an HTTP call ran the
cycle, the *caller's* schedule would drive broker interaction, and a worker
that submitted a thousand proposals a second would drive a thousand cycles a
second. The activity limits would eventually refuse, but the rate would have
been set by the caller rather than by us — and a limit catching a problem the
design created is not the same as a design that does not create it.

So the route **enqueues** and the runtime **dequeues on its own tick**. The
queue is where those two clocks meet.

## Three properties that matter

- **Raw bytes are stored, never a re-serialized object.** The ingress is the
  single parsing authority. Storing a parsed-then-re-encoded proposal would
  create a second representation that could drift from what was actually sent,
  and the thing that gets judged must be the thing that arrived.
- **The queue is bounded.** A worker that submits faster than the runtime
  drains would otherwise fill the disk, which is a denial-of-service against
  the process that holds the broker connection. Past the cap, enqueueing
  refuses — the newest proposal is dropped rather than the oldest, because
  dropping the oldest would let a flood erase a legitimate earlier proposal.
- **Queued is not authorized.** A row here means *received*. Registered rows
  retain the exact credential epoch and registry-entry digest that arrived;
  everything that decides whether they remain current or may become an order
  happens later, in gates the enqueuing caller cannot reach.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from chronos.persistence.schema import AutonomyProposalQueueRow
from chronos.supervisor import alerts
from chronos.utils.time import utc_now

#: How many unprocessed proposals may be held before enqueueing refuses. Sized
#: so a burst survives a slow tick while a runaway worker cannot fill a disk.
MAX_PENDING = 500

STATUS_PENDING = "PENDING"
#: Claimed by a drain and committed BEFORE it is evaluated (Kevin's K-a). A crash or
#: raise after that commit leaves the row CLAIMED: it is never selected again, never
#: moved back to PENDING, and never resolved automatically (option (c)). Interrupted
#: claims are listed (:func:`list_interrupted_claims`) and alerted, nothing more.
STATUS_CLAIMED = "CLAIMED"
STATUS_PROCESSED = "PROCESSED"

#: Written into ``cycle_stage`` while a row is CLAIMED, so a claim can be attributed to
#: the process that made it. One value per process; ``mark_processed`` overwrites it.
CLAIM_TOKEN = "claim:" + secrets.token_hex(8)

INTERRUPTED_CLAIMS_ALERT_KIND = "proposals.interrupted_claims"
#: Static on purpose: folding never refreshes an alert's detail, so a count, ids or
#: tokens would go stale. The current census is :func:`list_interrupted_claims`.
INTERRUPTED_CLAIMS_ALERT_SUMMARY = (
    "one or more pre-existing proposal claims require inspection; "
    "no automatic resolution is permitted"
)


class ClaimStateError(RuntimeError):
    """A proposal's queue state was not the one the operation requires."""


@dataclass(frozen=True, slots=True)
class QueuedProposal:
    """One received payload, awaiting or having had a cycle."""

    id: int
    payload: str
    received_at: datetime
    #: The registration the route's credential check matched (ADR-0023), or
    #: ``None`` for a row accepted under the pre-registry posture. Written from
    #: the verified match, never from the payload.
    proposer_id: str | None = None
    #: Immutable credential/registration facts captured with ``proposer_id``.
    #: NULL marks a legacy or pre-registry row and is never inferred later.
    proposer_credential_epoch: str | None = None
    proposer_registry_entry_digest: str | None = None


@dataclass(frozen=True, slots=True)
class InterruptedClaim:
    """One CLAIMED row, as found: never a judgement about whether its claimer is alive.

    ``received_at`` is the queue receipt time, not the claim time: no claim time is
    stored, so nothing here can say how long a row has been claimed or whether the
    claim is stale. ``claim_token`` names the process that claimed it.
    """

    id: int
    claim_token: str
    received_at: datetime


@dataclass(frozen=True, slots=True)
class EnqueueOutcome:
    """Whether a proposal was accepted into the queue."""

    queued: bool
    queue_id: int = 0
    refusal: str = ""
    pending_depth: int = 0


def pending_depth(session: Session, *, account_fingerprint: str) -> int:
    """How many proposals are waiting. A depth that only grows means the tick is stuck."""

    total = session.scalar(
        select(func.count())
        .select_from(AutonomyProposalQueueRow)
        .where(
            AutonomyProposalQueueRow.account_fingerprint == account_fingerprint,
            AutonomyProposalQueueRow.status == STATUS_PENDING,
        )
    )
    return int(total or 0)


def outstanding_depth(session: Session, *, account_fingerprint: str) -> int:
    """PENDING plus CLAIMED: everything not yet terminal. This is what the cap bounds."""

    total = session.scalar(
        select(func.count())
        .select_from(AutonomyProposalQueueRow)
        .where(
            AutonomyProposalQueueRow.account_fingerprint == account_fingerprint,
            AutonomyProposalQueueRow.status.in_((STATUS_PENDING, STATUS_CLAIMED)),
        )
    )
    return int(total or 0)


def enqueue(
    session: Session,
    *,
    account_fingerprint: str,
    payload: str,
    now: datetime,
    proposer_id: str | None = None,
    proposer_credential_epoch: str | None = None,
    proposer_registry_entry_digest: str | None = None,
) -> EnqueueOutcome:
    """Accept a proposal for later judging, or refuse because the queue is full.

    Refuses rather than raising: a full queue is a load condition the caller
    should be told about and can retry, not a programming error. The caller
    gets the depth back so a worker can back off on its own rather than
    hammering a queue it cannot see.
    """

    binding = (proposer_credential_epoch, proposer_registry_entry_digest)
    if proposer_id is None and binding != (None, None):
        raise ValueError("an unauthenticated proposal cannot carry a registration binding")
    if proposer_id is not None:
        for label, value in zip(
            ("credential epoch", "registry entry digest"), binding, strict=True
        ):
            if (
                value is None
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(
                    f"a registered proposal's {label} must be a 64-character lowercase "
                    "SHA-256 digest"
                )

    depth = pending_depth(session, account_fingerprint=account_fingerprint)
    outstanding = outstanding_depth(session, account_fingerprint=account_fingerprint)
    if outstanding >= MAX_PENDING:
        return EnqueueOutcome(
            queued=False,
            refusal=(
                f"the proposal queue holds {outstanding} unprocessed items, at its "
                f"{MAX_PENDING} cap; the runtime is not draining and new proposals are "
                "refused rather than displacing earlier ones"
            ),
            pending_depth=depth,
        )
    row = AutonomyProposalQueueRow(
        account_fingerprint=account_fingerprint,
        payload=payload,
        received_at=now,
        status=STATUS_PENDING,
        cycle_stage="",
        refusal="",
        proposer_id=proposer_id,
        proposer_credential_epoch=proposer_credential_epoch,
        proposer_registry_entry_digest=proposer_registry_entry_digest,
    )
    session.add(row)
    session.flush()
    return EnqueueOutcome(queued=True, queue_id=row.id, pending_depth=depth + 1)


def claim_batch(
    session: Session, *, account_fingerprint: str, limit: int
) -> tuple[QueuedProposal, ...]:
    """Claim the oldest pending proposals, in arrival order, and return them.

    Each claim is a compare-and-set: ``UPDATE ... SET status='CLAIMED' WHERE id=:id
    AND status='PENDING'``, so exactly one claimant wins a row even when two drains
    race. The caller's transaction commits the claims; the runtime commits it before
    any proposal is evaluated, so a crash after that commit leaves the row CLAIMED
    and it is never re-presented.

    Claims that already existed before this call are interrupted (this process's
    ticks are sequential, so its own claims are always finished by the next call;
    another process's live claims may also be counted, conservatively). They raise
    one static alert and are left exactly as they are.

    Bounded per tick so one flood cannot monopolise a cycle: the runtime should
    also deliver alerts and observe its own health each pass, and a tick that
    spent all its time draining a queue would starve exactly the work that
    reports the queue is flooded.
    """

    preexisting = session.scalar(
        select(func.count())
        .select_from(AutonomyProposalQueueRow)
        .where(
            AutonomyProposalQueueRow.account_fingerprint == account_fingerprint,
            AutonomyProposalQueueRow.status == STATUS_CLAIMED,
        )
    )
    if preexisting:
        alerts.raise_alert(
            session,
            account_fingerprint=account_fingerprint,
            severity=alerts.AlertSeverity.WARNING,
            kind=INTERRUPTED_CLAIMS_ALERT_KIND,
            summary=INTERRUPTED_CLAIMS_ALERT_SUMMARY,
            detail={},
            now=utc_now(),
        )
    candidates = session.scalars(
        select(AutonomyProposalQueueRow)
        .where(
            AutonomyProposalQueueRow.account_fingerprint == account_fingerprint,
            AutonomyProposalQueueRow.status == STATUS_PENDING,
        )
        .order_by(AutonomyProposalQueueRow.id.asc())
        .limit(limit)
    ).all()
    claimed: list[QueuedProposal] = []
    for row in candidates:
        result = session.execute(
            update(AutonomyProposalQueueRow)
            .where(
                AutonomyProposalQueueRow.id == row.id,
                AutonomyProposalQueueRow.status == STATUS_PENDING,
            )
            .values(status=STATUS_CLAIMED, cycle_stage=CLAIM_TOKEN)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            continue  # another drain claimed it first
        claimed.append(
            QueuedProposal(
                id=row.id,
                payload=row.payload,
                received_at=row.received_at,
                proposer_id=row.proposer_id,
                proposer_credential_epoch=row.proposer_credential_epoch,
                proposer_registry_entry_digest=row.proposer_registry_entry_digest,
            )
        )
    return tuple(claimed)


def list_interrupted_claims(
    session: Session, *, account_fingerprint: str
) -> tuple[InterruptedClaim, ...]:
    """Every CLAIMED row for the account, oldest first. Read-only; the current census.

    Nothing in production calls this today: it is the list a later, separately
    decided operator surface will show. It writes nothing and resolves nothing.
    """

    rows = session.scalars(
        select(AutonomyProposalQueueRow)
        .where(
            AutonomyProposalQueueRow.account_fingerprint == account_fingerprint,
            AutonomyProposalQueueRow.status == STATUS_CLAIMED,
        )
        .order_by(AutonomyProposalQueueRow.id.asc())
    )
    return tuple(
        InterruptedClaim(id=row.id, claim_token=row.cycle_stage, received_at=row.received_at)
        for row in rows
    )


def mark_processed(
    session: Session,
    *,
    queue_id: int,
    stage: str,
    refusal: str,
    now: datetime,
) -> None:
    """Record that a cycle judged this proposal, whatever the outcome.

    Marked *processed* even when the cycle refused, because "judged and
    refused" is a finished state. Leaving refusals pending would make the
    runtime re-judge them forever, which is both a loop and a way for one
    malformed payload to block every proposal behind it.

    Only a CLAIMED row may be marked: the update is conditional on that status, and
    any other state refuses loudly rather than overwriting a row it did not claim.
    """

    result = session.execute(
        update(AutonomyProposalQueueRow)
        .where(
            AutonomyProposalQueueRow.id == queue_id,
            AutonomyProposalQueueRow.status == STATUS_CLAIMED,
        )
        .values(
            status=STATUS_PROCESSED,
            processed_at=now,
            cycle_stage=stage[:32],
            refusal=refusal[:64],
        )
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise ClaimStateError(
            f"proposal {queue_id} is not CLAIMED; refusing to mark a row this drain did not claim"
        )
