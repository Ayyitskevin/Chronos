"""Execution-plane broker adapters.

``simulated`` is the deterministic in-process broker used by backtests,
replay, and the end-to-end and chaos test suites. ``ibkr_paper`` adapts the
same port onto the TWS API via ``ib_async`` for a verified paper account.
Nothing in this package can be constructed for a live account: the paper
adapter re-verifies account identity on connect and the mode lock refuses
live-capable modes at resolution time.
"""

from chronos.execution.brokers.port import (
    BrokerEvent,
    BrokerEventKind,
    ExecutionBrokerPort,
    SubmissionReceipt,
)

__all__ = [
    "BrokerEvent",
    "BrokerEventKind",
    "ExecutionBrokerPort",
    "SubmissionReceipt",
]
