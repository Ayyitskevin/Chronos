"""Experiment registry + holdout guardian (AI Quant plan C2, ADR-0013).

A research-plane, tamper-evident ledger (built on the hash-chained
:class:`chronos.auditlog.AuditLog`) that records every research run and every holdout
event. The ledger is the single source of truth for the multiple-testing trial count
and for which holdout windows are burned. The holdout guardian mediates every unmasking
read behind an owner-typed, single-use, logged unlock, so the M5 "burned holdout"
failure is structurally impossible.

Import-isolated from the trading plane and structurally unreachable from any automated
path (see ``tests/safety/test_registry_isolation.py`` and
``tests/safety/test_registry_no_automated_unlock.py``).
"""

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
