"""The deterministic supervisor: the only path from a model decision to an order.

ADR-0016 §2. This package is the ModelDecisionGateway. It is the sole consumer of
:class:`~chronos.autonomy.decision.AITradeDecision`, and it sits **between** the
model plane and the existing order pipeline:

    model worker  ->  decision queue  ->  [ supervisor ]  ->  chronos.orders
                                              |                     |
                                   admission + sizing         unchanged gates,
                                   (deterministic, pure)      single transmit site

What it does **today**: validates the mandate in force, admits or refuses the
decision, and independently derives the executable quantity. What it deliberately
does not do: transmit. Nothing here talks to a broker.

**Not yet built (read this before assuming the arrow above is code).** The right
half of that diagram is the design, not the current wiring. No module converts an
admitted, sized decision into a
:class:`~chronos.orders.intents.WheelOrderIntent`, and nothing calls
:class:`~chronos.orders.service.OrderManagementService` from here — the M2 review
found this docstring asserting the handoff in the present tense. Compilation is
M4's deliverable and it is the step that must resolve and qualify a contract,
select a permitted order form, and validate tick, lot, multiplier, currency and
exchange. Until it exists, ``admit()`` and ``size_order()`` are a gate nothing
has yet been routed through.

The design intent, when that lands: an admitted, sized decision becomes a
proposal handed to ``OrderManagementService``, which applies every gate it
already applies to a human-proposed order — risk engine, preview, the ten-gate
live stack, kill switch, drawdown breaker, writer lease — and owns the single
``transmit=True`` site. So autonomy adds a gate; it removes none. The supervisor
can only ever *narrow* what reaches the broker relative to a human proposing the
same trade.

Both halves are pure functions over explicit evidence, which is what makes the
kernel's veto testable without a broker, a database, or a model.
"""

from __future__ import annotations

from chronos.supervisor.admission import (
    MAX_RESUBMISSIONS,
    AdmissionCheck,
    AdmissionOutcome,
    AdmissionRefusal,
    DegradedReason,
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
    "DegradedReason",
    "MandateActivation",
    "MarketDataEvidence",
    "SizingOutcome",
    "SupervisorState",
    "admit",
    "size_order",
]
