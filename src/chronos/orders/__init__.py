"""Human-in-the-loop order-management pipeline (M5 paper; M7 live branch).

This package is the live-capable order path: intent construction, the
structured risk engine, what-if preview, the single submission boundary with
its paper and live branches (ADR-0009), lifecycle tracking,
buy-to-close/modify/cancel, and restart / SUBMISSION_UNKNOWN recovery. It
deliberately imports NOTHING from the autonomous ``chronos.execution`` /
``chronos.risk`` packages, which remain live-incapable and unchanged. The only
place ``transmit=True`` is ever assigned is inside
:class:`chronos.orders.submission.OrderSubmissionBoundary` — one line serving
both branches, each behind its own fail-closed gate chain.
"""

from __future__ import annotations

from chronos.orders.intent import (
    WheelOrderIntent,
    build_option_intent,
    build_stock_intent,
    order_summary_hash,
)
from chronos.orders.mutations import (
    OrderCancellationService,
    OrderModificationService,
    build_buy_to_close_intent,
)
from chronos.orders.preview import OrderPreviewResult, OrderPreviewService
from chronos.orders.reconciliation_recovery import OrderRestartReconciler
from chronos.orders.risk import (
    OrderRiskCheck,
    OrderRiskDecision,
    OrderRiskEngine,
    RiskEvidence,
)
from chronos.orders.service import OrderManagementService, ProposeResult
from chronos.orders.state_machine import (
    OrderLifecycleError,
    OrderLifecycleMachine,
)
from chronos.orders.submission import (
    OrderSubmissionBoundary,
    SubmissionOutcome,
    SubmissionRefusalCode,
)
from chronos.orders.tracker import OrderStatusUpdate, OrderTracker

__all__ = [
    "OrderCancellationService",
    "OrderLifecycleError",
    "OrderLifecycleMachine",
    "OrderManagementService",
    "OrderModificationService",
    "OrderPreviewResult",
    "OrderPreviewService",
    "OrderRestartReconciler",
    "OrderRiskCheck",
    "OrderRiskDecision",
    "OrderRiskEngine",
    "OrderStatusUpdate",
    "OrderSubmissionBoundary",
    "OrderTracker",
    "ProposeResult",
    "RiskEvidence",
    "SubmissionOutcome",
    "SubmissionRefusalCode",
    "WheelOrderIntent",
    "build_buy_to_close_intent",
    "build_option_intent",
    "build_stock_intent",
    "order_summary_hash",
]
