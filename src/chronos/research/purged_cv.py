"""Purged/embargoed K-fold CV — a capability for FITTED workflows (ADR-0014 §3).

López de Prado purged K-fold: contiguous test folds with the test neighborhood purged
from train and an embargo after it, so a fitted model/parameter search cannot leak across
the fold boundary of an autocorrelated series.

**It does not bind for the current fixed-rule strategies** — they estimate nothing inside
a fold (the Markov matrix is accumulated causally, already walk-forward by construction),
so there is nothing to purge. `requires_purging` returns False for them and the
walk-forward records "not applicable" rather than running the machinery as decoration.
Shipped tested so a real parameter search (C4) or fitted family can switch it on.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Fold:
    train: tuple[int, ...]
    test: tuple[int, ...]


def purged_kfold(n: int, *, folds: int, embargo: int = 0) -> tuple[Fold, ...]:
    """Contiguous test folds; train excludes the test block and an ``embargo`` after it."""

    if folds < 2 or n < folds:
        raise ValueError(f"purged_kfold needs folds >= 2 and n >= folds (got n={n}, folds={folds})")
    if embargo < 0:
        raise ValueError("embargo must be >= 0")
    bounds = [round(i * n / folds) for i in range(folds + 1)]
    out: list[Fold] = []
    for k in range(folds):
        test_start, test_end = bounds[k], bounds[k + 1]
        test = tuple(range(test_start, test_end))
        # Purge the test block itself and an embargo window immediately after it from train.
        blocked_end = min(n, test_end + embargo)
        train = tuple(i for i in range(n) if i < test_start or i >= blocked_end)
        out.append(Fold(train=train, test=test))
    return tuple(out)


def requires_purging(fits_in_fold: bool) -> bool:
    """Whether a workflow needs purged CV. False for fixed-rule replays (nothing fit)."""

    return fits_in_fold
