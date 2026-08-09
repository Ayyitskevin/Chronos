"""Experiment registry + holdout guardian (AI Quant plan C2, ADR-0013).

A research-plane, tamper-evident ledger (built on the hash-chained
:class:`chronos.auditlog.AuditLog`) that records every research run and every holdout
event. The ledger is the single source of truth for the multiple-testing trial count
and for which holdout windows are burned. The holdout guardian mediates every unmasking
read behind an owner-typed, single-use, logged unlock, so the M5 "burned holdout"
failure is structurally impossible.

Exports are loaded lazily so importing ledger-only research code does not also grant or
load the holdout-unlock capability. The package remains import-isolated from the trading
plane and structurally unreachable from automated paths.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from chronos.registry.budget import accrued_capture_sessions, available_budget
    from chronos.registry.holdout_guardian import (
        REQUIRED_HOLDOUT_UNLOCK_PHRASE,
        HoldoutGuardianError,
        UnlockGrant,
        burned_windows,
        is_burned,
        mediated_holdout_read,
        request_unlock,
    )
    from chronos.registry.ledger import KIND_CONSUME, KIND_RUN, KIND_UNLOCK, RegistryLedger
    from chronos.registry.runs import (
        RunStage,
        current_commit,
        data_fingerprint,
        register_run,
        trial_count,
    )

_EXPORT_MODULE = {
    "KIND_CONSUME": "chronos.registry.ledger",
    "KIND_RUN": "chronos.registry.ledger",
    "KIND_UNLOCK": "chronos.registry.ledger",
    "REQUIRED_HOLDOUT_UNLOCK_PHRASE": "chronos.registry.holdout_guardian",
    "HoldoutGuardianError": "chronos.registry.holdout_guardian",
    "RegistryLedger": "chronos.registry.ledger",
    "RunStage": "chronos.registry.runs",
    "UnlockGrant": "chronos.registry.holdout_guardian",
    "accrued_capture_sessions": "chronos.registry.budget",
    "available_budget": "chronos.registry.budget",
    "burned_windows": "chronos.registry.holdout_guardian",
    "current_commit": "chronos.registry.runs",
    "data_fingerprint": "chronos.registry.runs",
    "is_burned": "chronos.registry.holdout_guardian",
    "mediated_holdout_read": "chronos.registry.holdout_guardian",
    "register_run": "chronos.registry.runs",
    "request_unlock": "chronos.registry.holdout_guardian",
    "trial_count": "chronos.registry.runs",
}

__all__ = [
    "KIND_CONSUME",
    "KIND_RUN",
    "KIND_UNLOCK",
    "REQUIRED_HOLDOUT_UNLOCK_PHRASE",
    "HoldoutGuardianError",
    "RegistryLedger",
    "RunStage",
    "UnlockGrant",
    "accrued_capture_sessions",
    "available_budget",
    "burned_windows",
    "current_commit",
    "data_fingerprint",
    "is_burned",
    "mediated_holdout_read",
    "register_run",
    "request_unlock",
    "trial_count",
]


def __getattr__(name: str) -> Any:
    """Load only the registry capability explicitly requested by the caller."""

    module_name = _EXPORT_MODULE.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
