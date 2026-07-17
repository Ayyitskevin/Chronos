"""Service-plane ports: market-data source and broker evidence source.

These are the seams that keep the service loop deterministic and testable
without a network or credentials. Backtest/replay and the tests use the
in-memory implementations here; a real shadow/paper run supplies a live data
adapter and the IBKR paper adapter's evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from chronos.execution.brokers.port import BrokerEvent, SubmissionReceipt
from chronos.execution.engine import LedgerPort
from chronos.execution.intents import IntentStatus, OrderIntent
from chronos.execution.reconciliation import OrderStateView
from chronos.marketdata.bars import BarInterval, BarSeries
from chronos.marketdata.csv_provider import load_daily_csv


@runtime_checkable
class DataSource(Protocol):
    """Supplies closed daily bars for a symbol (oldest first, closed only)."""

    def closed_bars(self, symbol: str) -> BarSeries: ...


@runtime_checkable
class ReconcilableLedger(Protocol):
    """A ledger that can report its working orders for hydration/reconcile."""

    def working_intent_ids(self) -> tuple[str, ...]: ...

    def working_order_snapshots(self) -> dict[str, tuple[IntentStatus, int]]: ...


@runtime_checkable
class ServiceLedger(LedgerPort, ReconcilableLedger, Protocol):
    """A ledger usable by the service: durable write-through plus the
    reconciliation/hydration reads. ``MemoryLedger`` and ``SqliteLedger``
    both satisfy it."""


@dataclass
class NullExecutionBroker:
    """Execution broker for SHADOW: submission is impossible by construction.

    The mode lock already refuses submission in SHADOW before the engine ever
    reaches ``broker.submit``; this adapter makes the guarantee physical — a
    submission that somehow got this far raises, and there are never any
    events to drain.
    """

    def submit(self, intent: OrderIntent) -> SubmissionReceipt:
        raise AssertionError(
            "NullExecutionBroker.submit called — SHADOW must never submit an order"
        )

    def request_cancel(self, intent_id: str) -> None:
        return None

    def drain_events(self) -> tuple[BrokerEvent, ...]:
        return ()


@dataclass(frozen=True, slots=True)
class BrokerEvidence:
    """A point-in-time snapshot of broker state for reconciliation."""

    connected: bool
    open_order_refs: tuple[str, ...]
    positions: dict[str, int]
    order_states: dict[str, OrderStateView]


@runtime_checkable
class BrokerEvidenceSource(Protocol):
    """Gathers broker-reported state for the reconciliation gate."""

    def gather(self) -> BrokerEvidence: ...


@dataclass(frozen=True, slots=True)
class NullBrokerEvidence:
    """No broker (SHADOW): connected, nothing open, no positions.

    This reconciles clean against an empty ledger, which is exactly the
    correct SHADOW precondition — there is no broker and nothing to contradict.
    """

    def gather(self) -> BrokerEvidence:
        return BrokerEvidence(connected=True, open_order_refs=(), positions={}, order_states={})


@dataclass
class ReplayDataSource:
    """Deterministic in-memory data source for replay/tests."""

    series_by_symbol: dict[str, BarSeries] = field(default_factory=dict)

    def add(self, series: BarSeries) -> None:
        self.series_by_symbol[series.symbol] = series

    def closed_bars(self, symbol: str) -> BarSeries:
        existing = self.series_by_symbol.get(symbol)
        if existing is not None:
            return existing
        return BarSeries(symbol=symbol, interval=BarInterval.DAY_1, bars=())


@dataclass
class CsvDataSource:
    """Reads committed daily CSVs from a research data directory."""

    data_dir: Path
    source_label: str = "research_raw"

    def closed_bars(self, symbol: str) -> BarSeries:
        path = self.data_dir / f"{symbol.upper()}.csv"
        if not path.exists():
            return BarSeries(symbol=symbol.upper(), interval=BarInterval.DAY_1, bars=())
        return load_daily_csv(path, symbol=symbol.upper(), source=self.source_label).series
