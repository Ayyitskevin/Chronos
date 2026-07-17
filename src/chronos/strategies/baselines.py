"""Baseline strategies for Phase 6 comparisons.

These exist so candidate strategies must beat something dumb and honest:

- BuyAndHoldStrategy: enter on the first eligible bar, never exit.
- SmaTrendBaseline: classic 50/200 daily SMA cross (long-only).
- DeterministicRandomEntries: pseudo-random entries from a fixed seed with a
  fixed holding period — same costs, no signal. Any candidate that cannot
  beat its own random twin has no demonstrated edge.
"""

from __future__ import annotations

from dataclasses import dataclass

from chronos.strategies.base import (
    PositionSide,
    ProposalDirection,
    StrategyContext,
    StrategyProposal,
)


def _proposal(
    strategy: object,
    context: StrategyContext,
    direction: ProposalDirection,
    *,
    exposure: float,
    stop_fraction: float | None,
    reason: str,
) -> StrategyProposal:
    bar = context.decision_bar
    return StrategyProposal(
        strategy_id=getattr(strategy, "strategy_id", "baseline"),
        strategy_version=getattr(strategy, "version", "1"),
        timestamp_utc=bar.timestamp_utc,
        symbol=bar.symbol,
        direction=direction,
        desired_exposure_fraction=exposure,
        stop_fraction=stop_fraction,
        source_bar_ids=(bar.sequence_id,),
        reason=reason,
    )


class BuyAndHoldStrategy:
    strategy_id = "baseline_buy_hold"
    version = "1.0.0"
    warmup_bars = 1

    def on_bar(self, context: StrategyContext) -> StrategyProposal | None:
        if context.position.side is PositionSide.FLAT and len(context.bars) >= self.warmup_bars:
            return _proposal(
                self,
                context,
                ProposalDirection.ENTER_LONG,
                exposure=0.95,
                stop_fraction=0.49,
                reason="buy and hold",
            )
        return None


@dataclass
class SmaTrendBaseline:
    fast: int = 50
    slow: int = 200

    strategy_id = "baseline_sma_trend"
    version = "1.0.0"

    @property
    def warmup_bars(self) -> int:
        return self.slow + 1

    def on_bar(self, context: StrategyContext) -> StrategyProposal | None:
        bars = context.bars
        if len(bars) < self.warmup_bars:
            return None
        closes = [b.close for b in bars]
        fast_now = sum(closes[-self.fast :]) / self.fast
        slow_now = sum(closes[-self.slow :]) / self.slow
        flat = context.position.side is PositionSide.FLAT
        if flat and fast_now > slow_now:
            return _proposal(
                self,
                context,
                ProposalDirection.ENTER_LONG,
                exposure=0.95,
                stop_fraction=0.15,
                reason="fast SMA above slow SMA",
            )
        if not flat and fast_now < slow_now:
            return _proposal(
                self,
                context,
                ProposalDirection.EXIT_LONG,
                exposure=0.0,
                stop_fraction=None,
                reason="fast SMA below slow SMA",
            )
        return None


@dataclass
class DeterministicRandomEntries:
    """Entries from a linear congruential generator — deterministic, seedable."""

    seed: int = 20260717
    entry_probability_percent: int = 5
    holding_bars: int = 10

    strategy_id = "baseline_random_entries"
    version = "1.0.0"
    warmup_bars = 20

    def __post_init__(self) -> None:
        self._state = self.seed

    def _next_percent(self) -> int:
        # Numerical Recipes LCG; adequate for a deterministic no-signal twin.
        self._state = (1664525 * self._state + 1013904223) % 2**32
        return self._state % 100

    def on_bar(self, context: StrategyContext) -> StrategyProposal | None:
        if len(context.bars) < self.warmup_bars:
            return None
        draw = self._next_percent()
        flat = context.position.side is PositionSide.FLAT
        if flat and draw < self.entry_probability_percent:
            return _proposal(
                self,
                context,
                ProposalDirection.ENTER_LONG,
                exposure=0.95,
                stop_fraction=0.10,
                reason="random entry",
            )
        if not flat and context.position.bars_held >= self.holding_bars:
            return _proposal(
                self,
                context,
                ProposalDirection.EXIT_LONG,
                exposure=0.0,
                stop_fraction=None,
                reason="random holding period elapsed",
            )
        return None
