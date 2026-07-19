"""Holdout-unlock budget policy (ADR-0013 §6, C2 review remediation F4).

Rations holdout consumption against **newly accrued data**: one credit is earned per
``sessions_per_unlock`` accrued capture sessions, minus the credits currently spent —
where "spent" is **actual burns (consumes) plus still-active (unexpired, unconsumed)
grants**, capped at ``max_outstanding_unlocks``. An *expired, unused* grant releases its
credit (a fat-fingered unlock is refunded once it expires); only a burned window spends
a credit permanently. With no accrued data the budget is zero and unlocks fail closed.

This is a first cut (linear accrual credits); it rations, it does not model statistical
power — that lands with C3/C4.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from chronos.histdata.store import manifest_path
from chronos.registry.ledger import KIND_CONSUME, KIND_UNLOCK, RegistryLedger


def accrued_capture_sessions(history_root: Path) -> int:
    """A concrete proxy for newly accrued data: total option-snapshot dates captured."""

    path = manifest_path(history_root)
    if not path.exists():
        return 0
    manifest = json.loads(path.read_text(encoding="utf-8"))
    symbols = manifest.get("symbols", {})
    if not isinstance(symbols, dict):
        return 0
    total = 0
    for entry in symbols.values():
        options = entry.get("options") if isinstance(entry, dict) else None
        snapshots = options.get("snapshots", {}) if isinstance(options, dict) else {}
        total += len(snapshots)
    return total


def spent_credits(ledger: RegistryLedger, now: datetime) -> int:
    """Burned windows + still-active grants; expired unused grants are refunded."""

    consumed_ids = {str(r.payload.get("unlock_id")) for r in ledger.records_of(KIND_CONSUME)}
    active = 0
    for record in ledger.records_of(KIND_UNLOCK):
        if str(record.payload.get("unlock_id")) in consumed_ids:
            continue
        expires_at = _parse(record.payload.get("expires_at"))
        if expires_at is not None and now < expires_at:
            active += 1
    return len(consumed_ids) + active


def available_budget(
    ledger: RegistryLedger,
    *,
    now: datetime,
    accrued_sessions: int,
    sessions_per_unlock: int,
    max_outstanding_unlocks: int,
) -> int:
    """Unlock credits currently available (fail-closed at zero)."""

    earned = accrued_sessions // sessions_per_unlock if sessions_per_unlock > 0 else 0
    return max(0, min(max_outstanding_unlocks, earned - spent_credits(ledger, now)))


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
