"""Tamper-evident hash chaining for append-only database rows (M3).

``chronos.auditlog.log`` has hash-chained the JSONL audit file since Phase 9:
each record embeds the SHA-256 of the one before it, so an in-place edit,
a deletion, or a reordering breaks the chain and ``verify_chain`` detects it.
The database's append-only tables had no equivalent. They were append-only *by
convention* — nothing in the code updates or deletes a row — which is a
statement about the code, not about the data. Anyone with the file could edit a
row and nothing would notice.

That gap mattered more once M3 put the supervisor's durable state in this
database. A loss counter or a decision journal is only as trustworthy as its
resistance to silent revision, and the deterministic kernel is supposed to be
able to *prove* what it decided, not merely assert it.

Design, and why each part is the way it is:

- **The chain is per stream**, not global. ``(stream, sequence)`` is unique, and
  each row's ``previous_hash`` is the ``record_hash`` of ``sequence - 1`` in the
  same stream. A global chain would serialize unrelated writers behind one
  another, and a break in one stream would invalidate every other stream's
  history — a corruption blast radius with no safety benefit.
- **The digest covers the row's identity, not just its payload.** ``stream``,
  ``sequence`` and ``recorded_at`` are hashed along with the canonical payload,
  so a row cannot be moved between streams, renumbered, or re-dated without
  breaking.
- **Payloads are canonicalized before hashing** — sorted keys, no insignificant
  whitespace — so a semantically identical payload always produces the same
  digest, and verification does not depend on how a particular JSON encoder
  happened to format it.
- **Appending is serialized by the database, not by a lock.** The unique
  constraint on ``(stream, sequence)`` is what makes a concurrent double-append
  fail rather than fork the chain. Two writers racing produces an
  ``IntegrityError`` for the loser, which is correct: under the single-writer
  lease a second writer is already a bug, and a chain that silently forked
  would be worse than one that refused.

**Honest bound.** This is *tamper-evident*, not tamper-proof. An attacker who
can write to the database can also recompute every subsequent digest and
present an internally consistent chain — a hash chain in the same file it
protects cannot prevent that, and neither can the JSONL log. What it defeats is
the realistic case: a targeted edit, a deleted row, a reordering, or a partial
corruption, none of which will recompute the tail. Defeating a full rewrite
needs an external anchor (an offsite copy of the head digest, or signing), which
Chronos does not have and this module does not claim.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Text, case, cast, func, literal, select
from sqlalchemy.orm import Session

from chronos.config.limits import (
    MAX_DURABLE_HASH_TEXT_CHARS,
    MAX_DURABLE_SEQUENCE_TEXT_CHARS,
)
from chronos.persistence.schema import HashChainRow

#: The ``previous_hash`` of the first record in any stream. A literal zero
#: digest, matching ``chronos.auditlog.log``, so the two chains read alike.
GENESIS_HASH = "0" * 64


class HashChainError(RuntimeError):
    """The chain could not be extended or did not verify.

    A distinct type so a caller can halt on audit failure specifically, rather
    than catching every ``RuntimeError`` the persistence layer might raise —
    the same reason ``AuditLogCorruptionError`` exists for the JSONL log.
    """


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """The result of verifying one stream. ``ok`` is never a default."""

    ok: bool
    stream: str
    records: int
    detail: str = ""
    #: 1-indexed sequence of the first record that failed, if any.
    broken_at: int | None = None


def canonical_payload(payload: dict[str, Any]) -> str:
    """Serialize a payload so equal payloads always hash equally.

    ``sort_keys`` removes key-order sensitivity and the compact separators
    remove whitespace sensitivity, so verification does not silently depend on
    the encoder that happened to write the row.
    """

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(
    *,
    stream: str,
    sequence: int,
    recorded_at: datetime,
    payload_json: str,
    previous_hash: str,
) -> str:
    """Digest one record. Identity fields are covered, not only the payload."""

    normalized_at = recorded_at.astimezone(UTC) if recorded_at.tzinfo is not None else recorded_at
    material = f"{stream}|{sequence}|{normalized_at.isoformat()}|{payload_json}|{previous_hash}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def head(session: Session, stream: str) -> HashChainRow | None:
    """The most recent record in ``stream``, or ``None`` for an empty chain."""

    return session.scalar(
        select(HashChainRow)
        .where(HashChainRow.stream == stream)
        .order_by(HashChainRow.sequence.desc())
        .limit(1)
    )


def _bounded_head_link(session: Session, stream: str) -> tuple[int, str] | None:
    """Read only a bounded sequence/hash link for an append.

    SQLite does not enforce declared ``Integer``/``String`` widths.  Selecting
    the full ORM head would also materialize its unrelated payload, allowing a
    corrupt durable row to allocate arbitrary text after a caller's integrity
    check.  The database projects bounded scalar text instead, and SQLite's
    storage class is checked explicitly before the values can extend a chain.
    """

    sequence_text = cast(HashChainRow.sequence, Text)
    record_hash_text = cast(HashChainRow.record_hash, Text)
    sequence_storage_type: Any
    sequence_length: Any
    sequence_value: Any
    record_hash_storage_type: Any
    record_hash_length: Any
    record_hash_value: Any
    if session.get_bind().dialect.name == "sqlite":
        sequence_storage_type = func.typeof(HashChainRow.sequence)
        sequence_is_integer = sequence_storage_type == "integer"
        sequence_length = case(
            (sequence_is_integer, func.length(sequence_text)),
            else_=literal(None),
        )
        sequence_value = case(
            (
                sequence_is_integer,
                func.substr(sequence_text, 1, MAX_DURABLE_SEQUENCE_TEXT_CHARS + 1),
            ),
            else_=literal(None),
        )
        record_hash_storage_type = func.typeof(HashChainRow.record_hash)
        record_hash_is_text = record_hash_storage_type == "text"
        record_hash_length = case(
            (record_hash_is_text, func.length(record_hash_text)),
            else_=literal(None),
        )
        record_hash_value = case(
            (
                record_hash_is_text,
                func.substr(record_hash_text, 1, MAX_DURABLE_HASH_TEXT_CHARS + 1),
            ),
            else_=literal(None),
        )
    else:
        sequence_storage_type = literal("integer")
        sequence_length = func.length(sequence_text)
        sequence_value = func.substr(
            sequence_text,
            1,
            MAX_DURABLE_SEQUENCE_TEXT_CHARS + 1,
        )
        record_hash_storage_type = literal("text")
        record_hash_length = func.length(record_hash_text)
        record_hash_value = func.substr(
            record_hash_text,
            1,
            MAX_DURABLE_HASH_TEXT_CHARS + 1,
        )
    row = session.execute(
        select(
            sequence_storage_type.label("sequence_storage_type"),
            sequence_length.label("sequence_length"),
            sequence_value.label("sequence_text"),
            record_hash_storage_type.label("record_hash_storage_type"),
            record_hash_length.label("record_hash_length"),
            record_hash_value.label("record_hash"),
        )
        .where(HashChainRow.stream == stream)
        .order_by(HashChainRow.sequence.desc())
        .limit(1)
    ).one_or_none()
    if row is None:
        return None
    if row.sequence_storage_type != "integer":
        raise HashChainError("hash-chain head sequence has an invalid storage type")
    if row.record_hash_storage_type != "text":
        raise HashChainError("hash-chain head digest has an invalid storage type")
    if row.sequence_length > MAX_DURABLE_SEQUENCE_TEXT_CHARS:
        raise HashChainError("hash-chain head sequence exceeds its durable bound")
    sequence_text_value = row.sequence_text
    if (
        not sequence_text_value.isascii()
        or not sequence_text_value.isdecimal()
        or str(int(sequence_text_value)) != sequence_text_value
    ):
        raise HashChainError("hash-chain head sequence is not a canonical integer")
    sequence = int(sequence_text_value)
    if sequence <= 0:
        raise HashChainError("hash-chain head sequence must be positive")
    record_hash = row.record_hash
    if (
        not isinstance(record_hash, str)
        or row.record_hash_length != MAX_DURABLE_HASH_TEXT_CHARS
        or any(character not in "0123456789abcdef" for character in record_hash)
    ):
        raise HashChainError("hash-chain head digest is invalid")
    return sequence, record_hash


def append(
    session: Session,
    *,
    stream: str,
    kind: str,
    payload: dict[str, Any],
    recorded_at: datetime,
) -> HashChainRow:
    """Append one record to ``stream``, linked to the record before it.

    The caller supplies the session so an append participates in the *same*
    transaction as whatever it is recording. That is deliberate: an audit record
    that commits separately from the fact it describes can outlive a rolled-back
    fact, or be lost while the fact survives. Both are worse than an atomic
    write that either records everything or nothing.
    """

    if not stream:
        raise HashChainError("a hash-chain stream must be named")
    if recorded_at.tzinfo is None:
        # A naive timestamp would hash differently depending on the reader's
        # assumptions about its zone, which would break verification later.
        raise HashChainError(f"recorded_at must be timezone-aware, got {recorded_at!r}")

    previous = _bounded_head_link(session, stream)
    sequence = 1 if previous is None else previous[0] + 1
    previous_hash = GENESIS_HASH if previous is None else previous[1]

    normalized_recorded_at = recorded_at.astimezone(UTC)
    payload_json = canonical_payload(payload)
    record_hash = compute_hash(
        stream=stream,
        sequence=sequence,
        recorded_at=normalized_recorded_at,
        payload_json=payload_json,
        previous_hash=previous_hash,
    )
    row = HashChainRow(
        stream=stream,
        sequence=sequence,
        kind=kind,
        payload_json=payload_json,
        recorded_at=normalized_recorded_at,
        previous_hash=previous_hash,
        record_hash=record_hash,
    )
    session.add(row)
    # Surface a duplicate (stream, sequence) here rather than at commit, so the
    # caller learns immediately that another writer forked the chain.
    session.flush()
    return row


def verify(session: Session, stream: str) -> ChainVerification:
    """Recompute every digest in ``stream`` and check the links.

    Returns a result rather than raising: verification is something an operator
    runs to *learn* the state, and a corrupt chain is an answer, not an
    exception. Callers that must halt on failure check ``ok``.
    """

    expected_previous = GENESIS_HASH
    record_count = 0
    broken_at: int | None = None
    broken_detail = ""
    rows = session.execute(
        select(
            HashChainRow.stream,
            HashChainRow.sequence,
            HashChainRow.recorded_at,
            HashChainRow.payload_json,
            HashChainRow.previous_hash,
            HashChainRow.record_hash,
        )
        .where(HashChainRow.stream == stream)
        .order_by(HashChainRow.sequence.asc())
        .execution_options(yield_per=100)
    )
    for index, row in enumerate(rows, start=1):
        record_count = index
        # Preserve the first failure but drain the streamed cursor so ``records``
        # keeps its documented whole-stream meaning without retaining every row.
        if broken_at is not None:
            continue
        if row.sequence != index:
            broken_at = index
            broken_detail = (
                f"sequence gap: expected {index}, found {row.sequence}. A record was "
                "deleted or renumbered."
            )
            continue
        if row.previous_hash != expected_previous:
            broken_at = row.sequence
            broken_detail = (
                f"record {row.sequence} does not link to its predecessor; the chain was "
                "reordered or a record was replaced."
            )
            continue
        recomputed = compute_hash(
            stream=row.stream,
            sequence=row.sequence,
            recorded_at=row.recorded_at,
            payload_json=row.payload_json,
            previous_hash=row.previous_hash,
        )
        if recomputed != row.record_hash:
            broken_at = row.sequence
            broken_detail = (
                f"record {row.sequence} was modified after it was written; its contents no "
                "longer match its digest."
            )
            continue
        expected_previous = row.record_hash

    if record_count == 0:
        return ChainVerification(ok=True, stream=stream, records=0, detail="chain is empty")
    if broken_at is not None:
        return ChainVerification(
            ok=False,
            stream=stream,
            records=record_count,
            broken_at=broken_at,
            detail=broken_detail,
        )
    return ChainVerification(
        ok=True,
        stream=stream,
        records=record_count,
        detail=f"{record_count} record(s) verified to head {expected_previous[:12]}…",
    )


def payload_of(row: HashChainRow) -> dict[str, Any]:
    """Decode a stored payload. Never used for hashing — the JSON text is."""

    decoded: dict[str, Any] = json.loads(row.payload_json)
    return decoded
