"""The per-job evidence record: issue, resolve, retain (ADR-0028 Option C).

ADR-0023 made authorship real and said, in its own acceptance note, exactly what
it left undone: ``ProposerRegistration`` carries no evidence fields, so every
registered identity stamped the placeholder bundle id and an honestly-absent
digest. ADR-0028 found the sharper consequence. Admission check 9 compared
``provenance.evidence_bundle_id``/``_digest`` against
``SupervisorState.expected_*`` — and **both sides were two reads of the same
``INGRESS_IDENTITY`` constant**. The check was written correctly (exact match,
``None`` included, deny-by-default when the expectation is absent) and wired to a
comparison that had never had two independent sides. It could not refuse, in any
posture, for any proposer. That is the R-24..R-27 shape one level up: not a
control that failed, a control whose evidence was never gathered.

This module is the record that gives the comparison a side.

## The two kinds, and why the label is load-bearing

- ``backend_served`` — the backend composed a canonical document, took SHA-256
  over the **exact bytes it served**, and returned them. The backend is a
  *witness*: "unissued", "issued to another proposer" and "expired" become facts
  it can check rather than claims it must accept.
- ``alert_attested`` — a proposer asserted, under its own credential and at a
  recorded time, that it saw bytes with this digest. The backend cannot
  recompute it and does not claim to. It is the only shape available to the
  TradingView bridge, whose evidence is the alert itself: authored outside
  Chronos, delivered to a process that imports nothing from ``chronos``, and
  never seen by the backend at all.

**Attested is not witnessed.** The record binds a claim to a credential and a
time. That is non-repudiation, not verification, and ADR-0028's recommended rule
for the ladder is blunt: an attested bundle may back a proposal; it may not back
a promotion rung. The kinds never substitute for one another at any comparison
(:mod:`chronos.supervisor.evidence_kinds`, which holds that rule alone so the
pure admission kernel can apply it without importing a database), because a
source whose evidence originates outside Chronos cannot produce evidence
Chronos witnessed.

## Where each half is judged, and why they are split

- **Authority, at STAMP.** :func:`resolve` runs at the drain, exactly where and
  how the drain already re-resolves the proposer registration. No record, a
  record belonging to another proposer, or one expired against the drain's
  ``now`` refuses before the proposal is ever judged, and provenance is stamped
  from the **record**.
- **Agreement, at admission check 9.** The payload's own citation faces the
  backend's record inside the pure kernel, where every refusal is reproducible
  from its inputs. That is the half this module does not own; see
  :func:`chronos.supervisor.admission._check_evidence_bundle`.

Splitting them is the whole point of Option C. If the stamper stamped from the
record *and* the expectation came from the same record with nothing else
compared, check 9 would stay a tautology — a per-job one instead of a global one.

## Equality catches accident, not malice

Stated here because it is the honest description of the central rule and should
be repeated wherever this feature is described. A hostile proposer can fetch a
bundle, reason on entirely different text, and cite the issued digest; nothing
here detects that, because the backend cannot observe a prompt in another
process. What equality does catch is the realistic failure — an honest proposer
whose rendering drifts from what it fetched (truncation, reordering, a key-order
change, a partial fetch) — which is exactly the class that produced R-24..R-27.

## Bounds carried by the writes themselves

Issuance is a **write reachable by a proposal-only credential**, so it comes with
two bounds in the shape ``proposals.MAX_PENDING`` already uses: a per-proposer
cap on live bundles (a proposer that could mint unbounded rows is a disk-filling
denial of service against the process holding the broker connection), and a
retention rule for expired rows. Pruning deletes the *row*, never the hash-chain
record that describes its issuance — so the audit trail of what was issued
survives the expiry of the thing issued.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from chronos.persistence import hash_chain
from chronos.persistence.schema import AutonomyEvidenceBundleRow
from chronos.supervisor.evidence_kinds import BundleKind

__all__ = [
    "BUNDLE_VERSION",
    "MAX_LIVE_BUNDLES_PER_PROPOSER",
    "RETENTION_AFTER_EXPIRY",
    "BundleKind",
    "IssuanceRefused",
    "IssuedBundle",
    "Resolution",
    "ResolutionRefusal",
    "hash_chain_stream",
    "issue",
    "live_bundle_count",
    "load",
    "new_bundle_id",
    "prune_expired",
    "resolve",
]

#: Hash-chain stream carrying every evidence bundle issued or attested. Named
#: per account like every other supervisor stream, so one account's history
#: cannot be invalidated by another's.
EVIDENCE_STREAM = "autonomy.evidence"

#: The bundle serialization this build produces and understands. ADR-0028 warns
#: that requiring equality couples the backend's serialization to the proposer's
#: rendering: a change to either breaks every forward until both move. That is
#: the fail-closed direction, and this pin is what makes the break visible and
#: attributable rather than a silent digest disagreement.
BUNDLE_VERSION = "1"

#: How many unexpired bundles one proposer may hold at once, per account. Sized
#: like ``proposals.MAX_PENDING``: a burst of honest re-issues survives, and a
#: runaway proposer cannot fill the disk of the process holding the broker
#: connection. Past the cap, issuance refuses rather than evicting an earlier
#: bundle — evicting would let a flood invalidate a legitimate in-flight job.
MAX_LIVE_BUNDLES_PER_PROPOSER = 64

#: How long an expired row is kept before pruning. Long enough that an operator
#: reading a refusal can still find the record that caused it; short enough that
#: the table does not grow without bound. The hash-chain record of the issuance
#: is NOT pruned, so pruning never destroys the audit trail — only the lookup
#: row whose authority has already lapsed.
RETENTION_AFTER_EXPIRY = timedelta(days=7)


class ResolutionRefusal(StrEnum):
    """Why the drain could not bind a proposal to an issued bundle.

    Distinct codes because "forged", "stolen", "stale" and "absent" are four
    different owner-facing problems, and a journal that renders them as one
    refusal cannot tell an expired credential from an attack.
    """

    #: The proposal cites no evidence at all.
    UNCITED = "EVIDENCE_BUNDLE_UNCITED"
    #: No record exists for any bundle id the proposal cites.
    UNISSUED = "EVIDENCE_BUNDLE_UNISSUED"
    #: A record exists and was issued to a different registered proposer.
    FOREIGN = "EVIDENCE_BUNDLE_FOREIGN"
    #: A record exists and has expired against the drain's clock.
    EXPIRED = "EVIDENCE_BUNDLE_EXPIRED"


@dataclass(frozen=True, slots=True)
class IssuedBundle:
    """One issued record, as the issuing caller and the drain both see it."""

    bundle_id: str
    proposer_id: str
    kind: BundleKind
    digest: str
    bundle_version: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Resolution:
    """The drain's answer: a bound record, or exactly why not.

    Never both. A caller that received a refusal has nothing to stamp, which is
    the fail-closed direction — misattributing evidence in a hash-chained
    journal would be worse than a recorded refusal.
    """

    bundle: IssuedBundle | None = None
    refusal: ResolutionRefusal | None = None
    detail: str = ""


class IssuanceRefused(RuntimeError):
    """Issuance refused. Raised rather than returned because the caller is a
    route that must answer the proposer with a status code, and a silently
    unissued bundle would be a proposal that refuses later for no visible reason.
    """


def new_bundle_id() -> str:
    """A backend-chosen bundle id.

    Not security-bearing: authority comes from the durable record and the
    credential it names, never from the id being hard to guess. Random anyway,
    because a predictable id invites a proposer to cite one it has not been
    issued and makes the resulting refusals harder to read.
    """

    return f"evb_{secrets.token_hex(16)}"


def live_bundle_count(
    session: Session, *, account_fingerprint: str, proposer_id: str, now: datetime
) -> int:
    """How many unexpired bundles this proposer currently holds."""

    total = session.scalar(
        select(func.count())
        .select_from(AutonomyEvidenceBundleRow)
        .where(
            AutonomyEvidenceBundleRow.account_fingerprint == account_fingerprint,
            AutonomyEvidenceBundleRow.proposer_id == proposer_id,
            AutonomyEvidenceBundleRow.expires_at > now,
        )
    )
    return int(total or 0)


def issue(
    session: Session,
    *,
    account_fingerprint: str,
    proposer_id: str,
    kind: BundleKind,
    digest: str,
    now: datetime,
    ttl_seconds: float,
    bundle_version: str = BUNDLE_VERSION,
) -> IssuedBundle:
    """Record one bundle against the credential that asked for it.

    Written in the caller's transaction, like every other supervisor durable
    write: a record that committed separately from the hash-chain entry
    describing it could outlive a rolled-back issuance, or be lost while the
    issuance survived.

    Refuses — rather than trimming, evicting, or silently succeeding — when the
    proposer is at its cap. A cap that quietly made room would not be a cap.
    """

    normalized = digest.strip().lower()
    if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
        raise IssuanceRefused(
            "an evidence digest must be the 64-character lowercase hex SHA-256 of the "
            "exact bytes; anything else is not a digest this protocol can compare"
        )
    if ttl_seconds <= 0:
        raise IssuanceRefused(
            "an evidence bundle must have a positive time to live; a bundle that expires "
            "at issue would refuse every proposal it backs"
        )
    if not proposer_id.strip():
        raise IssuanceRefused(
            "a bundle is issued TO a credential; with no registered proposer there is no "
            "author to issue to, and an unattributed bundle is the constant this protocol "
            "exists to remove"
        )

    live = live_bundle_count(
        session,
        account_fingerprint=account_fingerprint,
        proposer_id=proposer_id,
        now=now,
    )
    if live >= MAX_LIVE_BUNDLES_PER_PROPOSER:
        raise IssuanceRefused(
            f"proposer {proposer_id} holds {live} unexpired evidence bundles, at its "
            f"{MAX_LIVE_BUNDLES_PER_PROPOSER} cap; issuance refuses rather than displacing "
            "an in-flight bundle"
        )

    bundle_id = new_bundle_id()
    expires_at = now + timedelta(seconds=ttl_seconds)
    row = AutonomyEvidenceBundleRow(
        account_fingerprint=account_fingerprint,
        bundle_id=bundle_id,
        proposer_id=proposer_id,
        kind=kind.value,
        digest=normalized,
        bundle_version=bundle_version,
        issued_at=now,
        expires_at=expires_at,
    )
    session.add(row)
    session.flush()
    hash_chain.append(
        session,
        stream=hash_chain_stream(account_fingerprint),
        kind="evidence_bundle_issued",
        payload={
            "bundle_id": bundle_id,
            "proposer_id": proposer_id,
            # The kind travels in the chain because "issued" and "attested" are
            # different claims, and a journal that recorded only "evidence" would
            # let an attested record later read as one the backend witnessed.
            "bundle_kind": kind.value,
            "digest": normalized,
            "bundle_version": bundle_version,
            "expires_at": expires_at.isoformat(),
        },
        recorded_at=now,
    )
    return IssuedBundle(
        bundle_id=bundle_id,
        proposer_id=proposer_id,
        kind=kind,
        digest=normalized,
        bundle_version=bundle_version,
        issued_at=now,
        expires_at=expires_at,
    )


def load(
    session: Session, *, account_fingerprint: str, bundle_id: str
) -> AutonomyEvidenceBundleRow | None:
    """The record for one bundle id, regardless of who holds it.

    Deliberately **not** filtered by proposer: a bundle issued to someone else
    must resolve so it can be refused as *foreign*. Filtering here would render
    a stolen bundle indistinguishable from one that was never issued, and those
    are different owner-facing events.
    """

    return session.scalar(
        select(AutonomyEvidenceBundleRow).where(
            AutonomyEvidenceBundleRow.account_fingerprint == account_fingerprint,
            AutonomyEvidenceBundleRow.bundle_id == bundle_id,
        )
    )


def resolve(
    session: Session,
    *,
    account_fingerprint: str,
    cited_ids: tuple[str, ...],
    proposer_id: str,
    now: datetime,
) -> Resolution:
    """Bind a proposal's citations to an issued record — the authority half.

    ``cited_ids`` is the ``evidence_id`` of every citation the proposal carries,
    in payload order. The first that names a record is the cited bundle; the
    rest are ordinary citations this protocol does not govern. Resolving by id
    alone and *then* checking ownership is what keeps "issued to another
    proposer" distinguishable from "never issued".

    Expiry is judged against ``now`` — the **drain's** clock, the same one that
    judges registration currency — so a bundle that expired between enqueue and
    drain refuses at the moment authority is exercised rather than the moment
    bytes arrived. The proposer's own ``as_of`` is data in the record, never the
    judge.
    """

    if not cited_ids:
        return Resolution(
            refusal=ResolutionRefusal.UNCITED,
            detail=(
                "the proposal carries no evidence citation, so there is nothing to bind it "
                "to; under the configured posture a proposal must cite the bundle it read"
            ),
        )
    for cited in cited_ids:
        row = load(session, account_fingerprint=account_fingerprint, bundle_id=cited)
        if row is None:
            continue
        if row.proposer_id != proposer_id:
            return Resolution(
                refusal=ResolutionRefusal.FOREIGN,
                detail=(
                    "the cited evidence bundle was issued to a different registered "
                    "proposer; a bundle is issued to a credential and is not transferable"
                ),
            )
        if now >= row.expires_at:
            return Resolution(
                refusal=ResolutionRefusal.EXPIRED,
                detail=(
                    f"the cited evidence bundle expired at {row.expires_at.isoformat()} and "
                    "the drain's clock is past it; re-read evidence and propose again"
                ),
            )
        return Resolution(
            bundle=IssuedBundle(
                bundle_id=row.bundle_id,
                proposer_id=row.proposer_id,
                kind=BundleKind(row.kind),
                digest=row.digest,
                bundle_version=row.bundle_version,
                issued_at=row.issued_at,
                expires_at=row.expires_at,
            )
        )
    return Resolution(
        refusal=ResolutionRefusal.UNISSUED,
        detail=(
            "no evidence bundle the proposal cites was ever issued for this account; a "
            "proposer cannot mint its own evidence record"
        ),
    )


def prune_expired(session: Session, *, account_fingerprint: str, now: datetime) -> int:
    """Delete rows whose authority lapsed longer ago than the retention horizon.

    Returns how many rows went. The hash-chain records describing their issuance
    are **not** touched: what was issued, to whom, and when stays permanently
    legible, and only the lookup row — which can no longer authorize anything —
    is reclaimed. An audit trail that forgot an issuance could not answer the
    first question an incident review asks.
    """

    cutoff = now - RETENTION_AFTER_EXPIRY
    stale = list(
        session.scalars(
            select(AutonomyEvidenceBundleRow).where(
                AutonomyEvidenceBundleRow.account_fingerprint == account_fingerprint,
                AutonomyEvidenceBundleRow.expires_at <= cutoff,
            )
        )
    )
    for row in stale:
        session.delete(row)
    return len(stale)


def hash_chain_stream(account_fingerprint: str) -> str:
    """Per-account stream name. Fingerprint only — never a raw account id."""

    return f"{EVIDENCE_STREAM}:{account_fingerprint}"
