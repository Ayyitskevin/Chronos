"""Batch-vs-incremental parity for the strategy recursions.

The strategies keep O(1) incremental state (EMA, ATR, RSI averages, rolling
realized vol). These tests replay a deterministic pseudorandom walk through
the strategies bar by bar and assert their internal recursions match the
batch indicator library computed on the same closes at multiple probe
indices, within 1e-9 relative tolerance.

They also prove the continuity-reset path: an on_bar call with a
non-continuous history rebuilds internal state from scratch and produces the
same proposal (and identical internal state) as a fresh instance fed the
whole history bar by bar.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta

from chronos.indicators.core import atr, ema, rsi, stdev
from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.strategies.base import PositionState, ProposalDirection, StrategyContext
from chronos.strategies.mean_reversion import Rsi2MeanReversionStrategy
from chronos.strategies.regime_trend import MarkovRegimeTrendStrategy

SEED = 12345
N_BARS = 600
DRIFT = 0.0005


def make_walk_series(
    seed: int = SEED, n: int = N_BARS, start: float = 100.0, drift: float = DRIFT
) -> BarSeries:
    """Deterministic daily bar walk from a fixed-seed LCG (weekday sessions)."""

    state = seed & 0xFFFFFFFF

    def draw() -> float:
        nonlocal state
        state = (1664525 * state + 1013904223) % 4294967296
        return state / 4294967296

    session = date(2020, 1, 6)  # a Monday
    bars: list[Bar] = []
    close_prev = start
    for _ in range(n):
        while session.weekday() >= 5:
            session += timedelta(days=1)
        ret = (draw() - 0.5) * 0.04 + drift
        close = close_prev * (1.0 + ret)
        open_ = close_prev
        high = max(open_, close) * (1.0 + draw() * 0.01)
        low = min(open_, close) * (1.0 - draw() * 0.01)
        bars.append(
            Bar(
                symbol="SPY",
                source="lcgwalk",
                exchange="ARCA",
                interval=BarInterval.DAY_1,
                session_date=session,
                timestamp_utc=datetime.combine(session, time(21, 0), tzinfo=UTC),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=1_000_000.0,
            )
        )
        close_prev = close
        session += timedelta(days=1)
    return BarSeries(symbol="SPY", interval=BarInterval.DAY_1, bars=tuple(bars))


def close_enough(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def feed(strategy: object, series: BarSeries, upto_inclusive: int) -> list[object]:
    """Feed growing prefixes [0..i] for i in 0..upto_inclusive; return proposals."""

    flat = PositionState.flat()
    proposals: list[object] = []
    for i in range(upto_inclusive + 1):
        context = StrategyContext(bars=series.slice_until(i + 1), position=flat)
        proposals.append(strategy.on_bar(context))  # type: ignore[attr-defined]
    return proposals


class TestRegimeTrendParity:
    def test_incremental_recursions_match_batch(self) -> None:
        series = make_walk_series()
        closes = list(series.closes())
        highs = [b.high for b in series]
        lows = [b.low for b in series]
        strategy = MarkovRegimeTrendStrategy()
        flat = PositionState.flat()

        # ATR(14) seeds at index 13, EMA(100) at index 99, realized vol needs
        # a 20-bar log-return window so it exists from index 20 on.
        atr_probes = {13, 99, 120, 250, 400, 599}
        ema_probes = {99, 120, 250, 400, 599}
        vol_probes = {20, 120, 250, 400, 599}

        for i in range(len(series)):
            context = StrategyContext(bars=series.slice_until(i + 1), position=flat)
            strategy.on_bar(context)

            if i in atr_probes:
                batch_atr = atr(highs[: i + 1], lows[: i + 1], closes[: i + 1], 14)[-1]
                assert batch_atr is not None
                assert strategy._atr is not None
                assert close_enough(strategy._atr, batch_atr), f"ATR mismatch at {i}"
            if i in ema_probes:
                batch_ema = ema(closes[: i + 1], 100)[-1]
                assert batch_ema is not None
                assert strategy._ema is not None
                assert close_enough(strategy._ema, batch_ema), f"EMA mismatch at {i}"
            if i in vol_probes:
                log_rets = [
                    math.log(closes[j] / closes[j - 1]) for j in range(1, i + 1)
                ]
                batch_sd = stdev(log_rets, 20)[-1]
                assert batch_sd is not None
                batch_vol = batch_sd * math.sqrt(20)
                assert close_enough(strategy._vol_history[-1], batch_vol), (
                    f"realized vol mismatch at {i}"
                )

        # Before the seed index the incremental values must not exist yet.
        fresh = MarkovRegimeTrendStrategy()
        feed(fresh, series, 12)
        assert fresh._atr is None
        assert fresh._ema is None

    def test_continuity_reset_rebuilds_identical_state(self) -> None:
        series = make_walk_series()
        continuous = MarkovRegimeTrendStrategy()
        proposals_b = feed(continuous, series, len(series) - 1)

        # The walk must actually produce entry proposals for this comparison
        # to be meaningful; pick the last one as the probe index.
        entry_indices = [
            i
            for i, p in enumerate(proposals_b)
            if p is not None and p.direction is ProposalDirection.ENTER_LONG  # type: ignore[attr-defined]
        ]
        assert entry_indices, "expected the fixed-seed walk to trigger entries"
        probe = entry_indices[-1]

        # Discontinuous instance: fed bar by bar but the prefix ending at
        # probe-1 is skipped, so the call at `probe` sees a non-continuous
        # history and must rebuild from scratch.
        broken = MarkovRegimeTrendStrategy()
        flat = PositionState.flat()
        for i in range(probe - 1):
            broken.on_bar(StrategyContext(bars=series.slice_until(i + 1), position=flat))
        proposal_a = broken.on_bar(
            StrategyContext(bars=series.slice_until(probe + 1), position=flat)
        )

        # Same proposal as the continuous instance produced on that bar...
        assert proposal_a == proposals_b[probe]
        # ...and bit-identical internal state after the rebuild. Rerun the
        # continuous instance up to the same probe for a state comparison.
        reference = MarkovRegimeTrendStrategy()
        feed(reference, series, probe)
        assert broken._ema == reference._ema
        assert broken._atr == reference._atr
        assert broken._confirmed_state == reference._confirmed_state
        assert broken._markov_counts == reference._markov_counts
        assert list(broken._closes) == list(reference._closes)
        assert list(broken._log_returns) == list(reference._log_returns)
        assert list(broken._vol_history) == list(reference._vol_history)


class TestMeanReversionParity:
    @staticmethod
    def rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
        if avg_loss == 0.0:
            return 100.0 if avg_gain > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    def test_incremental_recursions_match_batch(self) -> None:
        series = make_walk_series()
        closes = list(series.closes())
        highs = [b.high for b in series]
        lows = [b.low for b in series]
        strategy = Rsi2MeanReversionStrategy()
        flat = PositionState.flat()

        # RSI(2) first exists at index 2, EMA(20) at index 19, ATR(14) at 13.
        rsi_probes = {2, 50, 120, 300, 599}
        ema_probes = {19, 50, 120, 300, 599}
        atr_probes = {13, 50, 120, 300, 599}

        for i in range(len(series)):
            context = StrategyContext(bars=series.slice_until(i + 1), position=flat)
            strategy.on_bar(context)

            if i in rsi_probes:
                batch_rsi = rsi(closes[: i + 1], 2)[-1]
                assert batch_rsi is not None
                assert strategy._avg_gain is not None
                assert strategy._avg_loss is not None
                incremental = self.rsi_from_averages(strategy._avg_gain, strategy._avg_loss)
                assert close_enough(incremental, batch_rsi), f"RSI mismatch at {i}"
            if i in ema_probes:
                batch_ema = ema(closes[: i + 1], 20)[-1]
                assert batch_ema is not None
                assert strategy._ema is not None
                assert close_enough(strategy._ema, batch_ema), f"EMA mismatch at {i}"
            if i in atr_probes:
                batch_atr = atr(highs[: i + 1], lows[: i + 1], closes[: i + 1], 14)[-1]
                assert batch_atr is not None
                assert strategy._atr is not None
                assert close_enough(strategy._atr, batch_atr), f"ATR mismatch at {i}"

    def test_continuity_reset_rebuilds_identical_proposal(self) -> None:
        series = make_walk_series()
        continuous = Rsi2MeanReversionStrategy()
        proposals_b = feed(continuous, series, len(series) - 1)

        entry_indices = [
            i
            for i, p in enumerate(proposals_b)
            if p is not None and p.direction is ProposalDirection.ENTER_LONG  # type: ignore[attr-defined]
        ]
        assert entry_indices, "expected the fixed-seed walk to trigger RSI-2 entries"
        probe = entry_indices[-1]

        broken = Rsi2MeanReversionStrategy()
        flat = PositionState.flat()
        for i in range(probe - 1):
            broken.on_bar(StrategyContext(bars=series.slice_until(i + 1), position=flat))
        proposal_a = broken.on_bar(
            StrategyContext(bars=series.slice_until(probe + 1), position=flat)
        )

        assert proposal_a == proposals_b[probe]

        # Bar-derived internal state must be bit-identical after the rebuild.
        reference = Rsi2MeanReversionStrategy()
        feed(reference, series, probe)
        assert broken._avg_gain == reference._avg_gain
        assert broken._avg_loss == reference._avg_loss
        assert broken._ema == reference._ema
        assert broken._atr == reference._atr
        assert broken._previous_close == reference._previous_close
