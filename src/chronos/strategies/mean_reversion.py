"""RSI-2 mean-reversion — derived daily core of Pine 11 MR Extremes Study v1.1.

The Pine source is a *study*: it flags oversold/overbought extremes
(RSI-2 <= 10 with a session-VWAP sigma stretch, regime-filtered by the prior
day's EMA20) and scores whether price reverted within a horizon; it defines
no orders. This module derives an executable daily-bar long-only strategy
from the flag definition, as specified in ``specs/mean_reversion_v1.yaml``:

- entry flag: RSI(2) <= 10 on a confirmed daily bar, close above EMA(20)
  computed on prior-bar values (the study's non-repainting daily filter),
  re-armed only after RSI(2) recrosses above 50 (the study's suppression
  rule);
- the session-VWAP stretch condition CANNOT exist on daily bars and is
  omitted — a recorded deviation, so this is a different, simpler strategy
  than the study's intraday flags;
- exit: RSI(2) >= 50 recross (reversion realized) or 10-bar time stop;
- protective stop: 1.5 x ATR(14) below entry — the study's own "adverse
  move = fail" threshold repurposed as a hard stop.

Incremental O(1) per bar; parity of the recursions with the batch indicator
library is asserted in tests/parity.
"""

from __future__ import annotations

from dataclasses import dataclass

from chronos.strategies.base import (
    PositionSide,
    ProposalDirection,
    StrategyContext,
    StrategyProposal,
)


@dataclass(frozen=True, slots=True)
class MeanReversionParams:
    rsi_len: int = 2
    oversold: float = 10.0
    rearm_level: float = 50.0
    exit_level: float = 50.0
    ema_filter_len: int = 20
    atr_len: int = 14
    fail_atr_mult: float = 1.5
    time_stop_bars: int = 10
    exposure_fraction: float = 0.95


class Rsi2MeanReversionStrategy:
    """Deterministic, incremental implementation of the derived MR core."""

    strategy_id = "mean_reversion_v1"
    version = "1.0.0"

    def __init__(self, params: MeanReversionParams | None = None) -> None:
        self._params = params or MeanReversionParams()
        self._reset()

    @property
    def warmup_bars(self) -> int:
        return max(self._params.ema_filter_len, self._params.atr_len) + self._params.rsi_len + 2

    def _reset(self) -> None:
        self._bars_seen = 0
        self._last_sequence_id: str | None = None
        self._previous_close: float | None = None
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._gain_seed = 0.0
        self._loss_seed = 0.0
        self._change_count = 0
        self._ema: float | None = None
        self._ema_seed_sum = 0.0
        self._previous_ema: float | None = None
        self._previous_bar_close: float | None = None
        self._atr: float | None = None
        self._atr_seed_sum = 0.0
        self._tr_count = 0
        self._armed = True

    def on_bar(self, context: StrategyContext) -> StrategyProposal | None:
        bars = context.bars
        if not bars:
            return None
        if len(bars) != self._bars_seen + 1 or (
            len(bars) >= 2 and self._last_sequence_id != bars[-2].sequence_id
        ):
            self._reset()
            for index in range(len(bars) - 1):
                self._ingest(bars[index].high, bars[index].low, bars[index].close)
                self._bars_seen += 1
            self._last_sequence_id = bars[-2].sequence_id if len(bars) >= 2 else None

        bar = bars[-1]
        snapshot = self._ingest(bar.high, bar.low, bar.close)
        self._bars_seen += 1
        self._last_sequence_id = bar.sequence_id
        if snapshot is None:
            return None
        rsi_value, prior_ema, prior_close, atr_value = snapshot
        params = self._params

        flat = context.position.side is PositionSide.FLAT
        if flat:
            if len(bars) < self.warmup_bars:
                return None
            daily_up = prior_ema is not None and prior_close is not None and prior_close > prior_ema
            if (
                self._armed
                and rsi_value <= params.oversold
                and daily_up
                and atr_value is not None
                and bar.close > 0
            ):
                self._armed = False
                stop_fraction = min(0.5, max(0.005, params.fail_atr_mult * atr_value / bar.close))
                return StrategyProposal(
                    strategy_id=self.strategy_id,
                    strategy_version=self.version,
                    timestamp_utc=bar.timestamp_utc,
                    symbol=bar.symbol,
                    direction=ProposalDirection.ENTER_LONG,
                    desired_exposure_fraction=params.exposure_fraction,
                    stop_fraction=stop_fraction,
                    evidence={"rsi2": float(rsi_value), "atr": float(atr_value)},
                    source_bar_ids=(bar.sequence_id,),
                    reason="RSI-2 oversold extreme in daily uptrend",
                )
            if not self._armed and rsi_value >= params.rearm_level:
                self._armed = True
            return None

        exit_reversion = rsi_value >= params.exit_level
        exit_time = context.position.bars_held >= params.time_stop_bars
        if exit_reversion or exit_time:
            if rsi_value >= params.rearm_level:
                self._armed = True
            return StrategyProposal(
                strategy_id=self.strategy_id,
                strategy_version=self.version,
                timestamp_utc=bar.timestamp_utc,
                symbol=bar.symbol,
                direction=ProposalDirection.EXIT_LONG,
                desired_exposure_fraction=0.0,
                stop_fraction=None,
                evidence={"rsi2": float(rsi_value)},
                source_bar_ids=(bar.sequence_id,),
                reason="reversion target" if exit_reversion else "time stop",
            )
        return None

    def _ingest(
        self, high: float, low: float, close: float
    ) -> tuple[float, float | None, float | None, float | None] | None:
        """Update recursions; return (rsi, prior_ema, prior_close, atr)."""

        params = self._params

        prior_ema = self._previous_ema
        prior_close = self._previous_bar_close

        # ATR via Wilder RMA seeded with SMA.
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

        # EMA(20) seeded with SMA(20). The strategy uses PRIOR bar values for
        # the daily filter (the study reads close[1] > ema[1]).
        if self._ema is None:
            self._ema_seed_sum += close
            if self._bars_seen + 1 == params.ema_filter_len:
                self._ema = self._ema_seed_sum / params.ema_filter_len
        else:
            alpha = 2.0 / (params.ema_filter_len + 1.0)
            self._ema = alpha * close + (1.0 - alpha) * self._ema

        # RSI(2) via Wilder RMA of gains/losses, seeded with SMA like the
        # batch library (first change excluded exactly as in indicators.rsi).
        rsi_value: float | None = None
        if self._previous_close is not None:
            delta = close - self._previous_close
            gain = max(delta, 0.0)
            loss = max(-delta, 0.0)
            self._change_count += 1
            if self._avg_gain is None:
                self._gain_seed += gain
                self._loss_seed += loss
                if self._change_count == params.rsi_len:
                    self._avg_gain = self._gain_seed / params.rsi_len
                    self._avg_loss = self._loss_seed / params.rsi_len
            else:
                assert self._avg_loss is not None
                self._avg_gain = (self._avg_gain * (params.rsi_len - 1) + gain) / params.rsi_len
                self._avg_loss = (self._avg_loss * (params.rsi_len - 1) + loss) / params.rsi_len
            if self._avg_gain is not None and self._avg_loss is not None:
                if self._avg_loss == 0.0:
                    rsi_value = 100.0 if self._avg_gain > 0 else 50.0
                else:
                    rs = self._avg_gain / self._avg_loss
                    rsi_value = 100.0 - 100.0 / (1.0 + rs)

        self._previous_close = close
        self._previous_ema = self._ema
        self._previous_bar_close = close

        if rsi_value is None:
            return None
        return rsi_value, prior_ema, prior_close, self._atr
