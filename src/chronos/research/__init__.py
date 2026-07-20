"""Research-plane helpers: named backtest runner and validation harness."""

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
