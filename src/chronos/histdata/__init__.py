"""Historical-data plane (AI Quant plan C1, ADR-0011).

A separate, read-only data process that ingests IBKR historical bars into a
file-based store of **unadjusted** as-traded bars plus a corporate-action event
stream, deriving adjusted / total-return views at read time. It is structurally
isolated from the trading plane: it never holds the writer lease and never imports
the order/broker/persistence modules (proven by AST + subprocess import tests).

It also captures forward option-chain / IV / greeks snapshots (C0, ADR-0012) into
the same store with staleness recorded per row.
"""

from chronos.histdata.adjust import (
    AdjustmentError,
    AdjustmentResult,
    AdjustmentView,
    adjust_series,
)
from chronos.histdata.client import HistoricalDataClient, HistoricalDataError
from chronos.histdata.corporate_actions import ActionKind, CorporateAction
from chronos.histdata.holdout import (
    HoldoutWindow,
    embargoed_view,
    load_holdouts,
    read_embargoed_bars,
)
from chronos.histdata.options import (
    OptionChainSnapshot,
    OptionQuoteRow,
    OptionRight,
    SnapshotQuality,
)
from chronos.histdata.options_capture import CaptureOutcome, capture_snapshot, capture_symbols
from chronos.histdata.options_client import (
    ChainParams,
    ContractKey,
    OptionSnapshotClient,
    OptionSnapshotError,
)
from chronos.marketdata.pacing import PacingController

__all__ = [
    "ActionKind",
    "AdjustmentError",
    "AdjustmentResult",
    "AdjustmentView",
    "CaptureOutcome",
    "ChainParams",
    "ContractKey",
    "CorporateAction",
    "HistoricalDataClient",
    "HistoricalDataError",
    "HoldoutWindow",
    "OptionChainSnapshot",
    "OptionQuoteRow",
    "OptionRight",
    "OptionSnapshotClient",
    "OptionSnapshotError",
    "PacingController",
    "SnapshotQuality",
    "adjust_series",
    "capture_snapshot",
    "capture_symbols",
    "embargoed_view",
    "load_holdouts",
    "read_embargoed_bars",
]
