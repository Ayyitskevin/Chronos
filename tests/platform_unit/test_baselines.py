"""Baseline strategies produce the expected deterministic proposals."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.strategies.base import (
    PositionSide,
    PositionState,
    ProposalDirection,
    StrategyContext,
)
from chronos.strategies.baselines import (
    BuyAndHoldStrategy,
    DeterministicRandomEntries,
    SmaTrendBaseline,
)


def make_series(closes: list[float]) -> BarSeries:
    bars = []
    session = date(2020, 1, 6)
    for close in closes:
        while session.weekday() >= 5:
            session += timedelta(days=1)
        bars.append(
            Bar(
                symbol="TEST",
                source="synthetic",
                exchange="TEST",
                interval=BarInterval.DAY_1,
                session_date=session,
                timestamp_utc=datetime.combine(session, time(21, 0), tzinfo=UTC),
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=1000.0,
            )
        )
        session += timedelta(days=1)
    return BarSeries(symbol="TEST", interval=BarInterval.DAY_1, bars=tuple(bars))


def context(series: BarSeries, end: int, side: PositionSide, bars_held: int = 0) -> StrategyContext:
    return StrategyContext(
        bars=series.slice_until(end),
        position=PositionState(
            side=side,
            bars_held=bars_held,
            entry_price=100.0 if side is PositionSide.LONG else None,
        ),
    )


def test_buy_and_hold_enters_once_then_holds() -> None:
    series = make_series([100.0, 101.0, 102.0])
    strat = BuyAndHoldStrategy()
    entry = strat.on_bar(context(series, 1, PositionSide.FLAT))
    assert entry is not None
    assert entry.direction is ProposalDirection.ENTER_LONG
    held = strat.on_bar(context(series, 2, PositionSide.LONG, bars_held=1))
    assert held is None


def test_sma_trend_crosses() -> None:
    strat = SmaTrendBaseline(fast=2, slow=4)
    rising = make_series([10, 10, 10, 10, 20, 30])
    entry = strat.on_bar(context(rising, 6, PositionSide.FLAT))
    assert entry is not None
    assert entry.direction is ProposalDirection.ENTER_LONG

    falling = make_series([30, 30, 30, 30, 20, 10])
    exit_proposal = strat.on_bar(context(falling, 6, PositionSide.LONG, bars_held=3))
    assert exit_proposal is not None
    assert exit_proposal.direction is ProposalDirection.EXIT_LONG


def test_random_entries_deterministic() -> None:
    series = make_series([100.0 + i * 0.1 for i in range(60)])
    a = DeterministicRandomEntries()
    b = DeterministicRandomEntries()
    seq_a = [
        (p.direction if p else None)
        for i in range(25, 60)
        for p in [a.on_bar(context(series, i, PositionSide.FLAT))]
    ]
    seq_b = [
        (p.direction if p else None)
        for i in range(25, 60)
        for p in [b.on_bar(context(series, i, PositionSide.FLAT))]
    ]
    assert seq_a == seq_b  # same seed -> identical decisions


def test_baselines_respect_warmup() -> None:
    short = make_series([100.0, 101.0])
    assert SmaTrendBaseline().on_bar(context(short, 2, PositionSide.FLAT)) is None
