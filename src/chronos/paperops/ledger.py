"""Append-only paper decision ledger with hash-chain integrity.

Distinct from ``chronos.auditlog`` (generic kinds) and execution ledgers
(intent lifecycle): this store is schema-strict for paper decision provenance
and is the sole input for deterministic paperops replay.

**Concurrency.** Every ``append`` takes an exclusive OS file lock
(:func:`decision_ledger_lock`), re-reads the tail under that lock, then writes.
Two processes cannot both append into a corrupt chain: they serialize, and each
rebinds sequence/previous_hash from the on-disk head before writing.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chronos.paperops.reasons import DecisionKind, DecisionOutcome, PaperReasonCode
from chronos.paperops.records import (
    DecisionRecord,
    RecordValidationError,
    inputs_fingerprint,
    sanitize_payload,
)
from chronos.utils.secure_files import secure_owner_only

_GENESIS = "0" * 64
_SCHEMA = "paper-decision-ledger-v1"


class DecisionLedgerError(RuntimeError):
    """Ledger is corrupt, incomplete, or refused a write (fail closed)."""


@contextmanager
def decision_ledger_lock(ledger_path: Path) -> Iterator[None]:
    """Exclusive OS file lock for a read-verify-append critical section."""

    lock_path = ledger_path.parent / (ledger_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            secure_owner_only(lock_path)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _hash_record_body(
    *,
    sequence: int,
    at_utc: str,
    kind: str,
    reason_code: str,
    outcome: str,
    strategy_id: str,
    strategy_version: str,
    config_hash: str,
    data_timestamp_utc: str | None,
    data_source: str,
    data_quality_label: str,
    inputs_fingerprint: str,
    payload_json: str,
    previous_hash: str,
) -> str:
    material = (
        f"{sequence}|{at_utc}|{kind}|{reason_code}|{outcome}|{strategy_id}|"
        f"{strategy_version}|{config_hash}|{data_timestamp_utc}|{data_source}|"
        f"{data_quality_label}|{inputs_fingerprint}|{payload_json}|{previous_hash}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DecisionEvent:
    """Caller-supplied event before sequence/hash assignment."""

    kind: DecisionKind
    reason_code: PaperReasonCode
    outcome: DecisionOutcome
    strategy_id: str
    strategy_version: str
    config_hash: str
    data_timestamp_utc: str | None
    data_source: str
    data_quality_label: str
    decision_inputs: Mapping[str, Any]
    payload: Mapping[str, Any]


class DecisionLedger:
    """Append-only writer over one JSONL path."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._sequence, self._last_hash = self._recover()

    @property
    def path(self) -> Path:
        return self._path

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
            if record.get("schema") != _SCHEMA:
                raise DecisionLedgerError(
                    f"unsupported ledger schema {record.get('schema')!r}; "
                    f"refusing to append to {self._path}"
                )
            return int(record["sequence"]) + 1, str(record["record_hash"])
        except DecisionLedgerError:
            raise
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as error:
            raise DecisionLedgerError(
                f"decision ledger last record unreadable; refuse to append: {self._path}: {error}"
            ) from error

    def append(self, event: DecisionEvent, *, at_utc: str | None = None) -> DecisionRecord:
        """Append under an exclusive lock (safe for concurrent writers)."""

        self._validate_event(event)
        with decision_ledger_lock(self._path):
            return self._append_locked(event, at_utc=at_utc)

    def append_under_held_lock(
        self, event: DecisionEvent, *, at_utc: str | None = None
    ) -> DecisionRecord:
        """Append assuming the caller already holds :func:`decision_ledger_lock`.

        Used by ``record_paper_decision`` so rehydrate → evaluate → append is
        one critical section (prevents concurrent same-fingerprint double ALLOW).
        """

        self._validate_event(event)
        return self._append_locked(event, at_utc=at_utc)

    @staticmethod
    def _validate_event(event: DecisionEvent) -> None:
        if not event.strategy_id.strip():
            raise DecisionLedgerError("strategy_id is required")
        if not event.strategy_version.strip():
            raise DecisionLedgerError("strategy_version is required")
        if not event.config_hash.strip():
            raise DecisionLedgerError("config_hash is required")
        if not event.data_source.strip():
            raise DecisionLedgerError("data_source is required (use 'missing' if unknown)")

    def _append_locked(self, event: DecisionEvent, *, at_utc: str | None) -> DecisionRecord:
        # Fail closed if another writer left a corrupt chain.
        ok, detail, _ = load_and_verify(self._path)
        if not ok:
            raise DecisionLedgerError(f"refusing to append to corrupt decision ledger: {detail}")
        # Always rebind sequence/hash from disk under the lock (stale in-memory
        # counters after concurrent appends from another process/instance).
        self._sequence, self._last_hash = self._recover()

        at = at_utc or datetime.now(tz=UTC).isoformat()
        payload = sanitize_payload(dict(event.payload))
        fingerprint = inputs_fingerprint(dict(event.decision_inputs))
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        record_hash = _hash_record_body(
            sequence=self._sequence,
            at_utc=at,
            kind=event.kind.value,
            reason_code=event.reason_code.value,
            outcome=event.outcome.value,
            strategy_id=event.strategy_id,
            strategy_version=event.strategy_version,
            config_hash=event.config_hash,
            data_timestamp_utc=event.data_timestamp_utc,
            data_source=event.data_source,
            data_quality_label=event.data_quality_label,
            inputs_fingerprint=fingerprint,
            payload_json=payload_json,
            previous_hash=self._last_hash,
        )
        record = DecisionRecord(
            sequence=self._sequence,
            at_utc=at,
            kind=event.kind.value,
            reason_code=event.reason_code.value,
            outcome=event.outcome.value,
            strategy_id=event.strategy_id,
            strategy_version=event.strategy_version,
            config_hash=event.config_hash,
            data_timestamp_utc=event.data_timestamp_utc,
            data_source=event.data_source,
            data_quality_label=event.data_quality_label,
            inputs_fingerprint=fingerprint,
            payload=payload,
            previous_hash=self._last_hash,
            record_hash=record_hash,
        )
        line_obj = {"schema": _SCHEMA, **record.to_dict()}
        line = json.dumps(line_obj, sort_keys=True, separators=(",", ":"))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        secure_owner_only(self._path)
        self._sequence += 1
        self._last_hash = record_hash
        return record

    def read_all(self) -> tuple[DecisionRecord, ...]:
        ok, detail, records = load_and_verify(self._path)
        if not ok:
            raise DecisionLedgerError(detail)
        return records


def load_and_verify(path: Path) -> tuple[bool, str, tuple[DecisionRecord, ...]]:
    """Load every record and verify the hash chain. Fail closed on any fault."""

    if not path.exists():
        return True, "no decision ledger yet", ()
    previous = _GENESIS
    expected_sequence = 0
    records: list[DecisionRecord] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if raw.get("schema") != _SCHEMA:
                    return (
                        False,
                        f"line {line_number}: unsupported schema {raw.get('schema')!r}",
                        (),
                    )
                record = DecisionRecord.from_dict(raw)
                payload_json = json.dumps(record.payload, sort_keys=True, separators=(",", ":"))
                recomputed = _hash_record_body(
                    sequence=record.sequence,
                    at_utc=record.at_utc,
                    kind=record.kind,
                    reason_code=record.reason_code,
                    outcome=record.outcome,
                    strategy_id=record.strategy_id,
                    strategy_version=record.strategy_version,
                    config_hash=record.config_hash,
                    data_timestamp_utc=record.data_timestamp_utc,
                    data_source=record.data_source,
                    data_quality_label=record.data_quality_label,
                    inputs_fingerprint=record.inputs_fingerprint,
                    payload_json=payload_json,
                    previous_hash=record.previous_hash,
                )
            except (
                json.JSONDecodeError,
                KeyError,
                ValueError,
                TypeError,
                RecordValidationError,
            ) as error:
                return False, f"line {line_number}: unreadable record: {error}", ()
            if record.sequence != expected_sequence:
                return False, f"line {line_number}: sequence gap", ()
            if record.previous_hash != previous:
                return False, f"line {line_number}: chain break", ()
            if recomputed != record.record_hash:
                return False, f"line {line_number}: hash mismatch", ()
            previous = record.record_hash
            expected_sequence += 1
            records.append(record)
    return True, f"chain intact ({expected_sequence} records)", tuple(records)


def verify_decision_ledger(path: Path) -> tuple[bool, str]:
    ok, detail, _ = load_and_verify(path)
    return ok, detail
