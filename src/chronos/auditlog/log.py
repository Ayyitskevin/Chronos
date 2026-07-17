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


def verify_chain(path: Path) -> tuple[bool, str]:
    """Verify the whole chain; returns (ok, detail)."""

    if not path.exists():
        return True, "no audit log yet"
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
                return False, f"line {line_number}: unreadable record: {error}"
            if int(record["sequence"]) != expected_sequence:
                return False, f"line {line_number}: sequence gap"
            if record["previous_hash"] != previous:
                return False, f"line {line_number}: chain break"
            if recomputed != record["record_hash"]:
                return False, f"line {line_number}: hash mismatch"
            previous = str(record["record_hash"])
            expected_sequence += 1
    return True, f"chain intact ({expected_sequence} records)"
