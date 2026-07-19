"""Historical-data plane (AI Quant plan C1, ADR-0011).

A separate, read-only data process that ingests IBKR historical bars into a
file-based store of **unadjusted** as-traded bars plus a corporate-action event
stream, deriving adjusted / total-return views at read time. It is structurally
isolated from the trading plane: it never holds the writer lease and never imports
the order/broker/persistence modules (proven by AST + subprocess import tests).

C1-b (this commit) delivers the pure core — the corporate-action model and the
read-time adjustment. Later sub-milestones add the fetch client, pacing, the
runnable process + store, and the holdout embargo.
"""

from chronos.histdata.adjust import (
    AdjustmentError,
    AdjustmentResult,
    AdjustmentView,
    adjust_series,
)
from chronos.histdata.corporate_actions import ActionKind, CorporateAction

__all__ = [
    "ActionKind",
    "AdjustmentError",
    "AdjustmentResult",
    "AdjustmentView",
    "CorporateAction",
    "adjust_series",
]
