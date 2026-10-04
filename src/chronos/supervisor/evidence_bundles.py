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

import json
import math
import secrets
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import LargeBinary, Text, case, cast, func, literal, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from chronos.config.limits import (
    MAX_DURABLE_HASH_TEXT_CHARS,
    MAX_DURABLE_KIND_TEXT_CHARS,
    MAX_DURABLE_SEQUENCE_TEXT_CHARS,
    MAX_DURABLE_TIMESTAMP_TEXT_CHARS,
)
from chronos.persistence import hash_chain
from chronos.persistence.schema import AutonomyEvidenceBundleRow, HashChainRow
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

#: Hash-chain event kind for the durable sticky EXPIRED verdict (D-26/FIX-26).
#: Once a drain has refused a bundle as EXPIRED, that verdict is recorded on the
#: account's evidence stream in the same transaction as the refusal, and every
#: later resolution of the live row refuses EXPIRED regardless of what the wall
#: clock does afterwards — refused authority is not revocable by clock motion.
EXPIRED_EVENT_KIND = "evidence_bundle_expired"
#: The exact key set of an expiry record payload (the writer in ``resolve``). Expiry
#: records are recognised by this hashed shape, never by the unhashed ``kind`` (FIX-26-K1).
_EXPIRY_PAYLOAD_KEYS = frozenset({"bundle_id", "expires_at"})


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
    #: A pre-0011 record carries no immutable credential/registration binding.
    REGISTRATION_UNBOUND = "EVIDENCE_BUNDLE_REGISTRATION_UNBOUND"
    #: The record belongs to another credential epoch or registry entry.
    REGISTRATION_REPLACED = "EVIDENCE_BUNDLE_REGISTRATION_REPLACED"
    #: A record exists and has expired against the drain's clock. The verdict is
    #: durable: it is recorded on the account's evidence stream, and a later resolve
    #: of that bundle by the SAME proposer and registration stays EXPIRED across a
    #: clock rewind and a restart (D-26). A different proposer or registration keeps
    #: FOREIGN / REGISTRATION_UNBOUND / REGISTRATION_REPLACED, and once the row is
    #: pruned the bundle resolves UNISSUED. The same code is also returned when the
    #: durable evidence cannot be trusted (a stream that fails verification, or an
    #: undecodable or malformed expiry record); the detail text says which.
    EXPIRED = "EVIDENCE_BUNDLE_EXPIRED"


@dataclass(frozen=True, slots=True)
class IssuedBundle:
    """One issued record, as the issuing caller and the drain both see it."""

    bundle_id: str
    proposer_id: str
    proposer_credential_epoch: str
    proposer_registry_entry_digest: str
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
    proposer_credential_epoch: str,
    proposer_registry_entry_digest: str,
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
    for label, value in (
        ("credential epoch", proposer_credential_epoch),
        ("registry entry digest", proposer_registry_entry_digest),
    ):
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise IssuanceRefused(
                f"the proposer {label} must be a 64-character lowercase SHA-256 digest"
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
        proposer_credential_epoch=proposer_credential_epoch,
        proposer_registry_entry_digest=proposer_registry_entry_digest,
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
            "proposer_credential_epoch": proposer_credential_epoch,
            "proposer_registry_entry_digest": proposer_registry_entry_digest,
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
        proposer_credential_epoch=proposer_credential_epoch,
        proposer_registry_entry_digest=proposer_registry_entry_digest,
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


# ============================================================ FU2: the bounded sticky read
#
# FU2r7 (Daybreak PASS-DELTA, kimi CONFIRMED) under Kevin's A-prime ruling and
# K-20261004-007/-009. Until FU2 the sticky stage verified the WHOLE stream and decoded every
# record on every resolve. Now a verification PASS, run off the drain's path one chunk per
# tick inside ONE SQLite snapshot, publishes a verified head ``(N, H)`` and the expired ids
# it saw; each resolve reads ONE bounded statement from that head, proves that every row
# THIS PROCESS has verified is still present with its digest (``observed``), and answers.
#
# Fail-closed states, each refusing EXPIRED with its own detail (R1: the code is reused, the
# detail names the reason, and pins assert the detail):
#
# - not ready: no pass has completed since this process started (R7);
# - stale: the published state is older than ``max_age_seconds`` (monotonic, R2);
# - in progress: the stream outgrew the per-resolve bound ``resolve_rows`` (R3a);
# - latched: a row this process verified is missing, moved or rewritten; cleared ONLY by a
#   restart (C1.4; the owner-reviewed replacement is DEFERRED with the anchor lane, R12);
# - doubt: corruption detectable from the stream itself (K1's D0-D4 and the bounds); cleared
#   by the next clean pass.
#
# What it does NOT guarantee (C3, stated where the code is): a truncation or consistent
# rewrite of rows this process has never verified, including every row across a restart, is
# undetectable, exactly as K1's documented bound (no external anchor; R10-R12 deferred); an
# actor who can delete rows AND restart the drain; the 12 other hash_chain writers; a row at
# or below N edited after a pass's snapshot is caught only by the next pass (C3 item 5).
#
# Builder-derived values (Kevin K-20261004-009, flagged): max_age_seconds 900, row_bytes
# 4096 (the largest legitimate canonical record measured at 1208 bytes), expired_ids 10 000;
# max_pass_attempts 5 is also builder-derived (R16 named the policy, not a number).

#: The process's monotonic clock, through a seam a test can drive.
_monotonic: Callable[[], float] = time.monotonic

NOT_READY_DETAIL = (
    "the account's durable evidence stream has not been verified since this process started; "
    "evidence-bound proposals refuse until the first verification pass completes"
)
STALE_DETAIL = (
    "the account's verified evidence state is older than its verification bound; refusing "
    "until a fresh verification pass completes"
)
IN_PROGRESS_DETAIL = (
    "the account's evidence stream has grown past the verified state by more than the "
    "per-resolve bound; verification in progress, refusing until a pass catches up"
)
MONOTONIC_DETAIL = (
    "the process's monotonic clock reading is unusable (non-finite, or behind the verified "
    "state's start); refusing closed until a fresh verification pass"
)
LATCHED_DETAIL = (
    "the account's durable evidence stream no longer holds, with its digest, a record this "
    "process verified; every evidence-bound proposal refuses until the process is restarted "
    "and the stream is inspected"
)
SINGLE_CONNECTION_DETAIL = (
    "this database has one shared connection (an in-memory engine), which cannot hold a "
    "verification snapshot across ticks, and the evidence stream needs more than one pass "
    "chunk; refusing closed"
)
_DEADLINE_DETAIL = (
    "the verification deadline is not representable (the monotonic start plus the bound does "
    "not exceed the start); refusing closed"
)
_STICKY_DETAIL = (
    "the cited evidence bundle was already refused EXPIRED at an earlier "
    "drain and that verdict is durable; wall-clock motion does not revive "
    "refused authority"
)
#: Owner-alert kinds the runtime raises for this stage (R14, R16).
STREAM_TRUNCATED_ALERT_KIND = "evidence.stream_truncated"
PASS_FAILED_ALERT_KIND = "evidence.pass_failed"


def _d0(inner: str) -> str:
    return (
        f"the account's durable evidence stream failed verification ({inner}); "
        "refusing closed until the stream is repaired"
    )


@dataclass(frozen=True, slots=True)
class EvidenceVerificationLimits:
    """The bounds of the sticky read and its pass; each is a named setting (Kevin)."""

    #: T_max (R2): seconds a published state stays fresh, from its pass's start.
    max_age_seconds: float = 900.0
    #: B (R3a): suffix rows one resolve may verify past the published head.
    resolve_rows: int = 1000
    #: P (R3b): rows one pass chunk may verify per tick.
    pass_rows: int = 1000
    #: Cr (R3b): bytes of one record's payload, enforced before it reaches Python.
    row_bytes: int = 4096
    #: Ct (R3b): payload bytes one pass chunk may admit per tick.
    pass_bytes_per_tick: int = 1_048_576
    #: K (R3b): expired bundle ids one published state may retain.
    expired_ids: int = 10_000
    #: R16: consecutive aborted passes before the stream refuses until a restart.
    max_pass_attempts: int = 5

    def __post_init__(self) -> None:
        age = self.max_age_seconds
        if (
            isinstance(age, bool)
            or not isinstance(age, (int, float))
            or not math.isfinite(age)
            or age <= 0
        ):
            raise ValueError("max_age_seconds must be a finite number of seconds above zero")
        for name in (
            "resolve_rows",
            "pass_rows",
            "row_bytes",
            "pass_bytes_per_tick",
            "expired_ids",
            "max_pass_attempts",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.row_bytes > self.pass_bytes_per_tick:
            raise ValueError(
                "row_bytes must not exceed pass_bytes_per_tick: one admitted row must fit a tick"
            )

    @classmethod
    def from_settings(cls, settings: Any) -> EvidenceVerificationLimits:
        """The limits a ``Settings`` object names (read by attribute, so the supervisor does
        not import the configuration module)."""

        return cls(
            max_age_seconds=settings.autonomy_evidence_verification_max_age_seconds,
            resolve_rows=settings.autonomy_evidence_resolve_rows,
            pass_rows=settings.autonomy_evidence_pass_rows_per_tick,
            row_bytes=settings.autonomy_evidence_row_bytes,
            pass_bytes_per_tick=settings.autonomy_evidence_pass_bytes_per_tick,
            expired_ids=settings.autonomy_evidence_expired_ids,
            max_pass_attempts=settings.autonomy_evidence_max_pass_attempts,
        )


@dataclass(frozen=True, slots=True)
class PublishedState:
    """A full verification of ONE snapshot of the stream, from genesis to its head."""

    sequence: int
    record_hash: str
    expired_ids: frozenset[str]
    started_at_monotonic: float
    deadline: float


@dataclass(slots=True)
class _PassAttempt:
    session: Session
    cursor: int
    partial_hash: str
    partial_expired: set[str]
    observed_at_start: tuple[int, str] | None
    start_proven: bool
    started_at_monotonic: float


@dataclass(slots=True)
class StreamState:
    """Process memory for one stream (R5(a): never persisted)."""

    published: PublishedState | None = None
    #: The highest (sequence, record_hash) THIS PROCESS has verified: an identity, never a
    #: watermark; replaced only by a tuple whose range re-proved it, never lowered.
    observed: tuple[int, str] | None = None
    latched: str | None = None
    doubt: str | None = None
    needs_pass: bool = True
    attempts: int = 0
    latch_alerted: bool = False
    in_flight: _PassAttempt | None = None

    @property
    def pass_in_flight(self) -> bool:
        return self.in_flight is not None


@dataclass(slots=True)
class _EngineState:
    limits: EvidenceVerificationLimits = field(default_factory=EvidenceVerificationLimits)
    streams: dict[str, StreamState] = field(default_factory=dict)


#: Keyed by engine (weakly), so the state of one database never meets another database in
#: the same process (declared build decision: many tests share an account fingerprint).
_ENGINES: weakref.WeakKeyDictionary[Engine, _EngineState] = weakref.WeakKeyDictionary()


@dataclass(frozen=True, slots=True)
class PassOutcome:
    """What one verification tick (or a whole pass) did."""

    rows_verified: int = 0
    published: bool = False
    in_progress: bool = False
    discarded: bool = False
    latched: bool = False
    aborted: str | None = None


class _Refused(Exception):
    """A row (or the pass) cannot be trusted; ``detail`` is the closed refusal text."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _Latch(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _engine_state(engine: Engine) -> _EngineState:
    state = _ENGINES.get(engine)
    if state is None:
        state = _EngineState()
        _ENGINES[engine] = state
    return state


def configure_verification(engine: Engine, limits: EvidenceVerificationLimits) -> None:
    """Set the bounds the drain on this engine uses (the runtime calls this at start)."""

    _engine_state(engine).limits = limits


def verification_limits(engine: Engine) -> EvidenceVerificationLimits:
    return _engine_state(engine).limits


def stream_state(engine: Engine, stream: str) -> StreamState:
    streams = _engine_state(engine).streams
    state = streams.get(stream)
    if state is None:
        state = StreamState()
        streams[stream] = state
    return state


def _reset_verified_streams() -> None:
    """Forget all verified state, as a process restart does (the test seam)."""

    for engine_state in list(_ENGINES.values()):
        for state in engine_state.streams.values():
            _close(state)
    _ENGINES.clear()


def _close(state: StreamState) -> None:
    attempt, state.in_flight = state.in_flight, None
    if attempt is not None:
        try:
            attempt.session.rollback()
        finally:
            attempt.session.close()


def close_pass(engine: Engine, stream: str) -> None:
    """End an in-flight pass and its snapshot (``AutonomyRuntime.stop``)."""

    state = _engine_state(engine).streams.get(stream)
    if state is not None:
        _close(state)


# --- the six-field bounded projection (an in-file copy of loop.py's shape; a shared helper
# would touch loop.py and views.py, outside FU2's files: technical debt for the sweep) ------


def _bounded_text(
    column: Any, *, maximum: int, storage: str, is_sqlite: bool
) -> tuple[Any, Any, Any]:
    text_value = cast(column, Text)
    if not is_sqlite:
        return literal(storage), func.length(text_value), func.substr(text_value, 1, maximum + 1)
    storage_type = func.typeof(column)
    expected = storage_type == storage
    return (
        storage_type,
        case((expected, func.length(text_value)), else_=literal(None)),
        case((expected, func.substr(text_value, 1, maximum + 1)), else_=literal(None)),
    )


def _bounded_statement(
    session: Session,
    stream: str,
    *,
    row_bytes: int,
    limit: int,
    sequence_from: int | None = None,
    sequence_eq: int | None = None,
) -> Any:
    is_sqlite = session.get_bind().dialect.name == "sqlite"
    fields = []
    for label, column, maximum, storage in (
        ("sequence", HashChainRow.sequence, MAX_DURABLE_SEQUENCE_TEXT_CHARS, "integer"),
        ("kind", HashChainRow.kind, MAX_DURABLE_KIND_TEXT_CHARS, "text"),
        ("payload_json", HashChainRow.payload_json, row_bytes, "text"),
        ("recorded_at", HashChainRow.recorded_at, MAX_DURABLE_TIMESTAMP_TEXT_CHARS, "text"),
        ("previous_hash", HashChainRow.previous_hash, MAX_DURABLE_HASH_TEXT_CHARS, "text"),
        ("record_hash", HashChainRow.record_hash, MAX_DURABLE_HASH_TEXT_CHARS, "text"),
    ):
        storage_type, length, prefix = _bounded_text(
            column, maximum=maximum, storage=storage, is_sqlite=is_sqlite
        )
        fields += [
            storage_type.label(f"{label}_storage_type"),
            length.label(f"{label}_length"),
            prefix.label(label),
        ]
    payload_bytes: Any = func.length(cast(HashChainRow.payload_json, LargeBinary))
    if is_sqlite:
        payload_bytes = case(
            (func.typeof(HashChainRow.payload_json) == "text", payload_bytes), else_=literal(None)
        )
    statement = select(*fields, payload_bytes.label("payload_bytes")).where(
        HashChainRow.stream == stream
    )
    if sequence_eq is not None:
        statement = statement.where(HashChainRow.sequence == sequence_eq)
    elif sequence_from is not None:
        statement = statement.where(HashChainRow.sequence >= sequence_from)
    return statement.order_by(HashChainRow.sequence.asc()).limit(limit)


@dataclass(frozen=True, slots=True)
class _Row:
    sequence: int
    kind: str
    payload_json: str
    recorded_at: datetime
    previous_hash: str
    record_hash: str
    payload_bytes: int


_HEX = frozenset("0123456789abcdef")


def _admit_row(row: Any, *, row_bytes: int) -> _Row:
    """Validate the bounded projection of one record BEFORE it is hashed or classified."""

    if row.sequence_storage_type != "integer" or row.sequence is None:
        raise _Refused(_d0("a record's sequence has an invalid storage type"))
    if row.sequence_length > MAX_DURABLE_SEQUENCE_TEXT_CHARS:
        raise _Refused(_d0("a record's sequence exceeds its durable bound"))
    text_sequence = row.sequence
    if (
        not text_sequence.isascii()
        or not text_sequence.isdecimal()
        or str(int(text_sequence)) != text_sequence
        or int(text_sequence) < 1
    ):
        raise _Refused(_d0("a record's sequence is not a canonical positive integer"))
    sequence = int(text_sequence)
    if row.kind_storage_type != "text" or row.kind is None:
        raise _Refused(_d0(f"record {sequence}: kind has an invalid storage type"))
    if row.kind_length > MAX_DURABLE_KIND_TEXT_CHARS:
        raise _Refused(_d0(f"record {sequence}: kind exceeds its durable bound"))
    labelled = row.kind == EXPIRED_EVENT_KIND
    if row.payload_json_storage_type != "text" or row.payload_json is None:
        # K1's D1 text, kept verbatim: a payload that is not text does not decode
        raise _Refused(
            "a durable expiry record does not decode; refusing closed"
            if labelled
            else "a durable evidence record does not decode; refusing closed"
        )
    if row.payload_json_length > row_bytes or row.payload_bytes > row_bytes:
        raise _Refused(
            _d0(f"record {sequence}: payload_json exceeds the {row_bytes}-byte bound (R13)")
        )
    if row.recorded_at_storage_type != "text" or row.recorded_at is None:
        raise _Refused(_d0(f"record {sequence}: recorded_at has an invalid storage type"))
    if row.recorded_at_length > MAX_DURABLE_TIMESTAMP_TEXT_CHARS:
        raise _Refused(_d0(f"record {sequence}: recorded_at exceeds its durable bound"))
    try:
        recorded_at = datetime.fromisoformat(row.recorded_at)
    except (TypeError, ValueError):
        raise _Refused(_d0(f"record {sequence}: recorded_at is not an ISO timestamp")) from None
    # The stored text is the naive UTC form (UTCDateTime); the digest covers the aware value.
    recorded_at = (
        recorded_at.replace(tzinfo=UTC)
        if recorded_at.tzinfo is None
        else recorded_at.astimezone(UTC)
    )
    for name in ("previous_hash", "record_hash"):
        if getattr(row, f"{name}_storage_type") != "text" or getattr(row, name) is None:
            raise _Refused(_d0(f"record {sequence}: {name} has an invalid storage type"))
        value = getattr(row, name)
        if (
            getattr(row, f"{name}_length") != MAX_DURABLE_HASH_TEXT_CHARS
            or len(value) != MAX_DURABLE_HASH_TEXT_CHARS
            or not set(value) <= _HEX
        ):
            raise _Refused(_d0(f"record {sequence}: {name} is not a 64-character digest"))
    return _Row(
        sequence=sequence,
        kind=row.kind,
        payload_json=row.payload_json,
        recorded_at=recorded_at,
        previous_hash=row.previous_hash,
        record_hash=row.record_hash,
        payload_bytes=int(row.payload_bytes),
    )


def _verify_link(stream: str, row: _Row, *, expected_sequence: int, expected_previous: str) -> None:
    if row.sequence != expected_sequence:
        raise _Refused(_d0(f"record {row.sequence} does not follow record {expected_sequence - 1}"))
    if row.previous_hash != expected_previous:
        raise _Refused(_d0(f"record {row.sequence} does not link to its predecessor"))
    digest = hash_chain.compute_hash(
        stream=stream,
        sequence=row.sequence,
        recorded_at=row.recorded_at,
        payload_json=row.payload_json,
        previous_hash=row.previous_hash,
    )
    if digest != row.record_hash:
        raise _Refused(_d0(f"record {row.sequence} contents do not match its digest"))


def _classify(row: _Row) -> str | None:
    """K1's sealed classification (D1-D3), texts verbatim; returns an expired bundle id.

    An expiry record is recognised by its HASHED payload, never by ``kind``: any row whose
    payload decodes to an object with EXACTLY the keys ``bundle_id`` and ``expires_at``, both
    strings. A row LABELLED as an expiry that is not exactly that shape is a doubt (D2); an
    object with exactly those keys whose values are not both strings is a doubt whatever its
    label (D3); a payload that does not decode or is not an object is a doubt (D1).
    """

    labelled = row.kind == EXPIRED_EVENT_KIND
    try:
        decoded = json.loads(row.payload_json)
    except (ValueError, RecursionError, MemoryError):
        if labelled:
            raise _Refused("a durable expiry record does not decode; refusing closed") from None
        raise _Refused("a durable evidence record does not decode; refusing closed") from None
    if not isinstance(decoded, dict):
        if labelled:
            raise _Refused("a durable expiry record is not a JSON object; refusing closed")
        raise _Refused("a durable evidence record is not a JSON object; refusing closed")
    if set(decoded) == _EXPIRY_PAYLOAD_KEYS:
        recorded = decoded["bundle_id"]
        if not isinstance(recorded, str):
            raise _Refused("a durable expiry record names no string bundle id; refusing closed")
        if not isinstance(decoded["expires_at"], str):
            raise _Refused(
                "a durable expiry record carries a non-string expires_at; refusing closed"
            )
        return recorded
    if labelled:
        if not isinstance(decoded.get("bundle_id"), str):
            raise _Refused("a durable expiry record names no string bundle id; refusing closed")
        raise _Refused(
            "a durable record labelled as an expiry is not the exact expiry shape "
            "(bundle_id and expires_at only); refusing closed"
        )
    return None


def _doubt(state: StreamState, detail: str) -> str:
    state.published = None
    state.doubt = detail
    state.needs_pass = True
    return detail


def _latch(state: StreamState, reason: str) -> str:
    if state.latched is None:
        state.latched = reason
    _close(state)
    return f"{LATCHED_DETAIL} ({state.latched})"


def _durable_expiry_verdict(session: Session, *, stream: str, bundle_id: str) -> str | None:
    """The sticky stage (C1.2): the refusal detail, or ``None`` when no durable verdict or
    doubt stands against ``bundle_id``. One bounded statement; no writes."""

    engine = session.get_bind()
    engine_state = _engine_state(engine)  # type: ignore[arg-type]
    limits = engine_state.limits
    state = stream_state(engine, stream)  # type: ignore[arg-type]
    if state.latched is not None:
        return f"{LATCHED_DETAIL} ({state.latched})"
    if state.doubt is not None:
        return state.doubt
    published = state.published
    if published is None:
        state.needs_pass = True
        return NOT_READY_DETAIL
    reading = _monotonic()
    if not math.isfinite(reading) or reading < published.started_at_monotonic:
        return _doubt(state, MONOTONIC_DETAIL)
    if reading >= published.deadline:
        state.needs_pass = True
        return STALE_DETAIL

    head_rows = 1 if published.sequence >= 1 else 0
    row_limit = limits.resolve_rows + head_rows + 1  # r7 P2-1: the sentinel follows the state
    rows = session.execute(
        _bounded_statement(
            session,
            stream,
            row_bytes=limits.row_bytes,
            limit=row_limit,
            sequence_from=max(published.sequence, 1),
        )
    ).all()
    if len(rows) == row_limit:
        state.needs_pass = True
        return IN_PROGRESS_DETAIL
    try:
        admitted = [_admit_row(row, row_bytes=limits.row_bytes) for row in rows]
        if head_rows:
            head = admitted[0] if admitted else None
            if (
                head is None
                or head.sequence != published.sequence
                or head.record_hash != published.record_hash
            ):
                return _latch(state, "the verified head record is missing or rewritten")
            suffix = admitted[1:]
        else:
            suffix = admitted  # virtual genesis: no physical head row exists
        expected_sequence, expected_previous = published.sequence + 1, published.record_hash
        suffix_expired: set[str] = set()
        for row in suffix:
            _verify_link(
                stream,
                row,
                expected_sequence=expected_sequence,
                expected_previous=expected_previous,
            )
            expired = _classify(row)
            if expired is not None:
                suffix_expired.add(expired)
            expected_sequence, expected_previous = row.sequence + 1, row.record_hash
    except _Refused as refused:
        return _doubt(state, refused.detail)

    end = (
        (suffix[-1].sequence, suffix[-1].record_hash)
        if suffix
        else (
            published.sequence,
            published.record_hash,
        )
    )
    observed = state.observed
    if observed is not None and observed[0] > published.sequence:
        if observed[0] > end[0]:
            return _latch(state, "the stream is shorter than a record this process verified")
        at = next(row for row in suffix if row.sequence == observed[0])
        if at.record_hash != observed[1]:
            return _latch(state, "a record this process verified was replaced")
    if end[0] >= 1 and end[0] > (observed[0] if observed is not None else 0):
        state.observed = end
    if bundle_id in published.expired_ids or bundle_id in suffix_expired:
        return _STICKY_DETAIL
    return None


# --- the off-path pass (C1.3): one snapshot, chunked, published only by proof ------------


def _start_pass(sessions: sessionmaker[Session], state: StreamState) -> None:
    started = _monotonic()
    session = sessions()
    try:
        session.execute(text("BEGIN"))  # one SQLite snapshot for the whole pass (S2)
    except BaseException:
        session.close()
        raise
    state.in_flight = _PassAttempt(
        session=session,
        cursor=0,
        partial_hash=hash_chain.GENESIS_HASH,
        partial_expired=set(),
        observed_at_start=state.observed,
        start_proven=state.observed is None,
        started_at_monotonic=started,
    )


def _run_pass_chunk(
    stream: str, state: StreamState, limits: EvidenceVerificationLimits
) -> PassOutcome:
    attempt = state.in_flight
    assert attempt is not None
    rows = attempt.session.execute(
        _bounded_statement(
            attempt.session,
            stream,
            row_bytes=limits.row_bytes,
            limit=limits.pass_rows + 1,
            sequence_from=attempt.cursor + 1,
        )
    ).all()
    reached_end = len(rows) <= limits.pass_rows
    admitted_bytes = verified = 0
    for raw in rows[: limits.pass_rows]:
        row = _admit_row(raw, row_bytes=limits.row_bytes)
        if verified and admitted_bytes + row.payload_bytes > limits.pass_bytes_per_tick:
            reached_end = False  # this row leads the next chunk (Ct defers, never aborts)
            break
        _verify_link(
            stream,
            row,
            expected_sequence=attempt.cursor + 1,
            expected_previous=attempt.partial_hash,
        )
        expired = _classify(row)
        if expired is not None:
            attempt.partial_expired.add(expired)
            if len(attempt.partial_expired) > limits.expired_ids:
                raise _Refused(
                    "the account's evidence stream retains more than the "
                    f"{limits.expired_ids}-id retained expired-id bound; refusing closed until "
                    "an owner raises the bound (R13)"
                )
        start = attempt.observed_at_start
        if start is not None and row.sequence == start[0]:
            if row.record_hash != start[1]:
                raise _Latch("a record this process verified before the pass began was replaced")
            attempt.start_proven = True
        attempt.cursor, attempt.partial_hash = row.sequence, row.record_hash
        admitted_bytes += row.payload_bytes
        verified += 1
    if not reached_end:
        return PassOutcome(rows_verified=verified, in_progress=True)
    return _publish(stream, state, limits, verified)


def _publish(
    stream: str, state: StreamState, limits: EvidenceVerificationLimits, verified: int
) -> PassOutcome:
    attempt = state.in_flight
    assert attempt is not None
    end = (attempt.cursor, attempt.partial_hash)
    if attempt.observed_at_start is not None and not attempt.start_proven:
        raise _Latch("the stream is shorter than a record this process verified")
    live = state.observed
    if live is not None and live != attempt.observed_at_start:
        if live[0] <= end[0]:
            rows = attempt.session.execute(
                _bounded_statement(
                    attempt.session,
                    stream,
                    row_bytes=limits.row_bytes,
                    limit=1,
                    sequence_eq=live[0],
                )
            ).all()
            again = _admit_row(rows[0], row_bytes=limits.row_bytes) if rows else None
            if again is None or again.record_hash != live[1]:
                raise _Latch("a record this process verified contradicts the pass's snapshot")
        else:
            # R17 (Kevin, Daybreak's stricter rule): the snapshot cannot speak to a tuple
            # verified beyond its end, so the pass is discarded and restarts from a snapshot
            # that contains it. Fail-closed cost: a stream that grows during every pass never
            # publishes a newer state.
            _close(state)
            state.needs_pass = True
            return PassOutcome(rows_verified=verified, discarded=True)
    started = attempt.started_at_monotonic
    deadline = started + limits.max_age_seconds
    if not (math.isfinite(started) and math.isfinite(deadline) and deadline > started):
        raise _Refused(_DEADLINE_DETAIL)
    state.published = PublishedState(
        sequence=end[0],
        record_hash=end[1],
        expired_ids=frozenset(attempt.partial_expired),
        started_at_monotonic=started,
        deadline=deadline,
    )
    if end[0] >= 1 and end[0] > (live[0] if live is not None else 0):
        state.observed = end
    state.doubt = None
    state.attempts = 0
    state.needs_pass = True  # R4(d): continuous re-certification
    _close(state)
    return PassOutcome(rows_verified=verified, published=True)


def run_verification_tick(sessions: sessionmaker[Session], stream: str) -> PassOutcome:
    """One tick of the pass: start one if needed, run exactly ONE chunk. Never raises.

    The tick's work is bounded by one chunk (P rows, Ct bytes) on every topology (Daybreak
    FU2-BUILD P2-1). An in-memory database (``StaticPool``: one shared connection) cannot hold
    the pass's snapshot across ticks without sharing the drain's transaction, so a pass that
    needs more than one chunk there is aborted (snapshot closed, nothing published) and the
    stream refuses closed with :data:`SINGLE_CONNECTION_DETAIL`.
    """

    engine = sessions.kw["bind"]
    limits = _engine_state(engine).limits
    state = stream_state(engine, stream)
    if state.latched is not None:
        return PassOutcome()
    verified = 0
    try:
        if state.in_flight is None:
            if state.attempts >= limits.max_pass_attempts:
                return PassOutcome()
            _start_pass(sessions, state)
        outcome = _run_pass_chunk(stream, state, limits)
        verified = outcome.rows_verified
        if outcome.in_progress and isinstance(engine.pool, StaticPool):
            return _abort(state, SINGLE_CONNECTION_DETAIL, verified)
        return outcome
    except _Latch as latched:
        _latch(state, latched.reason)
        return PassOutcome(rows_verified=verified, latched=True)
    except _Refused as refused:
        return _abort(state, refused.detail, verified)
    except Exception as error:
        return _abort(
            state,
            f"the verification pass failed ({type(error).__name__}); refusing closed until a "
            "clean pass",
            verified,
        )


def _abort(state: StreamState, detail: str, verified: int) -> PassOutcome:
    try:
        _close(state)
    except Exception:  # pragma: no cover - the pass is gone either way
        state.in_flight = None
    _doubt(state, detail)
    state.attempts += 1
    return PassOutcome(rows_verified=verified, aborted=detail)


def certify_stream(sessions: sessionmaker[Session], stream: str) -> PassOutcome:
    """Run verification ticks until the pass ends (published, latched, aborted or discarded)."""

    verified = 0
    while True:
        outcome = run_verification_tick(sessions, stream)
        verified += outcome.rows_verified
        if not outcome.in_progress:
            return replace(outcome, rows_verified=verified)


def resolve(
    session: Session,
    *,
    account_fingerprint: str,
    cited_ids: tuple[str, ...],
    proposer_id: str,
    proposer_credential_epoch: str | None,
    proposer_registry_entry_digest: str | None,
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
    judge. An EXPIRED verdict is durable: the first one is recorded on the
    account's evidence stream, and a later resolve of that bundle by the SAME
    proposer and registration refuses EXPIRED even if the clock has since rewound
    or the process restarted (D-26). A different proposer or registration keeps
    FOREIGN / REGISTRATION_UNBOUND / REGISTRATION_REPLACED (those checks come
    first), and once the row is pruned the bundle resolves UNISSUED.
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
        if (
            row.proposer_credential_epoch is None
            or row.proposer_registry_entry_digest is None
            or proposer_credential_epoch is None
            or proposer_registry_entry_digest is None
        ):
            return Resolution(
                refusal=ResolutionRefusal.REGISTRATION_UNBOUND,
                detail=(
                    "the cited evidence bundle or proposal predates immutable registration "
                    "binding; its credential epoch cannot be inferred from current configuration"
                ),
            )
        if (
            row.proposer_credential_epoch != proposer_credential_epoch
            or row.proposer_registry_entry_digest != proposer_registry_entry_digest
        ):
            return Resolution(
                refusal=ResolutionRefusal.REGISTRATION_REPLACED,
                detail=(
                    "the cited evidence bundle was issued under a different credential epoch "
                    "or registry entry; queued authority does not transfer across replacement"
                ),
            )
        sticky_detail = _durable_expiry_verdict(
            session,
            stream=hash_chain_stream(account_fingerprint),
            bundle_id=row.bundle_id,
        )
        if sticky_detail is not None:
            return Resolution(
                refusal=ResolutionRefusal.EXPIRED,
                detail=sticky_detail,
            )
        if now >= row.expires_at:
            hash_chain.append(
                session,
                stream=hash_chain_stream(account_fingerprint),
                kind=EXPIRED_EVENT_KIND,
                payload={
                    "bundle_id": row.bundle_id,
                    "expires_at": row.expires_at.isoformat(),
                },
                recorded_at=now,
            )
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
                proposer_credential_epoch=row.proposer_credential_epoch,
                proposer_registry_entry_digest=row.proposer_registry_entry_digest,
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
