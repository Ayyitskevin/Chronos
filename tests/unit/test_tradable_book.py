from __future__ import annotations

import pytest

from chronos.autonomy import book as autonomy_book
from chronos.research.features import universe as research_universe
from chronos.research.features.models import FeatureInputError, FeaturePolicy


def test_autonomy_and_research_agree_on_the_locked_book() -> None:
    assert (
        autonomy_book.TRADABLE_SYMBOLS
        == research_universe.TRADABLE_SYMBOLS
        == (
            "GLD",
            "IWM",
            "QQQ",
        )
    )
    assert autonomy_book.COMPANION_ONLY_SYMBOLS == research_universe.COMPANION_ONLY_SYMBOLS
    assert autonomy_book.RESEARCH_PROXY == research_universe.RESEARCH_PROXY == {}
    assert autonomy_book.BOOK_SCHEMA == research_universe.BOOK_SCHEMA
    assert autonomy_book.book_digest() == research_universe.book_digest()
    assert len(research_universe.book_digest()) == 64
    assert research_universe.research_series_for("QQQ") == "QQQ"
    assert research_universe.research_series_for("GLD") == "GLD"
    assert research_universe.research_series_for("IWM") == "IWM"
    assert autonomy_book.is_tradable("gld")
    assert autonomy_book.is_tradable("IWM")
    assert autonomy_book.is_tradable("QQQ")
    assert not autonomy_book.is_tradable("QQQM")
    assert not autonomy_book.is_tradable("SPY")


def test_feature_policy_refuses_a_different_book() -> None:
    with pytest.raises(FeatureInputError, match="locked to GLD, IWM, and QQQ"):
        FeaturePolicy(tradable_symbols=("GLD", "IWM", "QQQM"))
    with pytest.raises(FeatureInputError, match="locked"):
        FeaturePolicy(companion_only_symbols=("QQQ", "RSP", "SPY", "VIX", "VIX3M"))


def test_feature_policy_digest_includes_the_locked_book() -> None:
    policy = FeaturePolicy()
    assert policy.tradable_symbols == research_universe.TRADABLE_SYMBOLS
    assert policy.digest == FeaturePolicy().digest
