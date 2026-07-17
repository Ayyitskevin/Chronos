"""Long-running shadow/paper service plane (Phase 11 SHADOW/PAPER).

This package wires the deterministic decision path (strategy -> portfolio ->
risk -> execution) into a supervised loop with a correct startup sequence:
resolve the mode lock, read the persistent halt, open durable storage,
**hydrate in-flight orders from the ledger** (R-23), gather broker evidence,
reconcile at the state level (R-22), and only then permit order generation.

In SHADOW the mode lock is ``NO_ORDERS``: the loop produces and audits
would-be intents and risk decisions but the execution engine structurally
refuses submission. PAPER submission is wired through the identical
``ExecutionEngine.submit_approved`` gate and only becomes possible under the
six-condition paper lock; it additionally requires the owner-gated real
broker adapter (see docs/IBKR_INTEGRATION.md). Nothing here can reach a live
account.
"""

from chronos.service.cycle import CycleReport, ServiceCycle, SymbolDecision
from chronos.service.ports import (
    BrokerEvidence,
    BrokerEvidenceSource,
    CsvDataSource,
    DataSource,
    NullBrokerEvidence,
    NullExecutionBroker,
    ReconcilableLedger,
    ReplayDataSource,
)
from chronos.service.runtime import ServiceComponents, ServiceConfig, build_shadow_components
from chronos.service.startup import StartupReport, run_startup

__all__ = [
    "BrokerEvidence",
    "BrokerEvidenceSource",
    "CsvDataSource",
    "CycleReport",
    "DataSource",
    "NullBrokerEvidence",
    "NullExecutionBroker",
    "ReconcilableLedger",
    "ReplayDataSource",
    "ServiceComponents",
    "ServiceConfig",
    "ServiceCycle",
    "StartupReport",
    "SymbolDecision",
    "build_shadow_components",
    "run_startup",
]
