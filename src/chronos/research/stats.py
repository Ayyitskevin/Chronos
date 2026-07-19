"""Sample-honest statistics for research verdicts (AI Quant plan C3, ADR-0014 §1).

Pure, deterministic, **stdlib only** (normal CDF via ``math.erf``; inverse normal via
Acklam's rational approximation; a *seeded* ``random.Random`` for the bootstrap) — no
numpy/scipy, matching ``backtest.metrics``. Every function fails soft (returns ``None``)
below the sample size it can support rather than emitting a falsely precise number.

Provides the Probabilistic Sharpe Ratio (PSR), the Deflated Sharpe Ratio (DSR) — PSR
against the expected maximum Sharpe of ``N`` trials (Bailey & López de Prado) — and a
**stationary block bootstrap** over a return series (geometric block lengths, circular
wrap) that preserves autocorrelation, deliberately *not* IID trade-level resampling.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

_EULER_MASCHERONI = 0.5772156649015329


@dataclass(frozen=True, slots=True)
class Moments:
    n: int
    mean: float
    std: float  # population standard deviation
    skew: float
    kurtosis: float  # non-excess (normal == 3.0)


def moments(returns: Sequence[float]) -> Moments | None:
    """Population mean/std/skew/(non-excess)kurtosis; ``None`` if fewer than 2 points."""

    n = len(returns)
    if n < 2:
        return None
    mean = math.fsum(returns) / n
    var = math.fsum((r - mean) ** 2 for r in returns) / n
    std = math.sqrt(var)
    if std == 0.0:
        return Moments(n=n, mean=mean, std=0.0, skew=0.0, kurtosis=3.0)
    skew = math.fsum((r - mean) ** 3 for r in returns) / n / std**3
    kurtosis = math.fsum((r - mean) ** 4 for r in returns) / n / std**4
    return Moments(n=n, mean=mean, std=std, skew=skew, kurtosis=kurtosis)


def sharpe(returns: Sequence[float]) -> float | None:
    """Per-observation Sharpe (mean/std, no annualization); ``None`` if undefined."""

    m = moments(returns)
    if m is None or m.std == 0.0:
        return None
    return m.mean / m.std


def total_return(returns: Sequence[float]) -> float:
    """Compounded total return of a per-bar simple-return series."""

    growth = 1.0
    for r in returns:
        growth *= 1.0 + r
    return growth - 1.0


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF (Acklam's approximation, |err| < 1.2e-9)."""

    if not 0.0 < p < 1.0:
        raise ValueError(f"norm_ppf requires 0 < p < 1, got {p!r}")
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )
    p_low = 0.02425
    p_high = 1.0 - p_low
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
    )


def probabilistic_sharpe(
    observed_sharpe: float,
    n_obs: int,
    *,
    benchmark: float = 0.0,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float | None:
    """P(true per-obs Sharpe > benchmark) given sample length and non-normality."""

    if n_obs < 2:
        return None
    variance = 1.0 - skew * observed_sharpe + (kurtosis - 1.0) / 4.0 * observed_sharpe**2
    if variance <= 0.0:
        return None
    z = (observed_sharpe - benchmark) * math.sqrt(n_obs - 1) / math.sqrt(variance)
    return norm_cdf(z)


def expected_max_sharpe(trial_count: int, trial_sharpe_variance: float) -> float:
    """Expected maximum per-obs Sharpe of ``trial_count`` independent trials."""

    if trial_count <= 1 or trial_sharpe_variance <= 0.0:
        return 0.0
    z1 = norm_ppf(1.0 - 1.0 / trial_count)
    z2 = norm_ppf(1.0 - 1.0 / (trial_count * math.e))
    return math.sqrt(trial_sharpe_variance) * (
        (1.0 - _EULER_MASCHERONI) * z1 + _EULER_MASCHERONI * z2
    )


def deflated_sharpe(
    observed_sharpe: float,
    n_obs: int,
    *,
    trial_count: int,
    trial_sharpe_variance: float,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float | None:
    """PSR against the expected-maximum Sharpe of ``trial_count`` trials (DSR)."""

    benchmark = expected_max_sharpe(trial_count, trial_sharpe_variance)
    return probabilistic_sharpe(
        observed_sharpe, n_obs, benchmark=benchmark, skew=skew, kurtosis=kurtosis
    )


def block_bootstrap_ci(
    returns: Sequence[float],
    statistic: Callable[[Sequence[float]], float | None],
    *,
    block_size: int,
    n_resamples: int,
    seed: int,
    alpha: float = 0.05,
) -> tuple[float, float] | None:
    """Percentile CI for ``statistic`` via a seeded stationary block bootstrap.

    Blocks have geometric length (mean ``block_size``) and wrap circularly, preserving
    autocorrelation. Deterministic in ``seed``. ``None`` below 2 observations.
    """

    n = len(returns)
    if n < 2 or block_size < 1:
        return None
    rng = random.Random(seed)
    p_end = 1.0 / block_size
    estimates: list[float] = []
    for _ in range(n_resamples):
        sample: list[float] = []
        while len(sample) < n:
            index = rng.randrange(n)
            while len(sample) < n:
                sample.append(returns[index % n])
                index += 1
                if rng.random() < p_end:
                    break
        value = statistic(sample[:n])
        if value is not None:
            estimates.append(value)
    if not estimates:
        return None
    estimates.sort()
    lo = estimates[int((alpha / 2.0) * (len(estimates) - 1))]
    hi = estimates[int((1.0 - alpha / 2.0) * (len(estimates) - 1))]
    return lo, hi
