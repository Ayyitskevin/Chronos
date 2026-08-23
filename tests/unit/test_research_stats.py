"""Sample-honest statistics tests (C3, ADR-0014 §1).

Pins the normal CDF/inverse, PSR (→0.5 at the benchmark, rises with n), DSR (falls as
the trial count rises — the deflation property), the seeded block bootstrap
(deterministic, widens with block size), small-sample fail-soft behavior, and CSCV/PBO
(informationless ≈ 0.5, a real edge → 0, a reversing edge → high, and the sign of the
logit) — the properties that fail if the estimator is inverted, not merely changed.
"""

from __future__ import annotations

import random
import statistics

import pytest

from chronos.research.stats import (
    block_bootstrap_ci,
    deflated_sharpe,
    expected_max_sharpe,
    moments,
    norm_cdf,
    norm_ppf,
    probabilistic_sharpe,
    probability_of_backtest_overfitting,
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


def _noise(trials: int, obs: int, seed: int) -> list[list[float]]:
    """``trials`` independent zero-edge return series — an informationless search."""

    rng = random.Random(seed)
    return [[rng.gauss(0.0, 0.01) for _ in range(obs)] for _ in range(trials)]


def test_pbo_is_none_for_inputs_it_cannot_support() -> None:
    """Fail soft, never a falsely precise number (module contract)."""

    assert probability_of_backtest_overfitting([[0.1] * 32], splits=4) is None  # <2 trials
    assert probability_of_backtest_overfitting(_noise(4, 32, 1), splits=5) is None  # odd
    assert probability_of_backtest_overfitting(_noise(4, 32, 1), splits=1) is None  # <2
    assert probability_of_backtest_overfitting(_noise(4, 4, 1), splits=8) is None  # T<S
    ragged = [[0.1] * 32, [0.1] * 30]
    assert probability_of_backtest_overfitting(ragged, splits=4) is None


def test_pbo_of_an_informationless_search_sits_near_one_half() -> None:
    """The paper's baseline: uniform ω ⇒ standard-normal logits ⇒ φ ≈ 0.5.

    Averaged over seeds deliberately. A single informationless run ranges roughly
    0.15 to 0.95 because the combinations overlap heavily, so a one-seed assertion here
    would be a flake generator rather than a check. A mild upward bias is expected and
    shrinks as N and T grow: in-sample and out-of-sample partition one *fixed*
    realization, so an unusually strong IS half implies a weaker OOS complement.
    """

    values = [
        report.pbo
        for seed in range(1, 21)
        if (report := probability_of_backtest_overfitting(_noise(8, 240, seed), splits=8))
    ]
    assert len(values) == 20
    assert 0.40 <= statistics.mean(values) <= 0.65


def test_pbo_collapses_to_zero_when_one_trial_has_a_real_edge() -> None:
    """A genuine edge is selected IS and *stays* best OOS — the non-overfit case."""

    trials = _noise(10, 320, 11)
    rng = random.Random(99)
    trials[3] = [0.004 + rng.gauss(0.0, 0.01) for _ in range(320)]
    report = probability_of_backtest_overfitting(trials, splits=8)
    assert report is not None
    assert report.pbo == pytest.approx(0.0, abs=0.05)
    # High logits mean IS/OOS consistency — the paper's direction, pinned explicitly so
    # an inverted comparison cannot pass this suite.
    assert report.median_logit > 0.0


def test_pbo_is_high_when_the_in_sample_winner_reverses_out_of_sample() -> None:
    """The overfitting case: the IS winner wins by a regime that flips.

    This is the test that fails if the rank direction is inverted — the two edge cases
    above and this one cannot both pass under a sign error.
    """

    obs, half = 320, 160
    rng = random.Random(5)
    trials = [
        [(0.004 if i < half else -0.004) + rng.gauss(0.0, 0.008) for i in range(obs)]
        if k == 0
        else [rng.gauss(0.0, 0.008) for _ in range(obs)]
        for k in range(10)
    ]
    report = probability_of_backtest_overfitting(trials, splits=8)
    assert report is not None
    assert report.pbo > 0.6
    assert report.median_logit < 0.0


def test_pbo_reports_truncation_rather_than_dropping_rows_silently() -> None:
    """T not divisible by splits: the remainder is dropped, and the report says so."""

    report = probability_of_backtest_overfitting(_noise(6, 251, 3), splits=8)
    assert report is not None
    assert report.observations_used == 248  # 31 per block, 8 blocks
    assert report.observations_dropped == 3
    assert report.combinations == 70  # C(8, 4)
    assert report.trials == 6


def test_pbo_skips_combinations_whose_statistic_is_undefined() -> None:
    """A configuration that never moves cannot be ranked, so nothing is invented.

    Sharpe is undefined at zero variance. Rather than rank an unrankable trial (a flat
    series with a positive mean is *excellent*, not worst), the combination is skipped
    and counted; when nothing is scorable the estimate is ``None``, not a number.
    """

    trials = _noise(4, 64, 2)
    trials[1] = [0.0] * 64  # never trades
    assert probability_of_backtest_overfitting(trials, splits=8) is None


def test_pbo_is_deterministic() -> None:
    trials = _noise(6, 128, 17)
    assert probability_of_backtest_overfitting(trials, splits=8) == (
        probability_of_backtest_overfitting(trials, splits=8)
    )
