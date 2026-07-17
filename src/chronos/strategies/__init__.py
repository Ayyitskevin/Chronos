"""Deterministic strategy framework (ADR-0004).

Strategies see bars and their own position state; they emit
``StrategyProposal`` objects. They cannot import broker adapters, mutate risk
policy, or size orders in shares — sizing belongs to the portfolio layer and
validation to the independent risk engine.
"""

from chronos.strategies.base import (
    PositionState,
    ProposalDirection,
    Strategy,
    StrategyContext,
    StrategyProposal,
)

__all__ = [
    "PositionState",
    "ProposalDirection",
    "Strategy",
    "StrategyContext",
    "StrategyProposal",
]
