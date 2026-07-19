"""The experiment-registry ledger (ADR-0013 §1).

A tamper-evident, append-only record of every research run and every holdout event,
built on the platform's hash-chained :class:`chronos.auditlog.AuditLog` (sequence +
per-record SHA-256 linked to the prior, ``fsync``'d, owner-only). The ledger is the
**single source of truth** for the multiple-testing trial count and for which holdout
windows are burned — never an in-memory or self-reported value.

It lives at ``research/registry/registry.jsonl``, separate from the trading plane's
``data/platform_audit.jsonl``. This module opens no trading database and imports no
order/broker module.
"""

from __future__ import annotations

import json
from pathlib import Path

from chronos.auditlog.log import AuditLog, AuditRecord, verify_chain

# Record kinds (the ledger's controlled vocabulary).
KIND_RUN = "experiment_run"
KIND_UNLOCK = "holdout_unlock"
KIND_CONSUME = "holdout_consume"


class RegistryLedger:
    """Append-only, hash-chained ledger of runs and holdout events."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._log = AuditLog(path)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, kind: str, payload: dict[str, object]) -> AuditRecord:
        """Append a sanitized record; returns the chained record."""

        return self._log.append(kind, payload)

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
        """Re-derive the chain; ``(ok, detail)`` — detects any edit/reorder/truncation."""

        return verify_chain(self._path)
