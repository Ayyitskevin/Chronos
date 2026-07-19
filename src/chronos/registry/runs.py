"""Experiment-run recording + honest data fingerprints + trial counting (ADR-0013 §2/§5).

A research run is recorded in the ledger with its config/data/criteria/commit
provenance; the multiple-testing trial count is then **derived** from the ledger
(every data-touching run counts) — never a self-reported number.

The data fingerprint is honest about *which* store a run used: it hashes the C1
histdata **bars + corporate-actions** pair per symbol (the dual hash D-14 pins), not
the legacy single-CSV sha; a run over the legacy raw corpus records that under a
separate ``legacy_raw`` key so the two provenance regimes are never conflated.
"""

from __future__ import annotations

import json
import secrets
import subprocess
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path

from chronos.auditlog.log import AuditRecord
from chronos.histdata.store import manifest_path
from chronos.registry.ledger import KIND_RUN, RegistryLedger


class RunStage(StrEnum):
    DEV = "dev"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


def current_commit(root: Path) -> str:
    """``git rev-parse HEAD`` for provenance; ``"unknown"`` on any failure."""

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def data_fingerprint(root: Path, symbols: Iterable[str]) -> dict[str, dict[str, str | None]]:
    """Per-symbol {bars_sha, actions_sha} from the histdata MANIFEST (None if absent)."""

    path = manifest_path(root / "research/data/history")
    manifest: dict[str, object] = {}
    if path.exists():
        manifest = json.loads(path.read_text(encoding="utf-8"))
    by_symbol = manifest.get("symbols", {})
    entries = by_symbol if isinstance(by_symbol, dict) else {}
    out: dict[str, dict[str, str | None]] = {}
    for symbol in symbols:
        entry = entries.get(symbol, {})
        entry = entry if isinstance(entry, dict) else {}
        out[symbol] = {
            "bars_sha": _sha_of(entry.get("bars")),
            "actions_sha": _sha_of(entry.get("corporate_actions")),
        }
    return out


def _sha_of(section: object) -> str | None:
    if isinstance(section, dict):
        value = section.get("sha256")
        return str(value) if value is not None else None
    return None


def register_run(
    ledger: RegistryLedger,
    *,
    stage: RunStage,
    strategy_id: str,
    config_hash: str,
    code_commit: str,
    data_hashes: dict[str, object],
    criteria_ref: str,
    touched_data: bool = True,
    experiment_id: str | None = None,
) -> AuditRecord:
    """Record one research run; returns the chained ledger record."""

    payload: dict[str, object] = {
        "experiment_id": experiment_id or secrets.token_hex(8),
        "stage": stage.value,
        "strategy_id": strategy_id,
        "config_hash": config_hash,
        "code_commit": code_commit,
        "data_hashes": data_hashes,
        "criteria_ref": criteria_ref,
        "touched_data": touched_data,
    }
    return ledger.append(KIND_RUN, payload)


def trial_count(ledger: RegistryLedger, *, strategy_id: str | None = None) -> int:
    """The multiple-testing N: data-touching runs in the ledger (optionally scoped)."""

    count = 0
    for record in ledger.records_of(KIND_RUN):
        if not record.payload.get("touched_data", False):
            continue
        if strategy_id is not None and record.payload.get("strategy_id") != strategy_id:
            continue
        count += 1
    return count
