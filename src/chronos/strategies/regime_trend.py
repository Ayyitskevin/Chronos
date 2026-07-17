"""Markov regime-gated trend continuation — derived core of Pine 01 BULL+ v1.1.

WHAT THIS IS (and is not): the BULL+ Pine script is a 1,516-line system with
a regime engine, a Markov continuation gate, relative-strength and AVWAP
setup layers, micro-gates, and its own risk overlays. This module implements
its load-bearing core — the script's own header states "the gate is the
strategy" — exactly as specified in ``specs/regime_trend_v1.yaml``:

- regime_z = log(close/close[20]) / (stdev(log returns, 20) * sqrt(20))
  (Daily preset, StDev vol model);
- volatility-percentile threshold adjustment: percentrank of realized vol
  over 252 bars, factor = max(0.5, 1 + (pct-50)/100 * 0.25);
- enter/exit thresholds 0.85 / 0.55 with hysteresis, EMA(100) direction
  filter, 2-bar confirmation to publish a regime;
- 3-state Markov transition counts on confirmed regimes (stride 1, Raw
  estimator); long entries require bull-row stay probability >= 60% with at
  least 20 row samples;
- exit on confirmed loss of the bull regime; protective stop = 2.0 x ATR(14)
  below entry (the script's fallback stop).

Deliberately omitted layers (recorded as translation deviations in the spec
and PARITY_REPORT): AVWAP/RS/setup selection (L1P/R/A/B), pyramiding legs,
session micro-gates, equity-based halts (the platform risk engine owns
halts). Because of these omissions this is a DERIVED strategy, not a
behavioral clone; it is verified against its specification, not against
TradingView output.

Implementation style: incremental O(1) per bar. ``tests/parity`` proves the
incremental recursions equal the batch indicator library on shared inputs.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from chronos.strategies.base import (
    PositionSide,
    ProposalDirection,
    StrategyContext,
    StrategyProposal,
)

_NEUTRAL, _BULL, _BEAR = 0, 1, 2


@dataclass(frozen=True, slots=True)
class RegimeTrendParams:
    lookback: int = 20
    enter_z_base: float = 0.85
    exit_z_base: float = 0.55
    confirm_bars: int = 2
    ema_filter_len: int = 100
    vol_percentile_len: int = 252
    percentile_sensitivity: float = 0.25
    markov_min_stay: float = 0.60
    markov_min_row_n: int = 20
    atr_len: int = 14
    atr_stop_mult: float = 2.0
    exposure_fraction: float = 0.95


class MarkovRegimeTrendStrategy:
    """Deterministic, incremental implementation of the derived BULL+ core."""

    def __init__(self, params: RegimeTrendParams | None = None) -> None:
        self._params = params or RegimeTrendParams()
        self._reset()

    strategy_id = "regime_trend_v1"
    version = "1.0.0"

    @property
    def warmup_bars(self) -> int:
        # EMA filter needs its length; the vol percentile needs its window on
        # top of the lookback chain. Entries before full warm-up are refused.
        return (
            max(
                self._params.ema_filter_len,
                self._params.lookback + self._params.vol_percentile_len,
            )
            + self._params.confirm_bars
        )

    def _reset(self) -> None:
        params = self._params
        self._bars_seen = 0
        self._last_sequence_id: str | None = None
        self._closes: deque[float] = deque(maxlen=params.lookback + 1)
        self._log_returns: deque[float] = deque(maxlen=params.lookback)
        self._vol_history: deque[float] = deque(maxlen=params.vol_percentile_len)
        self._ema: float | None = None
        self._ema_seed_sum = 0.0
        self._atr: float | None = None
        self._atr_seed_sum = 0.0
        self._tr_count = 0
        self._previous_close: float | None = None
        self._raw_state_streak_state = _NEUTRAL
        self._raw_state_streak = 0
        self._confirmed_state = _NEUTRAL
        self._markov_counts = [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
        self._previous_confirmed: int | None = None

    def on_bar(self, context: StrategyContext) -> StrategyProposal | None:
        bars = context.bars
        if not bars:
            return None
        # Continuity check: if the caller replays or rewinds, rebuild state
        # deterministically from scratch rather than trusting stale state.
        if len(bars) != self._bars_seen + 1 or (
            len(bars) >= 2 and self._last_sequence_id != bars[-2].sequence_id
        ):
            self._reset()
            for bar_index in range(len(bars) - 1):
                self._ingest(bars[bar_index].high, bars[bar_index].low, bars[bar_index].close)
                self._bars_seen += 1
            self._last_sequence_id = bars[-2].sequence_id if len(bars) >= 2 else None

        bar = bars[-1]
        snapshot = self._ingest(bar.high, bar.low, bar.close)
        self._bars_seen += 1
        self._last_sequence_id = bar.sequence_id

        if snapshot is None:
            return None
        confirmed_state, stay_probability, row_n, atr_value = snapshot
        params = self._params

        flat = context.position.side is PositionSide.FLAT
        if flat:
            if len(bars) < self.warmup_bars:
                return None
            gate = (
                confirmed_state == _BULL
                and row_n >= params.markov_min_row_n
                and stay_probability is not None
                and stay_probability >= params.markov_min_stay
            )
            if gate and stay_probability is not None and atr_value is not None and bar.close > 0:
                stop_fraction = min(0.5, max(0.005, params.atr_stop_mult * atr_value / bar.close))
                return StrategyProposal(
                    strategy_id=self.strategy_id,
                    strategy_version=self.version,
                    timestamp_utc=bar.timestamp_utc,
                    symbol=bar.symbol,
                    direction=ProposalDirection.ENTER_LONG,
                    desired_exposure_fraction=params.exposure_fraction,
                    stop_fraction=stop_fraction,
                    evidence={
                        "confirmed_state": float(confirmed_state),
                        "stay_probability": float(stay_probability),
                        "row_n": float(row_n),
                        "atr": float(atr_value),
                    },
                    source_bar_ids=(bar.sequence_id,),
                    reason="bull regime confirmed with sticky Markov row",
                )
            return None

        if confirmed_state != _BULL:
            return StrategyProposal(
                strategy_id=self.strategy_id,
                strategy_version=self.version,
                timestamp_utc=bar.timestamp_utc,
                symbol=bar.symbol,
                direction=ProposalDirection.EXIT_LONG,
                desired_exposure_fraction=0.0,
                stop_fraction=None,
                evidence={"confirmed_state": float(confirmed_state)},
                source_bar_ids=(bar.sequence_id,),
                reason="bull regime lost on confirmed bar",
            )
        return None

    def _ingest(
        self, high: float, low: float, close: float
    ) -> tuple[int, float | None, int, float | None] | None:
        """Update all recursions with one closed bar; return the decision state."""

        params = self._params

        # ATR via Wilder RMA, seeded with SMA like the batch library.
        if self._previous_close is None:
            true_range = high - low
        else:
            true_range = max(
                high - low,
                abs(high - self._previous_close),
                abs(low - self._previous_close),
            )
        self._tr_count += 1
        if self._atr is None:
            self._atr_seed_sum += true_range
            if self._tr_count == params.atr_len:
                self._atr = self._atr_seed_sum / params.atr_len
        else:
            self._atr = (self._atr * (params.atr_len - 1) + true_range) / params.atr_len

        # EMA(100), seeded with SMA(100).
        if self._ema is None:
            self._ema_seed_sum += close
            if self._bars_seen + 1 == params.ema_filter_len:
                self._ema = self._ema_seed_sum / params.ema_filter_len
        else:
            alpha = 2.0 / (params.ema_filter_len + 1.0)
            self._ema = alpha * close + (1.0 - alpha) * self._ema

        # Log return chain.
        if self._previous_close is not None and self._previous_close > 0 and close > 0:
            self._log_returns.append(math.log(close / self._previous_close))
        self._closes.append(close)
        self._previous_close = close

        regime_z: float | None = None
        realized_vol: float | None = None
        if len(self._closes) == params.lookback + 1 and len(self._log_returns) == params.lookback:
            oldest = self._closes[0]
            if oldest > 0 and close > 0:
                window_log_ret = math.log(close / oldest)
                mean = sum(self._log_returns) / params.lookback
                variance = sum((r - mean) ** 2 for r in self._log_returns) / params.lookback
                realized_vol = math.sqrt(variance) * math.sqrt(params.lookback)
                if realized_vol > 0:
                    regime_z = window_log_ret / realized_vol

        # Volatility percentile of realized vol (Pine ta.percentrank semantics:
        # current value ranked against the previous N values, excluding itself).
        vol_factor = 1.0
        if realized_vol is not None:
            if len(self._vol_history) == params.vol_percentile_len:
                rank = (
                    100.0
                    * sum(1 for v in self._vol_history if v <= realized_vol)
                    / params.vol_percentile_len
                )
                vol_factor = max(0.5, 1.0 + ((rank - 50.0) / 100.0) * params.percentile_sensitivity)
            self._vol_history.append(realized_vol)

        enter_z = max(0.10, params.enter_z_base * vol_factor)
        exit_z = max(0.05, min(params.exit_z_base * vol_factor, enter_z - 0.05))

        if regime_z is None:
            return None

        trend_up = self._ema is None or close > self._ema
        trend_down = self._ema is None or close < self._ema

        previous_confirmed = self._confirmed_state
        if previous_confirmed == _BULL and regime_z > exit_z and trend_up:
            raw_state = _BULL
        elif previous_confirmed == _BEAR and regime_z < -exit_z and trend_down:
            raw_state = _BEAR
        elif regime_z > enter_z and trend_up:
            raw_state = _BULL
        elif regime_z < -enter_z and trend_down:
            raw_state = _BEAR
        else:
            raw_state = _NEUTRAL

        # Confirmation: a state change publishes only after `confirm_bars`
        # consecutive closed bars in the new state (matching the confirmed-bar
        # state machine convention of the Pine suite).
        if raw_state == self._raw_state_streak_state:
            self._raw_state_streak += 1
        else:
            self._raw_state_streak_state = raw_state
            self._raw_state_streak = 1
        if self._raw_state_streak >= params.confirm_bars and self._confirmed_state != raw_state:
            self._confirmed_state = raw_state

        # Markov transition counting over confirmed states, stride 1.
        if self._previous_confirmed is not None:
            self._markov_counts[self._previous_confirmed][self._confirmed_state] += 1
        self._previous_confirmed = self._confirmed_state

        bull_row = self._markov_counts[_BULL]
        row_n = sum(bull_row)
        stay_probability = bull_row[_BULL] / row_n if row_n > 0 else None
        return self._confirmed_state, stay_probability, row_n, self._atr
