"""Pine-compatible indicator primitives used only by the Five-Tool research engine.

The production indicator package deliberately SMA-seeds EMA.  Pine's ``ta.ema``
starts at the first non-``na`` source value, so using that package here would hide a
known parity difference.  These functions keep the compatibility choice local and
make missing-value behavior explicit.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from itertools import pairwise
from typing import Literal

NullableSeries = tuple[float | None, ...]


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def pine_sma(source: Sequence[float | None], length: int) -> NullableSeries:
    """Pine-style SMA over the most recent ``length`` non-``na`` observations."""

    if length <= 0:
        raise ValueError("length must be positive")
    window: deque[float] = deque()
    running = 0.0
    output: list[float | None] = []
    for value in source:
        if _finite(value):
            assert value is not None
            window.append(value)
            running += value
            if len(window) > length:
                running -= window.popleft()
        output.append(running / length if len(window) == length else None)
    return tuple(output)


def pine_ema(source: Sequence[float | None], length: int) -> NullableSeries:
    """Pine ``ta.ema`` recursion, seeded by the first non-missing source value."""

    if length <= 0:
        raise ValueError("length must be positive")
    alpha = 2.0 / (length + 1.0)
    previous: float | None = None
    output: list[float | None] = []
    for value in source:
        if not _finite(value):
            output.append(previous)
            continue
        assert value is not None
        previous = value if previous is None else alpha * value + (1.0 - alpha) * previous
        output.append(previous)
    return tuple(output)


def pine_rma(source: Sequence[float | None], length: int) -> NullableSeries:
    """Wilder RMA with an SMA seed after ``length`` non-missing values."""

    if length <= 0:
        raise ValueError("length must be positive")
    alpha = 1.0 / length
    seed: list[float] = []
    previous: float | None = None
    output: list[float | None] = []
    for value in source:
        if not _finite(value):
            output.append(previous)
            continue
        assert value is not None
        if previous is None:
            seed.append(value)
            if len(seed) == length:
                previous = math.fsum(seed) / length
            output.append(previous)
            continue
        previous = alpha * value + (1.0 - alpha) * previous
        output.append(previous)
    return tuple(output)


def pine_stdev(source: Sequence[float | None], length: int) -> NullableSeries:
    """Population standard deviation over the latest non-missing observations."""

    if length <= 0:
        raise ValueError("length must be positive")
    window: deque[float] = deque()
    output: list[float | None] = []
    for value in source:
        if _finite(value):
            assert value is not None
            window.append(value)
            if len(window) > length:
                window.popleft()
        if len(window) != length:
            output.append(None)
            continue
        mean = math.fsum(window) / length
        output.append(math.sqrt(math.fsum((item - mean) ** 2 for item in window) / length))
    return tuple(output)


def pine_percentrank(source: Sequence[float | None], length: int) -> NullableSeries:
    """Percentage of the preceding ``length`` values no greater than current.

    Unlike Pine moving averages, ``ta.percentrank`` includes missing source slots
    in its window and returns ``na`` while any such slot remains in that window.
    """

    if length <= 0:
        raise ValueError("length must be positive")
    history: list[float | None] = []
    output: list[float | None] = []
    for value in source:
        prior = history[-length:]
        if not _finite(value) or len(prior) < length or any(not _finite(item) for item in prior):
            output.append(None)
        else:
            assert value is not None
            output.append(
                100.0 * sum(item is not None and item <= value for item in prior) / length
            )
        history.append(value if _finite(value) else None)
    return tuple(output)


def true_range(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> tuple[float, ...]:
    if not len(highs) == len(lows) == len(closes):
        raise ValueError("high, low, and close series must have equal length")
    result: list[float] = []
    for index, (high, low) in enumerate(zip(highs, lows, strict=True)):
        if index == 0:
            result.append(high - low)
        else:
            previous_close = closes[index - 1]
            result.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return tuple(result)


def pine_atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], length: int
) -> NullableSeries:
    return pine_rma(true_range(highs, lows, closes), length)


def pine_rsi(source: Sequence[float], length: int) -> NullableSeries:
    if length <= 0:
        raise ValueError("length must be positive")
    gains: list[float | None] = [None]
    losses: list[float | None] = [None]
    for previous, current in pairwise(source):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = pine_rma(gains, length)
    avg_loss = pine_rma(losses, length)
    output: list[float | None] = []
    for gain, loss in zip(avg_gain, avg_loss, strict=True):
        if gain is None or loss is None:
            output.append(None)
        elif loss == 0.0:
            output.append(100.0 if gain > 0.0 else 50.0)
        else:
            output.append(100.0 - 100.0 / (1.0 + gain / loss))
    return tuple(output)


def pine_mfi(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    length: int,
) -> NullableSeries:
    """Money Flow Index with Pine-compatible typical-price direction."""

    if not len(highs) == len(lows) == len(closes) == len(volumes):
        raise ValueError("OHLCV series must have equal length")
    typical = tuple(
        (high + low + close) / 3.0 for high, low, close in zip(highs, lows, closes, strict=True)
    )
    if not typical:
        return ()
    # Pine v6 comparisons with the first bar's missing change are false, so both
    # ternaries in the documented ta.mfi equivalent select the first raw flow.
    initial_flow = typical[0] * volumes[0]
    positive: list[float | None] = [initial_flow]
    negative: list[float | None] = [initial_flow]
    for index in range(1, len(typical)):
        flow = typical[index] * volumes[index]
        positive.append(flow if typical[index] > typical[index - 1] else 0.0)
        negative.append(flow if typical[index] < typical[index - 1] else 0.0)
    positive_sum = tuple(
        None if item is None else item * length for item in pine_sma(positive, length)
    )
    negative_sum = tuple(
        None if item is None else item * length for item in pine_sma(negative, length)
    )
    output: list[float | None] = []
    for pos, neg in zip(positive_sum, negative_sum, strict=True):
        if pos is None or neg is None:
            output.append(None)
        elif neg == 0.0:
            output.append(100.0 if pos > 0.0 else 50.0)
        else:
            output.append(100.0 - 100.0 / (1.0 + pos / neg))
    return tuple(output)


def pine_dmi(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    di_length: int,
    adx_smoothing: int,
) -> tuple[NullableSeries, NullableSeries, NullableSeries]:
    """Directional movement (+DI, -DI, ADX) using Wilder smoothing."""

    if not len(highs) == len(lows) == len(closes):
        raise ValueError("high, low, and close series must have equal length")
    plus_dm: list[float | None] = [None]
    minus_dm: list[float | None] = [None]
    for index in range(1, len(highs)):
        up = highs[index] - highs[index - 1]
        down = lows[index - 1] - lows[index]
        plus_dm.append(up if up > down and up > 0.0 else 0.0)
        minus_dm.append(down if down > up and down > 0.0 else 0.0)
    # Pine's true-range seed includes the first bar's high-low range.  Directional
    # movement itself still begins on the second bar because it needs a predecessor.
    atr_values = pine_rma(true_range(highs, lows, closes), di_length)
    plus_smoothed = pine_rma(plus_dm, di_length)
    minus_smoothed = pine_rma(minus_dm, di_length)
    plus_di: list[float | None] = []
    minus_di: list[float | None] = []
    dx: list[float | None] = []
    for atr_value, plus_value, minus_value in zip(
        atr_values, plus_smoothed, minus_smoothed, strict=True
    ):
        if atr_value is None or plus_value is None or minus_value is None or atr_value <= 0.0:
            plus_di.append(None)
            minus_di.append(None)
            dx.append(None)
            continue
        plus = 100.0 * plus_value / atr_value
        minus = 100.0 * minus_value / atr_value
        denominator = plus + minus
        plus_di.append(plus)
        minus_di.append(minus)
        dx.append(0.0 if denominator == 0.0 else 100.0 * abs(plus - minus) / denominator)
    return tuple(plus_di), tuple(minus_di), pine_rma(dx, adx_smoothing)


def rolling_extreme(
    source: Sequence[float | None], length: int, kind: Literal["highest", "lowest"]
) -> NullableSeries:
    if length <= 0:
        raise ValueError("length must be positive")
    valid: deque[float] = deque()
    output: list[float | None] = []
    for value in source:
        if _finite(value):
            assert value is not None
            valid.append(value)
            if len(valid) > length:
                valid.popleft()
        if len(valid) < length:
            output.append(None)
        else:
            output.append(max(valid) if kind == "highest" else min(valid))
    return tuple(output)


def confirmed_pivot(
    source: Sequence[float | None],
    *,
    left: int,
    right: int,
    kind: Literal["high", "low"],
) -> tuple[int, float] | None:
    """Return a pivot only on its right-confirmation bar.

    Ties use the common Pine-compatible convention: the candidate may equal a
    left-side observation, but must strictly beat every right-side observation.
    The tie rule is isolated here because it needs genuine TradingView fixtures.
    """

    if left <= 0 or right <= 0:
        raise ValueError("pivot strengths must be positive")
    candidate_index = len(source) - 1 - right
    if candidate_index < left:
        return None
    candidate = source[candidate_index]
    if not _finite(candidate):
        return None
    assert candidate is not None
    left_values = source[candidate_index - left : candidate_index]
    right_values = source[candidate_index + 1 : candidate_index + right + 1]
    if any(not _finite(value) for value in (*left_values, *right_values)):
        return None
    concrete_left = tuple(float(value) for value in left_values if value is not None)
    concrete_right = tuple(float(value) for value in right_values if value is not None)
    if kind == "high":
        matches = all(candidate >= value for value in concrete_left) and all(
            candidate > value for value in concrete_right
        )
    else:
        matches = all(candidate <= value for value in concrete_left) and all(
            candidate < value for value in concrete_right
        )
    return (candidate_index, candidate) if matches else None
