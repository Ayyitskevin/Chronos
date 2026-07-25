"""Contracts for controlled autonomous model authority (ADR-0016, D-16).

This package is **contracts only**: the typed
:class:`~chronos.autonomy.decision.AITradeDecision` a model may originate, and
the owner-authored :class:`~chronos.autonomy.mandate.AutonomyMandate` that
bounds it. There is no admission, no evaluation, no sizing, no scheduling, and
no broker behavior here — the deterministic ModelDecisionGateway and supervisor
land in Milestone 2, and until they do nothing in this package is wired into any
runtime path.

Structural guarantees, each asserted by a test in ``tests/safety/``:

- this package imports nothing from ``chronos.orders``, ``chronos.broker``,
  ``chronos.execution``, ``chronos.risk``, ``chronos.api``, or
  ``chronos.persistence`` — the model plane cannot reach a broker, a policy
  writer, or the submission path, even transitively;
- ``AITradeDecision`` carries no account, broker, routing, or transmit field;
- ``AutonomyMandate`` is frozen, must expire, and authorizes nothing by default.
"""

from __future__ import annotations

from chronos.autonomy.decision import (
    AITradeDecision,
    DecisionProvenance,
    EntryPlan,
    EvidenceCitation,
    ExitPlan,
    PriceTrigger,
)
from chronos.autonomy.enums import (
    DEFAULT_AUTONOMY_MODE,
    EXPOSURE_CREATING_DECISION_KINDS,
    LIVE_AUTONOMY_MODES,
    MINIMUM_PROMOTION_FOR_MODE,
    NON_SUBMITTING_AUTONOMY_MODES,
    PROMOTION_LADDER,
    RISK_REDUCING_DECISION_KINDS,
    SUBMITTING_AUTONOMY_MODES,
    TARGETED_DECISION_KINDS,
    AutonomyMode,
    DecisionDirection,
    DecisionKind,
    OrderForm,
    PriceReference,
    PromotionLevel,
    RestartBehavior,
    StrategyForm,
    TimeHorizon,
    TradableAssetClass,
    TradingSession,
    TriggerComparator,
    promotion_rank,
)
from chronos.autonomy.mandate import (
    MAX_LIVE_MANDATE_DURATION,
    ActivityLimits,
    AutonomyMandate,
    CapitalLimits,
    ConcentrationLimits,
    InstrumentScope,
    LossLimits,
    MarketDataRequirements,
    SessionPolicy,
    VersionPins,
)

__all__ = [
    "DEFAULT_AUTONOMY_MODE",
    "EXPOSURE_CREATING_DECISION_KINDS",
    "LIVE_AUTONOMY_MODES",
    "MAX_LIVE_MANDATE_DURATION",
    "MINIMUM_PROMOTION_FOR_MODE",
    "NON_SUBMITTING_AUTONOMY_MODES",
    "PROMOTION_LADDER",
    "RISK_REDUCING_DECISION_KINDS",
    "SUBMITTING_AUTONOMY_MODES",
    "TARGETED_DECISION_KINDS",
    "AITradeDecision",
    "ActivityLimits",
    "AutonomyMandate",
    "AutonomyMode",
    "CapitalLimits",
    "ConcentrationLimits",
    "DecisionDirection",
    "DecisionKind",
    "DecisionProvenance",
    "EntryPlan",
    "EvidenceCitation",
    "ExitPlan",
    "InstrumentScope",
    "LossLimits",
    "MarketDataRequirements",
    "OrderForm",
    "PriceReference",
    "PriceTrigger",
    "PromotionLevel",
    "RestartBehavior",
    "SessionPolicy",
    "StrategyForm",
    "TimeHorizon",
    "TradableAssetClass",
    "TradingSession",
    "TriggerComparator",
    "VersionPins",
    "promotion_rank",
]
