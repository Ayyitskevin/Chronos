"""Deterministic synthetic-series tests for the Pine-derived regime context."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from chronos.services.regime_context import (
    MIN_BARS,
    RegimeContext,
    TrendState,
    compute_regime_context,
    compute_regime_context_from_closes,
)

AS_OF = date(2026, 7, 16)


def rising_closes(count: int) -> list[float]:
    return [100.0 * (1.001**i) for i in range(count)]


def falling_closes(count: int) -> list[float]:
    return [300.0 * (0.999**i) for i in range(count)]


def test_rising_series_is_uptrend_with_rsi_near_100() -> None:
    context = compute_regime_context_from_closes("spy", rising_closes(300), as_of_session=AS_OF)
    assert context.symbol == "SPY"
    assert context.available is True
    assert context.bars == 300
    assert context.as_of_session == AS_OF
    assert context.trend_state is TrendState.UPTREND
    assert context.ema_fast is not None and context.ema_slow is not None
    assert context.ema_fast > context.ema_slow
    assert context.rsi2 is not None
    assert context.rsi2 >= Decimal("99")
    assert context.vol_percentile is not None
    assert Decimal("0") <= context.vol_percentile <= Decimal("1")
    assert context.reasons


def test_falling_series_is_downtrend_with_rsi_near_0() -> None:
    context = compute_regime_context_from_closes("SPY", falling_closes(300), as_of_session=AS_OF)
    assert context.available is True
    assert context.trend_state is TrendState.DOWNTREND
    assert context.ema_fast is not None and context.ema_slow is not None
    assert context.ema_fast < context.ema_slow
    assert context.rsi2 is not None
    assert context.rsi2 <= Decimal("1")


def test_fewer_than_min_bars_is_unavailable() -> None:
    closes = rising_closes(MIN_BARS - 1)
    context = compute_regime_context_from_closes("SPY", closes, as_of_session=AS_OF)
    assert context.available is False
    assert context.bars == MIN_BARS - 1
    assert context.trend_state is None
    assert context.ema_fast is None
    assert context.rsi2 is None
    assert context.vol_percentile is None
    assert any(str(MIN_BARS) in reason for reason in context.reasons)


def test_non_finite_or_non_positive_closes_are_unavailable() -> None:
    closes = rising_closes(300)
    closes[150] = float("nan")
    context = compute_regime_context_from_closes("SPY", closes, as_of_session=AS_OF)
    assert context.available is False
    assert context.reasons


def test_missing_file_is_unavailable_with_reason(tmp_path: Path) -> None:
    context = compute_regime_context("msft", tmp_path)
    assert context.symbol == "MSFT"
    assert context.available is False
    assert context.bars == 0
    assert "no local daily history for MSFT" in context.reasons


def test_unparseable_file_is_unavailable_never_raises(tmp_path: Path) -> None:
    (tmp_path / "SPY.csv").write_text("not,a,daily,bar,file\n1,2,3,4,5\n", encoding="utf-8")
    context = compute_regime_context("SPY", tmp_path)
    assert context.available is False
    assert any("strict CSV load" in reason for reason in context.reasons)


def test_csv_backed_computation_round_trip(tmp_path: Path) -> None:
    closes = rising_closes(300)
    start = date(2025, 1, 1)
    rows = ["date,open,high,low,close,volume"]
    rows.extend(
        f"{start + timedelta(days=i)},{close:.4f},{close:.4f},{close:.4f},{close:.4f},1000"
        for i, close in enumerate(closes)
    )
    (tmp_path / "SPY.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    context = compute_regime_context("SPY", tmp_path)
    assert context.available is True
    assert context.bars == 300
    assert context.as_of_session == start + timedelta(days=299)
    assert context.trend_state is TrendState.UPTREND


def test_note_always_carries_the_disclaimer() -> None:
    available = compute_regime_context_from_closes("SPY", rising_closes(300), as_of_session=AS_OF)
    unavailable = compute_regime_context_from_closes("SPY", [1.0, 2.0], as_of_session=AS_OF)
    for context in (available, unavailable):
        assert "not a validated signal" in context.note
        assert "heuristic context — not a validated signal" in context.note


def test_same_input_twice_is_identical() -> None:
    closes = rising_closes(300)
    first = compute_regime_context_from_closes("SPY", closes, as_of_session=AS_OF)
    second = compute_regime_context_from_closes("SPY", list(closes), as_of_session=AS_OF)
    assert first == second
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_decimals_serialize_as_strings_in_json_mode() -> None:
    context = compute_regime_context_from_closes("SPY", rising_closes(300), as_of_session=AS_OF)
    payload = context.model_dump(mode="json")
    assert isinstance(payload["ema_fast"], str)
    assert isinstance(payload["ema_slow"], str)
    assert isinstance(payload["rsi2"], str)
    assert isinstance(payload["vol_percentile"], str)
    assert payload["as_of_session"] == AS_OF.isoformat()
    assert payload["trend_state"] == "UPTREND"


def test_model_requires_disclaimer_and_metric_consistency() -> None:
    with pytest.raises(ValueError, match="disclaimer"):
        RegimeContext(
            symbol="SPY",
            available=False,
            bars=0,
            note="all clear",
            reasons=("x",),
        )
