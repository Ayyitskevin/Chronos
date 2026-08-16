from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.fixtures.features.bars import START, daily_bars

from chronos.research.features.alignment import align_companions, latest_companion
from chronos.research.features.models import (
    GOLD_INERT_FAMILIES,
    FeatureFamily,
    FeatureInputError,
    FeaturePolicy,
)


def test_feature_policy_digest_is_stable_and_content_addressed() -> None:
    first = FeaturePolicy()
    second = FeaturePolicy()
    changed = FeaturePolicy(tail_kurtosis_fat=4.0)
    assert first.digest == second.digest
    assert first.digest != changed.digest
    assert len(first.digest) == 64


def test_feature_policy_refuses_ready_companion_catalog() -> None:
    with pytest.raises(FeatureInputError, match="pending"):
        FeaturePolicy(companion_catalog_status="ready")


def test_feature_policy_locks_gld_inert_equity_weather() -> None:
    policy = FeaturePolicy()
    assert policy.gold_inert_families == GOLD_INERT_FAMILIES
    assert FeatureFamily.IV_REGIME not in policy.enabled_families("GLD")
    assert FeatureFamily.BREADTH not in policy.enabled_families("GLD")
    assert FeatureFamily.IV_REGIME in policy.enabled_families("QQQ")
    with pytest.raises(FeatureInputError, match="inert"):
        FeaturePolicy(gold_inert_families=())
    with pytest.raises(FeatureInputError, match="required"):
        policy.enabled_families(" ")


def test_latest_companion_uses_at_or_before_and_refuses_future() -> None:
    primary = daily_bars("AAA", count=3).bars[1]
    companion = daily_bars("SPY", count=3)
    chosen = latest_companion(primary, companion, label="spy", allow_equal=True)
    assert chosen is not None
    assert chosen.timestamp_utc == primary.timestamp_utc
    prior = latest_companion(primary, companion, label="vix", allow_equal=False)
    assert prior is not None
    assert prior.timestamp_utc < primary.timestamp_utc


def test_align_companions_ignores_future_companions() -> None:
    primary = daily_bars("AAA", count=2)
    future = daily_bars("VIX", count=1, start=START + timedelta(days=10))
    aligned = align_companions(primary.bars, {"vix": future}, allow_equal={"vix": False})
    assert all(row["vix"] is None for row in aligned)


def test_align_companions_is_deterministic() -> None:
    primary = daily_bars("AAA", count=4)
    spy = daily_bars("SPY", count=4, step=0.2)
    first = align_companions(primary.bars, {"spy": spy})
    second = align_companions(primary.bars, {"spy": spy})
    assert [row["spy"].close if row["spy"] else None for row in first] == [
        row["spy"].close if row["spy"] else None for row in second
    ]
    assert first[0]["spy"] is not None
    assert first[0]["spy"].timestamp_utc == datetime(2020, 1, 2, 21, tzinfo=UTC)
