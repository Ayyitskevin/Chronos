"""Hash-chained JSONL audit log.

Each record embeds the SHA-256 of the previous record, so any in-place edit,
deletion, or reordering breaks the chain and is detected by ``verify_chain``.
Writes are flushed and fsynced before returning; a failed write raises so the
caller (execution engine / halt monitor) can halt trading on audit failure.

No secrets, credentials, or raw account identifiers may be written here;
callers pass already-sanitized payloads. Payload values are JSON-serializable
primitives only.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from chronos.utils.secure_files import secure_owner_only

_GENESIS = "0" * 64


class AuditLogCorruptionError(RuntimeError):
    """The audit log's last record could not be recovered; construction fails
    closed with this specific, catchable exception rather than a raw
    ``json.JSONDecodeError`` or ``KeyError`` so a caller can halt trading
    cleanly (see ``HaltReason.AUDIT_LOG_FAILURE``)."""


@dataclass(frozen=True, slots=True)
class AuditRecord:
    sequence: int
    at_utc: str
    kind: str
    payload: dict[str, object]
    previous_hash: str
    record_hash: str


def _hash_record(sequence: int, at_utc: str, kind: str, payload_json: str, prev: str) -> str:
    material = f"{sequence}|{at_utc}|{kind}|{payload_json}|{prev}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class AuditLog:
    """Append-only writer over one JSONL file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._sequence, self._last_hash = self._recover()

    def _recover(self) -> tuple[int, str]:
        if not self._path.exists():
            return 0, _GENESIS
        last_line = ""
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last_line = line
        if not last_line:
            return 0, _GENESIS
        try:
            record = json.loads(last_line)
            return int(record["sequence"]) + 1, str(record["record_hash"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as error:
            raise AuditLogCorruptionError(
                f"audit log's last record is unreadable, refusing to append past it: "
                f"{self._path}: {error}"
            ) from error

    def append(self, kind: str, payload: dict[str, object]) -> AuditRecord:
        at = datetime.now(tz=UTC).isoformat()
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        record_hash = _hash_record(self._sequence, at, kind, payload_json, self._last_hash)
        record = AuditRecord(
            sequence=self._sequence,
            at_utc=at,
            kind=kind,
            payload=payload,
            previous_hash=self._last_hash,
            record_hash=record_hash,
        )
        line = json.dumps(
            {
                "sequence": record.sequence,
                "at_utc": record.at_utc,
                "kind": record.kind,
                "payload": payload,
                "previous_hash": record.previous_hash,
                "record_hash": record.record_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        secure_owner_only(self._path)
        self._sequence += 1
        self._last_hash = record_hash
        return record


class ChainState(StrEnum):
    """The three distinguishable outcomes of verifying an audit chain.

    ABSENT is not a weaker VALID. A missing audit log means the chain could not be
    examined at all, so nothing about tamper-evidence has been established — the same
    distinction the certification plane draws between NOT_CERTIFIED and UNVERIFIED.
    """

    VALID = "VALID"
    BROKEN = "BROKEN"
    ABSENT = "ABSENT"


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """The verdict and its detail.

    Deliberately NOT a ``(bool, str)`` tuple and deliberately not unpackable: the old
    signature returned ``True`` for a missing file, so every caller that wrote
    ``ok, detail = verify_chain(...)`` silently reported an absent chain as verified.
    Making the type un-unpackable turns each of those into a loud failure rather than a
    wrong answer, which is why the migration is by type rather than by convention.

    No ``.ok`` is provided on purpose: it would have to choose a truthiness for ABSENT,
    which is the defect this exists to remove. ``__bool__`` RAISES for the same reason —
    omitting it is not enough, because a dataclass without ``__bool__`` is truthy by
    default, so ``if verify_chain(path):`` answered True for a missing chain and
    reproduced the original bug one layer down.
    """

    state: ChainState
    detail: str

    def __bool__(self) -> bool:
        """Refuse truth-testing; there is no correct answer for ABSENT.

        Raising means ``if verify_chain(path):`` and ``assert verify_chain(path)`` fail
        loudly at the call site instead of silently reporting an unexamined chain as a
        verified one. Compare ``.state`` against a :class:`ChainState` member instead.
        """

        raise TypeError(
            f"ChainVerification({self.state.value}) has no truth value: compare .state "
            f"against a ChainState member (VALID, BROKEN or ABSENT) instead"
        )


def verify_chain(path: Path) -> ChainVerification:
    """Verify the whole chain, distinguishing absent from valid from broken."""

    if not path.exists():
        return ChainVerification(ChainState.ABSENT, "no audit log yet")
    previous = _GENESIS
    expected_sequence = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                payload_json = json.dumps(record["payload"], sort_keys=True, separators=(",", ":"))
                recomputed = _hash_record(
                    int(record["sequence"]),
                    str(record["at_utc"]),
                    str(record["kind"]),
                    payload_json,
                    str(record["previous_hash"]),
                )
            except (KeyError, ValueError, TypeError) as error:
                return ChainVerification(
                    ChainState.BROKEN, f"line {line_number}: unreadable record: {error}"
                )
            if int(record["sequence"]) != expected_sequence:
                return ChainVerification(ChainState.BROKEN, f"line {line_number}: sequence gap")
            if record["previous_hash"] != previous:
                return ChainVerification(ChainState.BROKEN, f"line {line_number}: chain break")
            if recomputed != record["record_hash"]:
                return ChainVerification(ChainState.BROKEN, f"line {line_number}: hash mismatch")
            previous = str(record["record_hash"])
            expected_sequence += 1
    return ChainVerification(ChainState.VALID, f"chain intact ({expected_sequence} records)")
