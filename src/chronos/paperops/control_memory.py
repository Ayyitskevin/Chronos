"""Durable paper-control memory rehydrated from the decision ledger.

Duplicate-order fingerprints and cooldown timestamps must survive process
restart. Callers that pass empty ephemeral ``recent_order_fingerprints`` /
``last_order_at_utc`` still get fail-closed behavior when the ledger already
recorded those opens — this module is the restart-safe source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from chronos.paperops.decision import PaperDecisionInput
from chronos.paperops.ledger import DecisionLedgerError, load_and_verify
from chronos.paperops.records import DecisionRecord


@dataclass(frozen=True, slots=True)
class DurableControlMemory:
    """Restart-safe duplicate/cooldown evidence derived from ledger history."""

    recent_order_fingerprints: tuple[str, ...]
    last_order_at_utc: datetime | None
    record_count: int

    def to_payload(self) -> dict[str, object]:
        return {
            "recent_order_fingerprints": list(self.recent_order_fingerprints),
            "last_order_at_utc": (
                self.last_order_at_utc.isoformat() if self.last_order_at_utc else None
            ),
            "record_count": self.record_count,
        }


_DECISION_KINDS = frozenset(
    {
        "candidate_signal",
        "rejection",
        "proposed_order",
        "risk_decision",
        "data_health",
        "control_refusal",
        "paper_fill",
    }
)


def _order_fingerprint_from_record(record: DecisionRecord) -> str | None:
    """Extract the durable control identity written for this decision.

    Preference order:
    1. payload.effective_order_fingerprint / payload.order_fingerprint
    2. decision_inputs.order_fingerprint
    3. record.inputs_fingerprint for decision kinds when explicit fields were
       empty (legacy records written before effective-fp persistence).
    """

    payload = record.payload
    for key in ("effective_order_fingerprint", "order_fingerprint"):
        raw = payload.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    di = payload.get("decision_inputs")
    if isinstance(di, dict):
        raw = di.get("order_fingerprint")
        if raw is not None and str(raw).strip():
            return str(raw).strip()
        raw = di.get("effective_order_fingerprint")
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    # Legacy / empty-fp fallback: evaluate used inputs_fingerprint as the
    # proposed order identity when order_fingerprint was blank.
    if record.kind in _DECISION_KINDS and record.inputs_fingerprint.strip():
        return record.inputs_fingerprint.strip()
    return None


def _parse_record_time(record: DecisionRecord) -> datetime | None:
    try:
        dt = datetime.fromisoformat(record.at_utc)
    except ValueError:
        return None
    return dt


def rehydrate_control_memory(path: Path) -> DurableControlMemory:
    """Load fingerprints and last-order time from a verified decision ledger.

    Fail closed on corrupt/incomplete chains: a restart must not silently
    forget prior opens because the ledger was unreadable.
    """

    ok, detail, records = load_and_verify(path)
    if not ok:
        raise DecisionLedgerError(f"cannot rehydrate control memory from corrupt ledger: {detail}")

    seen: list[str] = []
    last_order: datetime | None = None
    for record in records:
        fp = _order_fingerprint_from_record(record)
        if fp is None:
            continue
        # Every recorded open attempt with a fingerprint is durable evidence —
        # ALLOW or DENY — so a restart cannot re-try the same order as new.
        if fp not in seen:
            seen.append(fp)
        at = _parse_record_time(record)
        if at is not None and (last_order is None or at > last_order):
            last_order = at

    return DurableControlMemory(
        recent_order_fingerprints=tuple(seen),
        last_order_at_utc=last_order,
        record_count=len(records),
    )


def apply_durable_control_memory(
    inp: PaperDecisionInput,
    memory: DurableControlMemory,
) -> PaperDecisionInput:
    """Merge durable ledger memory into ephemeral decision inputs (union)."""

    combined_fps = tuple(
        dict.fromkeys(list(memory.recent_order_fingerprints) + list(inp.recent_order_fingerprints))
    )
    last = memory.last_order_at_utc
    if inp.last_order_at_utc:
        try:
            candidate = datetime.fromisoformat(inp.last_order_at_utc)
        except ValueError:
            candidate = None
        if candidate is not None and (last is None or candidate > last):
            last = candidate
    last_iso = last.isoformat() if last is not None else inp.last_order_at_utc
    return replace(
        inp,
        recent_order_fingerprints=combined_fps,
        last_order_at_utc=last_iso,
    )
