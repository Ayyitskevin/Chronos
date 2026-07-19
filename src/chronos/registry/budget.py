"""Holdout-unlock budget policy (ADR-0013 §6).

Rations holdout consumption against **newly accrued data**: one unlock credit is
earned per ``sessions_per_unlock`` accrued capture sessions, minus the unlocks already
spent (from the ledger), capped at ``max_outstanding_unlocks``. With no accrued data
the budget is zero and unlocks fail closed — the default-closed posture for a store
that ships empty.

This is a first cut (linear accrual credits); it rations, it does not model statistical
power — that lands with C3/C4.
"""

from __future__ import annotations

import json
from pathlib import Path

from chronos.histdata.store import manifest_path
from chronos.registry.ledger import KIND_UNLOCK, RegistryLedger


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


def available_budget(
    ledger: RegistryLedger,
    *,
    accrued_sessions: int,
    sessions_per_unlock: int,
    max_outstanding_unlocks: int,
) -> int:
    """Unlock credits currently available (fail-closed at zero)."""

    earned = accrued_sessions // sessions_per_unlock if sessions_per_unlock > 0 else 0
    spent = len(ledger.records_of(KIND_UNLOCK))
    return max(0, min(max_outstanding_unlocks, earned - spent))
