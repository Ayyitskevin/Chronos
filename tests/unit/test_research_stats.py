"""Sample-honest statistics tests (C3, ADR-0014 §1).

Pins the normal CDF/inverse, PSR (→0.5 at the benchmark, rises with n), DSR (falls as
the trial count rises — the deflation property), the seeded block bootstrap
(deterministic, widens with block size), and small-sample fail-soft behavior.
"""

from __future__ import annotations

import pytest

from chronos.research.stats import (
    block_bootstrap_ci,
    deflated_sharpe,
    expected_max_sharpe,
    moments,
    norm_cdf,
    norm_ppf,
    probabilistic_sharpe,
    sharpe,
    total_return,
)


def test_normal_cdf_and_inverse() -> None:
    assert norm_cdf(0.0) == pytest.approx(0.5)
    assert norm_cdf(1.959964) == pytest.approx(0.975, abs=1e-4)
    assert norm_ppf(0.975) == pytest.approx(1.959964, abs=1e-4)
    assert norm_ppf(0.5) == pytest.approx(0.0, abs=1e-9)


def test_psr_is_half_at_the_benchmark_and_rises_with_n() -> None:
    assert probabilistic_sharpe(0.1, 100, benchmark=0.1) == pytest.approx(0.5)
    weak = probabilistic_sharpe(0.1, 100)
    strong = probabilistic_sharpe(0.1, 1000)
    assert weak is not None and strong is not None
    assert 0.5 < weak < strong < 1.0


def test_deflated_sharpe_falls_as_trials_rise() -> None:
    # More trials → higher expected-max benchmark → lower deflated Sharpe.
    v = 0.01
    assert expected_max_sharpe(1, v) == 0.0  # a single trial: no deflation
    assert expected_max_sharpe(10, v) < expected_max_sharpe(100, v)
    dsr_1 = deflated_sharpe(0.15, 100, trial_count=1, trial_sharpe_variance=v)
    dsr_50 = deflated_sharpe(0.15, 100, trial_count=50, trial_sharpe_variance=v)
    assert dsr_1 is not None and dsr_50 is not None
    assert dsr_1 > dsr_50


def test_moments_and_sharpe() -> None:
    assert moments([1.0]) is None  # fail soft below 2
    m = moments([-2.0, -1.0, 0.0, 1.0, 2.0])
    assert m is not None
    assert m.skew == pytest.approx(0.0)  # symmetric
    assert sharpe([0.01, 0.01, 0.01]) is None  # zero variance → undefined
    assert total_return([0.1, -0.1]) == pytest.approx(1.1 * 0.9 - 1.0)  # compounded, not additive


def test_block_bootstrap_is_deterministic() -> None:
    returns = [0.01, -0.02, 0.03, -0.01, 0.02, -0.03, 0.01, 0.0, 0.02, -0.01] * 6
    a = block_bootstrap_ci(returns, sharpe, block_size=5, n_resamples=400, seed=7)
    b = block_bootstrap_ci(returns, sharpe, block_size=5, n_resamples=400, seed=7)
    assert a is not None and a == b  # same seed → identical CI (recorded-seed determinism)
    c = block_bootstrap_ci(returns, sharpe, block_size=5, n_resamples=400, seed=8)
    assert c is not None and c != a  # a different seed genuinely resamples


def test_block_bootstrap_preserves_autocorrelation() -> None:
    # A stationary block bootstrap must widen the CI on a *positively* autocorrelated series
    # relative to block=1 (near-IID), because larger blocks retain the persistence that IID
    # resampling destroys. (On a mean-reverting series the effect reverses — so this is a
    # property of preserving dependence, not a universal "bigger block = wider" law.)
    persistent = [0.02] * 30 + [-0.02] * 30  # two long same-sign regimes
    iid = block_bootstrap_ci(persistent, sharpe, block_size=1, n_resamples=800, seed=7)
    blocked = block_bootstrap_ci(persistent, sharpe, block_size=15, n_resamples=800, seed=7)
    assert iid is not None and blocked is not None
    assert (blocked[1] - blocked[0]) > (iid[1] - iid[0])


def test_small_sample_fails_soft() -> None:
    assert probabilistic_sharpe(0.1, 1) is None
    assert block_bootstrap_ci([0.01], sharpe, block_size=1, n_resamples=10, seed=1) is None
    with pytest.raises(ValueError):
        norm_ppf(0.0)
