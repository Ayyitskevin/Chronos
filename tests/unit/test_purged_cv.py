"""Purged/embargoed K-fold correctness (C3, ADR-0014 §3).

Pins that test folds are contiguous and partition the index, that the purge+embargo
neighborhood is excluded from train, and that ``requires_purging`` is False for the
fixed-rule replays (nothing is fit in-fold, so the machinery is not applied as theater).
"""

from __future__ import annotations

import pytest

from chronos.research.purged_cv import purged_kfold, requires_purging


def test_test_folds_partition_the_index_contiguously() -> None:
    folds = purged_kfold(20, folds=4)
    assert len(folds) == 4
    covered: list[int] = []
    for fold in folds:
        # Each test fold is a contiguous run of indices.
        assert list(fold.test) == list(range(fold.test[0], fold.test[-1] + 1))
        covered.extend(fold.test)
    # The test folds together partition [0, n) with no gap or overlap.
    assert sorted(covered) == list(range(20))
    assert len(covered) == len(set(covered))


def test_train_excludes_the_test_block() -> None:
    for fold in purged_kfold(20, folds=4):
        assert set(fold.train).isdisjoint(fold.test)


def test_embargo_purges_the_window_after_the_test_block() -> None:
    # Fold 0 tests [0, 5); an embargo of 3 must also drop indices 5, 6, 7 from train.
    fold0 = purged_kfold(20, folds=4, embargo=3)[0]
    assert set(fold0.test) == set(range(0, 5))
    assert {5, 6, 7}.isdisjoint(fold0.train)
    assert 8 in fold0.train  # the embargo does not over-reach


def test_zero_embargo_keeps_all_non_test_indices_in_train() -> None:
    for fold in purged_kfold(20, folds=5, embargo=0):
        expected_train = set(range(20)) - set(fold.test)
        assert set(fold.train) == expected_train


def test_invalid_parameters_are_rejected() -> None:
    with pytest.raises(ValueError):
        purged_kfold(10, folds=1)  # need >= 2 folds
    with pytest.raises(ValueError):
        purged_kfold(3, folds=4)  # need n >= folds
    with pytest.raises(ValueError):
        purged_kfold(10, folds=2, embargo=-1)  # embargo must be >= 0


def test_requires_purging_is_false_for_fixed_rule_replays() -> None:
    # The current strategies estimate nothing inside a fold (Markov matrix is causal).
    assert requires_purging(fits_in_fold=False) is False
    assert requires_purging(fits_in_fold=True) is True
