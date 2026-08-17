"""Experiment-run recording + honest data fingerprints + trial counting (ADR-0013 §2/§5).

A research run is recorded in the ledger with its config/data/criteria/commit
provenance; the multiple-testing trial count is then **derived** from the recorded runs.

Honesty (C2 review F3/F5/F7): the derivation is honest, but its *completeness* depends on
every data-touching run actually being registered with ``touched_data=True`` — which is
enforced structurally only once the runner auto-registers (a follow-on). ``register_run``
fails closed on null provenance (an "unknown" commit or an empty criteria reference is
refused rather than silently recorded). ``data_fingerprint`` attests the *manifest's*
recorded hash for each file, and distinguishes "no corporate-actions captured" from "no
dividends" via an explicit ``actions_captured`` flag — it does not recompute file bytes.
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
from chronos.registry.ledger import (
    KIND_RUN,
    RegistryLedger,
    verified_registry_records,
    verified_registry_transaction,
)

_NULL_COMMITS = frozenset({"", "unknown"})


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


def data_fingerprint(history_root: Path, symbols: Iterable[str]) -> dict[str, dict[str, object]]:
    """Per-symbol provenance from the histdata MANIFEST at ``history_root``.

    Returns ``{symbol: {bars_sha, actions_sha, actions_captured}}``. ``actions_captured``
    is ``False`` when no corporate-actions file was ever captured for the symbol (so a
    ``None`` ``actions_sha`` is not mistaken for "this instrument has no dividends").
    """

    path = manifest_path(history_root)
    manifest: dict[str, object] = {}
    if path.exists():
        manifest = json.loads(path.read_text(encoding="utf-8"))
    by_symbol = manifest.get("symbols", {})
    entries = by_symbol if isinstance(by_symbol, dict) else {}
    out: dict[str, dict[str, object]] = {}
    for symbol in symbols:
        entry = entries.get(symbol, {})
        entry = entry if isinstance(entry, dict) else {}
        actions = entry.get("corporate_actions")
        out[symbol] = {
            "bars_sha": _sha_of(entry.get("bars")),
            "actions_sha": _sha_of(actions),
            "actions_captured": isinstance(actions, dict),
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
    """Record one research run; fails closed on null provenance."""

    if code_commit.strip() in _NULL_COMMITS:
        raise ValueError("register_run requires a real code_commit (got empty/'unknown')")
    if not criteria_ref.strip():
        raise ValueError("register_run requires a non-empty criteria_ref")
    if not strategy_id.strip():
        raise ValueError("register_run requires a strategy_id")
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
    # The supplied object may have cached a head before another writer appended.
    # Recover a fresh writer under the shared lock and verify before/after mutation.
    with verified_registry_transaction(ledger._path_capability) as fresh:
        if any(
            record.kind == KIND_RUN
            and record.payload.get("experiment_id") == payload["experiment_id"]
            for record in fresh.records()
        ):
            raise ValueError(
                f"experiment_id {payload['experiment_id']!r} is already registered; "
                "every data-touching run requires a unique identity"
            )
        return fresh.append(KIND_RUN, payload)


def trial_count(ledger: RegistryLedger, *, strategy_id: str | None = None) -> int:
    """The multiple-testing N: data-touching runs recorded in the ledger (optionally scoped).

    Derived from the ledger, not a self-reported field — but honest only insofar as every
    data-touching run is registered (see module docstring).
    """

    count = 0
    for record in verified_registry_records(ledger._path_capability):
        if record.kind != KIND_RUN:
            continue
        if not record.payload.get("touched_data", False):
            continue
        if strategy_id is not None and record.payload.get("strategy_id") != strategy_id:
            continue
        count += 1
    return count
