"""Core indicator math. See package docstring for conventions.

Pine reference semantics implemented here:

- ``ta.sma(src, n)``: arithmetic mean of the last n values; na until n values.
- ``ta.ema(src, n)``: alpha = 2/(n+1); seeded with SMA of the first n values
  (Pine seeds EMA with the first non-na source value; TradingView's
  documented recursion uses the previous EMA once available. We seed with
  SMA(n) — the difference decays geometrically and the parity report
  documents this explicitly.)
- ``ta.rma(src, n)``: Wilder's smoothing, alpha = 1/n, seeded with SMA(n);
  this matches TradingView's documented definition.
- ``ta.rsi(src, n)``: 100 - 100/(1+rs) on RMA of gains/losses.
- ``ta.atr(n)``: RMA of true range.
- ``ta.stdev(src, n)``: population standard deviation (divide by n), matching
  TradingView.
- ``ta.highest/lowest(src, n)``: rolling extremes including current bar.
- ``ta.change(src)``: src - src[1].
- ``ta.crossover(a, b)``: a[now] > b[now] and a[prev] <= b[prev].
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Series = list[float | None]


def _valid(value: float | None) -> bool:
    return value is not None and not math.isnan(value)


def sma(source: Sequence[float], length: int) -> Series:
    if length <= 0:
        raise ValueError("length must be positive")
    out: Series = [None] * len(source)
    running = 0.0
    for i, value in enumerate(source):
        running += value
        if i >= length:
            running -= source[i - length]
        if i >= length - 1:
            out[i] = running / length
    return out


def ema(source: Sequence[float], length: int) -> Series:
    if length <= 0:
        raise ValueError("length must be positive")
    out: Series = [None] * len(source)
    alpha = 2.0 / (length + 1.0)
    previous: float | None = None
    running = 0.0
    for i, value in enumerate(source):
        if previous is None:
            running += value
            if i == length - 1:
                previous = running / length
                out[i] = previous
        else:
            previous = alpha * value + (1.0 - alpha) * previous
            out[i] = previous
    return out


def rma(source: Sequence[float], length: int) -> Series:
    if length <= 0:
        raise ValueError("length must be positive")
    out: Series = [None] * len(source)
    alpha = 1.0 / length
    previous: float | None = None
    running = 0.0
    for i, value in enumerate(source):
        if previous is None:
            running += value
            if i == length - 1:
                previous = running / length
                out[i] = previous
        else:
            previous = alpha * value + (1.0 - alpha) * previous
            out[i] = previous
    return out


def change(source: Sequence[float]) -> Series:
    out: Series = [None] * len(source)
    for i in range(1, len(source)):
        out[i] = source[i] - source[i - 1]
    return out


def roc(source: Sequence[float], length: int) -> Series:
    if length <= 0:
        raise ValueError("length must be positive")
    out: Series = [None] * len(source)
    for i in range(length, len(source)):
        base = source[i - length]
        out[i] = 100.0 * (source[i] - base) / base if base != 0 else None
    return out


def rsi(source: Sequence[float], length: int) -> Series:
    if length <= 0:
        raise ValueError("length must be positive")
    gains: list[float] = [0.0] * len(source)
    losses: list[float] = [0.0] * len(source)
    for i in range(1, len(source)):
        delta = source[i] - source[i - 1]
        gains[i] = max(delta, 0.0)
        losses[i] = max(-delta, 0.0)
    # Pine computes rma over the series starting at the first change; the
    # leading synthetic zero at index 0 is excluded by seeding from index 1.
    avg_gain = rma(gains[1:], length)
    avg_loss = rma(losses[1:], length)
    out: Series = [None] * len(source)
    for j in range(len(avg_gain)):
        gain = avg_gain[j]
        loss = avg_loss[j]
        if not _valid(gain) or not _valid(loss):
            continue
        assert gain is not None and loss is not None
        if loss == 0.0:
            out[j + 1] = 100.0 if gain > 0 else 50.0
        else:
            rs = gain / loss
            out[j + 1] = 100.0 - 100.0 / (1.0 + rs)
    return out


def true_range(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[float]:
    if not len(highs) == len(lows) == len(closes):
        raise ValueError("high/low/close must be the same length")
    out: list[float] = []
    for i in range(len(highs)):
        if i == 0:
            out.append(highs[0] - lows[0])
        else:
            out.append(
                max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
            )
    return out


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], length: int
) -> Series:
    return rma(true_range(highs, lows, closes), length)


def stdev(source: Sequence[float], length: int) -> Series:
    if length <= 0:
        raise ValueError("length must be positive")
    out: Series = [None] * len(source)
    for i in range(length - 1, len(source)):
        window = source[i - length + 1 : i + 1]
        mean = sum(window) / length
        variance = sum((value - mean) ** 2 for value in window) / length
        out[i] = math.sqrt(variance)
    return out


def highest(source: Sequence[float], length: int) -> Series:
    if length <= 0:
        raise ValueError("length must be positive")
    out: Series = [None] * len(source)
    for i in range(length - 1, len(source)):
        out[i] = max(source[i - length + 1 : i + 1])
    return out


def lowest(source: Sequence[float], length: int) -> Series:
    if length <= 0:
        raise ValueError("length must be positive")
    out: Series = [None] * len(source)
    for i in range(length - 1, len(source)):
        out[i] = min(source[i - length + 1 : i + 1])
    return out


def percentrank(source: Sequence[float], length: int) -> Series:
    """Pine ``ta.percentrank``: % of the previous ``length`` values <= current."""

    if length <= 0:
        raise ValueError("length must be positive")
    out: Series = [None] * len(source)
    for i in range(length, len(source)):
        window = source[i - length : i]
        out[i] = 100.0 * sum(1 for value in window if value <= source[i]) / length
    return out


def crossover(a: Series, b: Series) -> list[bool]:
    out = [False] * len(a)
    for i in range(1, min(len(a), len(b))):
        a_now, b_now, a_prev, b_prev = a[i], b[i], a[i - 1], b[i - 1]
        if _valid(a_now) and _valid(b_now) and _valid(a_prev) and _valid(b_prev):
            assert a_now is not None and b_now is not None
            assert a_prev is not None and b_prev is not None
            out[i] = a_now > b_now and a_prev <= b_prev
    return out


def crossunder(a: Series, b: Series) -> list[bool]:
    out = [False] * len(a)
    for i in range(1, min(len(a), len(b))):
        a_now, b_now, a_prev, b_prev = a[i], b[i], a[i - 1], b[i - 1]
        if _valid(a_now) and _valid(b_now) and _valid(a_prev) and _valid(b_prev):
            assert a_now is not None and b_now is not None
            assert a_prev is not None and b_prev is not None
            out[i] = a_now < b_now and a_prev >= b_prev
    return out
