"""Pure, deterministic Pine-derived regime context over locally stored daily bars.

This module powers the Milestone 4 regime-context panel (decision support
only, per docs/LIVE_WHEEL_GAME_PLAN.md §0/§3.8). It reads **closed daily
bars** from ``research/data/raw/{SYMBOL}.csv`` via the strict
:func:`chronos.marketdata.csv_provider.load_daily_csv` loader and computes a
small, documented set of heuristics. It has no broker dependency, opens no
network connection, and can never gate or transmit an order.

Math (all closed-bar, no lookahead — every value at index ``i`` uses closes
``[0..i]`` only, and the published context uses the full series of already
closed sessions):

- **EMA(50) / EMA(200)** — standard recursive exponential moving average
  with ``alpha = 2 / (period + 1)``, seeded with the first close.
- **Trend state** — ``UPTREND`` when ``ema_fast > ema_slow`` and the last
  close is above ``ema_fast``; ``DOWNTREND`` when ``ema_fast < ema_slow``
  and the last close is below ``ema_fast``; otherwise ``MIXED`` (ties are
  deliberately MIXED — fail toward the neutral label).
- **RSI(2)** — Cutler's simple-average RSI over the last two close-to-close
  changes (not Wilder smoothing): ``RSI = 100 - 100 / (1 + avg_gain /
  avg_loss)``. Zero average loss with any gain ⇒ 100; zero average gain
  with any loss ⇒ 0; a fully flat window ⇒ 50 (neutral).
- **Volatility percentile** — sample standard deviation (n-1) of the last
  20 simple daily returns, percentile-ranked (inclusive: share of window
  observations ``<=`` the latest) within the trailing 252 observations of
  that same rolling stdev series (fewer when history is shorter).

Fail-closed: fewer than ``MIN_BARS`` closed sessions, a missing file, or a
file the strict loader rejects all yield ``available=False`` with reasons —
never an exception to the caller, never partial numbers.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from pathlib import Path

from pydantic import Field, field_validator, model_validator

from chronos.domain.models import ChronosModel
from chronos.marketdata.csv_provider import load_daily_csv

EMA_FAST_PERIOD = 50
EMA_SLOW_PERIOD = 200
RSI_PERIOD = 2
VOL_RETURN_WINDOW = 20
VOL_PERCENTILE_LOOKBACK = 252
MIN_BARS = 210

HEURISTIC_NOTE = (
    "Pine-derived heuristic context — not a validated signal, not an assignment "
    "probability; research verdict: zero strategies validated."
)
_REQUIRED_NOTE_PHRASE = "heuristic context — not a validated signal"


class TrendState(StrEnum):
    UPTREND = "UPTREND"
    DOWNTREND = "DOWNTREND"
    MIXED = "MIXED"


class RegimeContext(ChronosModel):
    """Labeled heuristic context for one symbol; never an order gate."""

    symbol: str
    available: bool
    as_of_session: date | None = None
    bars: int = Field(ge=0)
    trend_state: TrendState | None = None
    ema_fast: Decimal | None = None
    ema_slow: Decimal | None = None
    rsi2: Decimal | None = None
    vol_percentile: Decimal | None = None
    note: str = HEURISTIC_NOTE
    reasons: tuple[str, ...] = ()

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be blank")
        return normalized

    @field_validator("note")
    @classmethod
    def require_heuristic_disclaimer(cls, value: str) -> str:
        if _REQUIRED_NOTE_PHRASE not in value:
            raise ValueError("note must carry the heuristic-context disclaimer")
        return value

    @model_validator(mode="after")
    def validate_availability(self) -> RegimeContext:
        metrics = (self.trend_state, self.ema_fast, self.ema_slow, self.rsi2, self.vol_percentile)
        if self.available:
            if any(metric is None for metric in metrics):
                raise ValueError("an available regime context requires every metric")
            if self.as_of_session is None or self.bars < MIN_BARS:
                raise ValueError("an available regime context requires dated, sufficient history")
        else:
            if any(metric is not None for metric in metrics):
                raise ValueError("an unavailable regime context must not publish metrics")
            if not self.reasons:
                raise ValueError("an unavailable regime context must explain itself")
        return self


def _unavailable(
    symbol: str,
    *,
    bars: int,
    as_of_session: date | None,
    reasons: tuple[str, ...],
) -> RegimeContext:
    return RegimeContext(
        symbol=symbol,
        available=False,
        as_of_session=as_of_session,
        bars=bars,
        reasons=reasons,
    )


def _ema(closes: Sequence[float], period: int) -> float:
    """Standard EMA with ``alpha = 2/(period+1)``, seeded with the first close."""

    if not closes:
        raise ValueError("EMA requires at least one close")
    alpha = 2.0 / (period + 1.0)
    value = closes[0]
    for close in closes[1:]:
        value = alpha * close + (1.0 - alpha) * value
    return value


def _rsi_simple(closes: Sequence[float], period: int) -> float:
    """Cutler's simple-average RSI over the last ``period`` close-to-close changes."""

    if len(closes) < period + 1:
        raise ValueError("RSI requires more closes than its period")
    changes = [closes[i] - closes[i - 1] for i in range(len(closes) - period, len(closes))]
    avg_gain = sum(max(change, 0.0) for change in changes) / period
    avg_loss = sum(max(-change, 0.0) for change in changes) / period
    if avg_loss == 0.0 and avg_gain == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _vol_percentile(closes: Sequence[float]) -> float:
    """Inclusive percentile rank of the latest 20-day return stdev in its trailing window."""

    returns = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
    if len(returns) < VOL_RETURN_WINDOW:
        raise ValueError("volatility percentile requires a full return window")
    stdevs = [
        statistics.stdev(returns[end - VOL_RETURN_WINDOW : end])
        for end in range(VOL_RETURN_WINDOW, len(returns) + 1)
    ]
    window = stdevs[-VOL_PERCENTILE_LOOKBACK:]
    latest = window[-1]
    return sum(1 for value in window if value <= latest) / len(window)


def _classify_trend(last_close: float, ema_fast: float, ema_slow: float) -> TrendState:
    if ema_fast > ema_slow and last_close > ema_fast:
        return TrendState.UPTREND
    if ema_fast < ema_slow and last_close < ema_fast:
        return TrendState.DOWNTREND
    return TrendState.MIXED


def _quantize(value: float, places: int) -> Decimal:
    return Decimal(f"{value:.{places}f}")


def compute_regime_context_from_closes(
    symbol: str,
    closes: Sequence[float],
    *,
    as_of_session: date | None,
) -> RegimeContext:
    """Pure deterministic core: identical close sequences yield identical contexts.

    ``closes`` must be the chronological closes of already **closed** daily
    sessions (oldest first); the context describes the state as of the final
    close and uses no information beyond it.
    """

    normalized = symbol.strip().upper()
    bars = len(closes)
    if bars < MIN_BARS:
        return _unavailable(
            normalized,
            bars=bars,
            as_of_session=as_of_session if bars else None,
            reasons=(
                f"insufficient daily history for {normalized}: "
                f"{bars} closed bars < {MIN_BARS} required",
            ),
        )
    if any(not isfinite(close) or close <= 0.0 for close in closes):
        return _unavailable(
            normalized,
            bars=bars,
            as_of_session=as_of_session,
            reasons=(f"daily history for {normalized} contains non-finite or non-positive closes",),
        )

    last_close = closes[-1]
    ema_fast = _ema(closes, EMA_FAST_PERIOD)
    ema_slow = _ema(closes, EMA_SLOW_PERIOD)
    trend_state = _classify_trend(last_close, ema_fast, ema_slow)
    rsi2 = _rsi_simple(closes, RSI_PERIOD)
    vol_percentile = _vol_percentile(closes)
    return RegimeContext(
        symbol=normalized,
        available=True,
        as_of_session=as_of_session,
        bars=bars,
        trend_state=trend_state,
        ema_fast=_quantize(ema_fast, 6),
        ema_slow=_quantize(ema_slow, 6),
        rsi2=_quantize(rsi2, 4),
        vol_percentile=_quantize(vol_percentile, 6),
        reasons=(
            f"trend {trend_state.value}: last close {last_close:.6f} vs "
            f"EMA({EMA_FAST_PERIOD}) {ema_fast:.6f} and EMA({EMA_SLOW_PERIOD}) {ema_slow:.6f}",
            f"RSI({RSI_PERIOD}) {rsi2:.4f} (Cutler simple average, closed bars only)",
            f"{VOL_RETURN_WINDOW}-day return stdev percentile {vol_percentile:.6f} "
            f"within its trailing {VOL_PERCENTILE_LOOKBACK} observations",
        ),
    )


def compute_regime_context(symbol: str, data_dir: Path) -> RegimeContext:
    """Load ``data_dir/{SYMBOL}.csv`` (closed daily bars) and compute the context.

    Fail-closed and exception-free toward the caller: an unknown symbol shape,
    a missing file, or a strict-loader rejection all yield ``available=False``
    with reasons rather than an error.
    """

    normalized = symbol.strip().upper()
    if not normalized or not normalized.isalnum():
        return _unavailable(
            symbol=normalized or "INVALID",
            bars=0,
            as_of_session=None,
            reasons=("symbol must be a nonblank alphanumeric code",),
        )
    path = data_dir / f"{normalized}.csv"
    if not path.is_file():
        return _unavailable(
            normalized,
            bars=0,
            as_of_session=None,
            reasons=(f"no local daily history for {normalized}",),
        )
    try:
        loaded = load_daily_csv(path, normalized, source=str(data_dir))
    except (ValueError, OSError, UnicodeDecodeError):
        return _unavailable(
            normalized,
            bars=0,
            as_of_session=None,
            reasons=(f"local daily history for {normalized} failed the strict CSV load",),
        )
    bars = loaded.series.bars
    closes = [bar.close for bar in bars]
    as_of_session = bars[-1].session_date if bars else None
    return compute_regime_context_from_closes(normalized, closes, as_of_session=as_of_session)
