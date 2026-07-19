"""The experiment-registry ledger (ADR-0013 §1, C2 review remediation).

A tamper-evident, append-only record of every research run and every holdout event,
built on the platform's hash-chained :class:`chronos.auditlog.AuditLog`.

**Truncation/rollback detection (review F1/safety-1).** A bare hash chain cannot detect
that its *tail* was deleted — a valid prefix is itself a valid chain. So each append
also updates an out-of-band **head anchor** (``registry.head.json``: expected record
count + last record hash, owner-only). ``verify()`` requires the chain to be internally
consistent **and** to match the anchor, so deleting the trailing ``holdout_consume``
line (which would un-burn a window) is detected rather than passing as "intact".

Honest residual: an actor who deletes/rewrites *both* the ledger and its anchor
consistently can still reset state — the anchor is not a signed, off-host root of trust.
The guarantee is **detection of accidental/incidental truncation, deletion, in-place
edits, and rollback**, not prevention against a writer who controls both files.

**Concurrency (review safety-2).** Guardian read-verify-append critical sections take an
exclusive OS file lock via :func:`registry_lock`, so two processes cannot both consume
one grant or interleave appends into a corrupt chain.

Research-plane only: opens no trading database and imports no order/broker module.
"""

from __future__ import annotations

import fcntl
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from chronos.auditlog.log import AuditLog, AuditRecord, verify_chain
from chronos.utils.secure_files import secure_owner_only

# Record kinds (the ledger's controlled vocabulary).
KIND_RUN = "experiment_run"
KIND_UNLOCK = "holdout_unlock"
KIND_CONSUME = "holdout_consume"


@contextmanager
def registry_lock(ledger_path: Path) -> Iterator[None]:
    """Exclusive OS file lock guarding a read-verify-append critical section."""

    lock_path = ledger_path.parent / (ledger_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            secure_owner_only(lock_path)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class RegistryLedger:
    """Append-only, hash-chained, anchor-verified ledger of runs and holdout events."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._log = AuditLog(path)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def anchor_path(self) -> Path:
        return self._path.parent / (self._path.stem + ".head.json")

    def append(self, kind: str, payload: dict[str, object]) -> AuditRecord:
        """Append a sanitized record and update the head anchor; returns the record."""

        record = self._log.append(kind, payload)
        self._write_anchor(record.sequence + 1, record.record_hash)
        return record

    def _write_anchor(self, count: int, last_hash: str) -> None:
        self.anchor_path.write_text(
            json.dumps({"count": count, "last_hash": last_hash}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        secure_owner_only(self.anchor_path)

    def records(self) -> tuple[AuditRecord, ...]:
        """Every record in order (parsed from the JSONL; empty if the ledger is new)."""

        if not self._path.exists():
            return ()
        out: list[AuditRecord] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            out.append(
                AuditRecord(
                    sequence=int(row["sequence"]),
                    at_utc=str(row["at_utc"]),
                    kind=str(row["kind"]),
                    payload=dict(row["payload"]),
                    previous_hash=str(row["previous_hash"]),
                    record_hash=str(row["record_hash"]),
                )
            )
        return tuple(out)

    def records_of(self, kind: str) -> tuple[AuditRecord, ...]:
        return tuple(record for record in self.records() if record.kind == kind)

    def verify(self) -> tuple[bool, str]:
        """Verify the chain **and** the head anchor; catches truncation/deletion/rollback."""

        ok, detail = verify_chain(self._path)
        if not ok:
            return False, detail
        records = self.records()
        anchor = self._load_anchor()
        if anchor is None:
            if records:
                return False, "head anchor missing (possible deletion of registry.head.json)"
            return True, "empty ledger"
        expected_count = anchor.get("count")
        if not isinstance(expected_count, int) or len(records) != expected_count:
            return False, (
                f"tail truncation/rollback: {len(records)} records "
                f"but anchor expects {expected_count}"
            )
        if not records:
            return False, "head anchor present but ledger is empty (truncation)"
        if records[-1].record_hash != str(anchor.get("last_hash")):
            return False, "head hash mismatch (rollback to an earlier state)"
        return True, f"chain + anchor intact ({len(records)} records)"

    def _load_anchor(self) -> dict[str, object] | None:
        if not self.anchor_path.exists():
            return None
        data: dict[str, object] = json.loads(self.anchor_path.read_text(encoding="utf-8"))
        return data
