"""Research-plane helpers, loaded lazily to preserve import isolation."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from chronos.research.repro import (
        SCHEMA_VERSION,
        CompareReason,
        CompareReport,
        CompareStatus,
        compare_manifests,
        load_manifest,
        produce_named_backtest_run,
        replay_from_manifest,
    )

__all__ = [
    "SCHEMA_VERSION",
    "CompareReason",
    "CompareReport",
    "CompareStatus",
    "compare_manifests",
    "load_manifest",
    "produce_named_backtest_run",
    "replay_from_manifest",
]


def __getattr__(name: str) -> Any:
    """Resolve legacy package exports without importing the runner eagerly."""

    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module("chronos.research.repro"), name)
    globals()[name] = value
    return value
