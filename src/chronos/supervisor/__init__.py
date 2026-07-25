"""The deterministic supervisor: the only path from a model decision to an order.

ADR-0016 §2. This package is the ModelDecisionGateway. It is the sole consumer of
:class:`~chronos.autonomy.decision.AITradeDecision`, and it sits **between** the
model plane and the existing order pipeline:

    model worker  ->  decision queue  ->  [ supervisor ]  ->  chronos.orders
                                              |                     |
                                   admission + sizing         unchanged gates,
                                   (deterministic, pure)      single transmit site

What it does: validates the mandate in force, admits or refuses the decision,
and independently derives the executable quantity. What it deliberately does
**not** do: transmit. Nothing here talks to a broker. An admitted, sized
decision becomes a proposal handed to the existing
:class:`~chronos.orders.service.OrderManagementService`, which then applies every
gate it already applied to a human-proposed order — risk engine, preview,
confirmation-or-mandate, the ten-gate live stack, kill switch, drawdown breaker,
writer lease — and owns the single ``transmit=True`` site.

So autonomy adds a gate; it removes none. The supervisor can only ever *narrow*
what reaches the broker relative to a human proposing the same trade.

Both halves are pure functions over explicit evidence, which is what makes the
kernel's veto testable without a broker, a database, or a model.
"""

from __future__ import annotations

from chronos.supervisor.admission import (
    MAX_RESUBMISSIONS,
    AdmissionCheck,
    AdmissionOutcome,
    AdmissionRefusal,
    MandateActivation,
    MarketDataEvidence,
    SupervisorState,
    admit,
)
from chronos.supervisor.sizing import AccountEvidence, SizingOutcome, size_order

__all__ = [
    "MAX_RESUBMISSIONS",
    "AccountEvidence",
    "AdmissionCheck",
    "AdmissionOutcome",
    "AdmissionRefusal",
    "MandateActivation",
    "MarketDataEvidence",
    "SizingOutcome",
    "SupervisorState",
    "admit",
    "size_order",
]
