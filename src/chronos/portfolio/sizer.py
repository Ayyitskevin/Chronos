"""Proposal-to-intent conversion (Phase 7 portfolio layer).

Sizing rules are deliberately dull:

- whole shares only, floor-rounded;
- notional = min(available cash, equity * proposal fraction * policy cap);
- entries carry the strategy stop translated into an absolute stop price;
- exits sell exactly the held quantity — a strategy cannot leave a residual
  position by proposing a partial exit, and cannot create a short.

The conversion emits an ``OrderIntent`` for the risk engine; it never talks to
a broker and performs no validation that belongs to the risk engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from chronos.domain.enums import OrderSide
from chronos.execution.intents import OrderIntent, TimeInForce
from chronos.strategies.base import ProposalDirection, StrategyProposal


@dataclass(frozen=True, slots=True)
class PortfolioPolicy:
    """Static sizing bounds; the risk engine independently re-checks exposure."""

    max_fraction_per_position: float
    tick_size: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        if not 0.0 < self.max_fraction_per_position <= 1.0:
            raise ValueError("max_fraction_per_position must be in (0, 1]")
        if self.tick_size <= 0:
            raise ValueError("tick_size must be positive")


@dataclass(frozen=True, slots=True)
class ProposalConversion:
    intent: OrderIntent | None
    skipped_reason: str


def _round_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


def convert_proposal(
    proposal: StrategyProposal,
    *,
    policy: PortfolioPolicy,
    equity_usd: Decimal,
    cash_usd: Decimal,
    held_shares: int,
    reference_price: Decimal,
    limit_offset_fraction: Decimal = Decimal("0.001"),
) -> ProposalConversion:
    """Convert one proposal into at most one limit-order intent."""

    if reference_price <= 0:
        return ProposalConversion(intent=None, skipped_reason="reference price is not positive")

    if proposal.direction is ProposalDirection.HOLD:
        return ProposalConversion(intent=None, skipped_reason="proposal is HOLD")

    if proposal.direction is ProposalDirection.EXIT_LONG:
        if held_shares <= 0:
            return ProposalConversion(intent=None, skipped_reason="no position to exit")
        limit = _round_to_tick(
            reference_price * (Decimal(1) - limit_offset_fraction), policy.tick_size
        )
        if limit <= 0:
            return ProposalConversion(intent=None, skipped_reason="exit limit rounded to zero")
        return ProposalConversion(
            intent=OrderIntent(
                strategy_id=proposal.strategy_id,
                strategy_version=proposal.strategy_version,
                symbol=proposal.symbol,
                side=OrderSide.SELL,
                quantity=held_shares,
                limit_price=limit,
                stop_price=None,
                time_in_force=TimeInForce.DAY,
                decision_timestamp_utc=proposal.timestamp_utc,
                source_bar_sequence_id=(
                    proposal.source_bar_ids[-1] if proposal.source_bar_ids else "unknown"
                ),
                proposal_reason=proposal.reason,
            ),
            skipped_reason="",
        )

    # ENTER_LONG
    if held_shares > 0:
        return ProposalConversion(
            intent=None, skipped_reason="already long; pyramiding is not supported"
        )
    capped = min(proposal.desired_exposure_fraction, policy.max_fraction_per_position)
    fraction = Decimal(str(capped))
    budget = min(equity_usd * fraction, cash_usd)
    limit = _round_to_tick(reference_price * (Decimal(1) + limit_offset_fraction), policy.tick_size)
    if limit <= 0:
        return ProposalConversion(intent=None, skipped_reason="entry limit rounded to zero")
    quantity = int(budget / limit)
    if quantity < 1:
        return ProposalConversion(
            intent=None,
            skipped_reason=(f"budget ${budget:.2f} buys no whole share at limit ${limit}"),
        )
    if proposal.stop_fraction is None:
        return ProposalConversion(
            intent=None, skipped_reason="entry proposal lacks a stop fraction"
        )
    stop_multiplier = Decimal(1) - Decimal(str(proposal.stop_fraction))
    stop = _round_to_tick(limit * stop_multiplier, policy.tick_size)
    if stop <= 0 or stop >= limit:
        return ProposalConversion(intent=None, skipped_reason="stop price is not below the limit")
    return ProposalConversion(
        intent=OrderIntent(
            strategy_id=proposal.strategy_id,
            strategy_version=proposal.strategy_version,
            symbol=proposal.symbol,
            side=OrderSide.BUY,
            quantity=quantity,
            limit_price=limit,
            stop_price=stop,
            time_in_force=TimeInForce.DAY,
            decision_timestamp_utc=proposal.timestamp_utc,
            source_bar_sequence_id=(
                proposal.source_bar_ids[-1] if proposal.source_bar_ids else "unknown"
            ),
            proposal_reason=proposal.reason,
        ),
        skipped_reason="",
    )
