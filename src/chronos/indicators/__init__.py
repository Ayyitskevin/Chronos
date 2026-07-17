"""Deterministic indicator library with Pine Script v5/v6 `ta.*` semantics.

Each function consumes Python sequences (oldest first) and returns a list the
same length as the input where positions before the warm-up period are
``None``, mirroring Pine ``na`` propagation. Math is IEEE-754 float64,
matching Pine's numeric model (ADR-0005). Parity fixtures in
``tests/parity`` pin these functions against hand-computed and
spec-derived reference values.
"""

from chronos.indicators.core import (
    atr,
    change,
    crossover,
    crossunder,
    ema,
    highest,
    lowest,
    percentrank,
    rma,
    roc,
    rsi,
    sma,
    stdev,
    true_range,
)

__all__ = [
    "atr",
    "change",
    "crossover",
    "crossunder",
    "ema",
    "highest",
    "lowest",
    "percentrank",
    "rma",
    "roc",
    "rsi",
    "sma",
    "stdev",
    "true_range",
]
