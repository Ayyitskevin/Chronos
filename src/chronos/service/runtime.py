"""Service configuration and component wiring.

``ServiceConfig`` is pure data. ``ServiceComponents`` holds the live wired
objects the startup sequence and cycle operate on. ``build_shadow_components``
assembles a SHADOW service over committed CSV data with an all-safe posture;
tests and a future paper wiring construct ``ServiceComponents`` directly with
their own ports.

Account model (honest scope): in SHADOW nothing is ever submitted, so the
account is modelled as flat — configured equity, no positions. That is exact
for SHADOW. PAPER-mode position/equity sourcing from the real broker adapter
is a documented owner-gated follow-on (docs/IBKR_INTEGRATION.md); this build
does not fabricate a paper account.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from chronos.auditlog.log import AuditLog
from chronos.control.halt import HaltStore
from chronos.control.modes import ModeLock, TradingMode, resolve_mode_lock
from chronos.execution.engine import ExecutionEngine
from chronos.execution.ledger import MemoryLedger
from chronos.notifications.notifier import ConsoleNotifier, SafeNotifierFanout
from chronos.portfolio.sizer import PortfolioPolicy
from chronos.risk.engine import RiskEngine
from chronos.risk.policy import RiskPolicy, load_risk_policy
from chronos.service.ports import (
    BrokerEvidenceSource,
    CsvDataSource,
    DataSource,
    NullBrokerEvidence,
    NullExecutionBroker,
    ReconcilableLedger,
    ServiceLedger,
)
from chronos.strategies.base import Strategy


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    mode: TradingMode
    symbols: tuple[str, ...]
    equity_usd: float
    strategy_ids: tuple[str, ...]
    halt_file: Path
    audit_file: Path
    data_dir: Path
    risk_policy_path: Path
    paper_account_allowlist: tuple[str, ...] = ()
    order_transmission_enabled: bool = False


@dataclass
class ServiceComponents:
    mode_lock: ModeLock
    risk_engine: RiskEngine
    engine: ExecutionEngine
    ledger: ReconcilableLedger
    halt_store: HaltStore
    audit_log: AuditLog
    notifier: SafeNotifierFanout
    data_source: DataSource
    evidence_source: BrokerEvidenceSource
    portfolio_policy: PortfolioPolicy
    strategies: dict[str, Strategy]
    symbols: tuple[str, ...]
    equity_usd: float
    explained_position_symbols: frozenset[str] = field(default_factory=frozenset)


def build_shadow_components(
    config: ServiceConfig,
    *,
    strategy_factories: dict[str, Callable[[], Strategy]],
    risk_policy: RiskPolicy | None = None,
    ledger: ServiceLedger | None = None,
) -> ServiceComponents:
    """Wire a SHADOW service over CSV data with a null (no-submit) broker.

    A durable ledger may be supplied (e.g. ``SqliteLedger``) to make a run
    restart-survivable; the default in-memory ledger is fine for SHADOW, where
    nothing is ever submitted so there is no state to persist.
    """

    mode_lock = resolve_mode_lock(
        requested_mode=config.mode,
        paper_account_allowlist=config.paper_account_allowlist,
        broker_reported_account_id=None,
        broker_reported_environment_is_paper=None,
        order_transmission_enabled=config.order_transmission_enabled,
    )
    policy = risk_policy if risk_policy is not None else load_risk_policy(config.risk_policy_path)
    risk_engine = RiskEngine(policy)
    used_ledger: ServiceLedger = ledger if ledger is not None else MemoryLedger()
    engine = ExecutionEngine(
        broker=NullExecutionBroker(),
        risk_engine=risk_engine,
        ledger=used_ledger,
        halt_store=HaltStore(config.halt_file),
        mode_lock=mode_lock,
        reconciliation_passed=False,  # startup sets this only on a clean report
    )
    strategies = {
        name: strategy_factories[name]()
        for name in config.strategy_ids
        if name in strategy_factories
    }
    return ServiceComponents(
        mode_lock=mode_lock,
        risk_engine=risk_engine,
        engine=engine,
        ledger=used_ledger,
        halt_store=engine.halt_store,
        audit_log=AuditLog(config.audit_file),
        notifier=SafeNotifierFanout(notifiers=(ConsoleNotifier(),)),
        data_source=CsvDataSource(data_dir=config.data_dir),
        evidence_source=NullBrokerEvidence(),
        portfolio_policy=PortfolioPolicy(max_fraction_per_position=0.95),
        strategies=strategies,
        symbols=config.symbols,
        equity_usd=config.equity_usd,
    )
