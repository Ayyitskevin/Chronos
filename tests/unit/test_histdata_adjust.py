"""Read-time corporate-action adjustment tests (AI Quant plan C1-b, ADR-0011 §4).

Golden coverage of the back-adjustment math and every panel-specified guard:
split direction + reverse split, dividend factor exactness, the strictly-before-ex
boundary, split-plus-dividend composition with native-basis dividends (no split-sized
drift — D2), future-dated skip (D3), non-positive/absent-reference rejection (D1),
determinism, and the model's validation + JSON round-trip.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from chronos.histdata import (
    ActionKind,
    AdjustmentError,
    AdjustmentView,
    CorporateAction,
    adjust_series,
)
from chronos.marketdata.bars import Bar, BarInterval, BarSeries


def _bar(d: date, o: float, h: float, low: float, c: float, v: float) -> Bar:
    return Bar(
        symbol="TEST",
        source="ibkr",
        exchange="SMART",
        interval=BarInterval.DAY_1,
        session_date=d,
        timestamp_utc=datetime(d.year, d.month, d.day, 21, 0, tzinfo=UTC),
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
    )


def _series(rows: list[tuple[date, float, float, float, float, float]]) -> BarSeries:
    return BarSeries(
        symbol="TEST",
        interval=BarInterval.DAY_1,
        bars=tuple(_bar(*row) for row in rows),
    )


def _split(ex: date, r: float) -> CorporateAction:
    return CorporateAction(kind=ActionKind.SPLIT, ex_date=ex, value=r, source="test")


def _dividend(ex: date, d: float) -> CorporateAction:
    return CorporateAction(kind=ActionKind.CASH_DIVIDEND, ex_date=ex, value=d, source="test")


# --- RAW is a straight copy -------------------------------------------------


def test_raw_view_is_a_no_op() -> None:
    series = _series([(date(2024, 1, 2), 10, 11, 9, 10, 100)])
    result = adjust_series(series, [_split(date(2024, 1, 3), 2)], AdjustmentView.RAW)
    assert result.series is series
    assert result.warnings == ()


# --- splits -----------------------------------------------------------------


def test_forward_split_quarters_price_and_quadruples_volume_before_ex() -> None:
    ex = date(2024, 1, 4)
    series = _series(
        [
            (date(2024, 1, 2), 400, 404, 396, 400, 100),
            (date(2024, 1, 3), 420, 424, 416, 420, 110),
            (ex, 105, 106, 104, 105, 400),  # ex-date: already post-split, untouched
            (date(2024, 1, 5), 106, 107, 105, 106, 410),
        ]
    )
    for view in (AdjustmentView.SPLIT_ADJUSTED, AdjustmentView.TOTAL_RETURN):
        out = adjust_series(series, [_split(ex, 4)], view).series
        assert out[0].close == pytest.approx(100.0)  # 400 / 4
        assert out[0].volume == pytest.approx(400.0)  # 100 * 4
        assert out[1].close == pytest.approx(105.0)  # 420 / 4
        assert out[1].volume == pytest.approx(440.0)
        assert out[2].close == pytest.approx(105.0)  # ex-date bar unchanged
        assert out[2].volume == pytest.approx(400.0)
        assert out[3].close == pytest.approx(106.0)  # after ex unchanged
        assert out[3].adjusted_close is None


def test_reverse_split_multiplies_price_and_divides_volume() -> None:
    ex = date(2024, 1, 4)
    series = _series(
        [
            (date(2024, 1, 3), 5, 5, 5, 5, 1000),
            (ex, 50, 50, 50, 50, 100),
        ]
    )
    out = adjust_series(series, [_split(ex, 0.1)], AdjustmentView.SPLIT_ADJUSTED).series
    assert out[0].close == pytest.approx(50.0)  # 5 * 10
    assert out[0].volume == pytest.approx(100.0)  # 1000 * 0.1
    assert out[1].close == pytest.approx(50.0)  # ex-date untouched


# --- dividends --------------------------------------------------------------


def test_dividend_adjusts_only_total_return_by_exact_factor() -> None:
    ex = date(2024, 1, 4)
    series = _series(
        [
            (date(2024, 1, 2), 90, 91, 89, 90, 100),
            (date(2024, 1, 3), 100, 101, 99, 100, 110),  # C_ref = 100
            (ex, 98, 99, 97, 98, 120),
            (date(2024, 1, 5), 99, 100, 98, 99, 130),
        ]
    )
    div = _dividend(ex, 2.0)  # factor = 1 - 2/100 = 0.98

    tr = adjust_series(series, [div], AdjustmentView.TOTAL_RETURN).series
    assert tr[0].close == pytest.approx(90 * 0.98)  # 88.2
    assert tr[1].close == pytest.approx(98.0)  # 100 * 0.98
    assert tr[2].close == pytest.approx(98.0)  # ex-date bar unchanged
    assert tr[3].close == pytest.approx(99.0)  # after ex unchanged
    assert tr[0].volume == pytest.approx(100.0)  # dividends never touch volume

    split_view = adjust_series(series, [div], AdjustmentView.SPLIT_ADJUSTED).series
    assert [b.close for b in split_view] == [90, 100, 98, 99]  # dividend ignored


def test_dividend_at_or_above_reference_close_is_rejected() -> None:
    ex = date(2024, 1, 3)
    series = _series(
        [
            (date(2024, 1, 2), 2, 2, 2, 2, 100),  # C_ref = 2
            (ex, 1, 1, 1, 1, 100),
        ]
    )
    result = adjust_series(series, [_dividend(ex, 3.0)], AdjustmentView.TOTAL_RETURN)
    assert [b.close for b in result.series] == [2, 1]  # unchanged, not applied
    assert any("rejected dividend" in w for w in result.warnings)


def test_dividend_with_no_preceding_bar_is_skipped() -> None:
    first = date(2024, 1, 2)
    series = _series([(first, 10, 10, 10, 10, 100), (date(2024, 1, 3), 11, 11, 11, 11, 100)])
    result = adjust_series(series, [_dividend(first, 1.0)], AdjustmentView.TOTAL_RETURN)
    assert [b.close for b in result.series] == [10, 11]
    assert any("no bar before ex-date" in w for w in result.warnings)


# --- composition (D2): native-basis dividend does not drift across a later split ---


def test_dividend_then_later_split_composes_without_split_sized_drift() -> None:
    div_ex = date(2024, 1, 4)
    split_ex = date(2024, 1, 8)
    series = _series(
        [
            (date(2024, 1, 2), 100, 100, 100, 100, 100),  # before both
            (date(2024, 1, 3), 100, 100, 100, 100, 100),  # C_ref for the dividend
            (div_ex, 99, 99, 99, 99, 100),  # dividend ex-date
            (date(2024, 1, 5), 400, 400, 400, 400, 100),  # before split ex (pre-split price)
            (split_ex, 100, 100, 100, 100, 400),  # split ex-date
            (date(2024, 1, 9), 101, 101, 101, 101, 100),
        ]
    )
    actions = [
        _dividend(div_ex, 2.0),
        _split(split_ex, 4),
    ]  # native d=2 against unadjusted C_ref=100
    out = adjust_series(series, actions, AdjustmentView.TOTAL_RETURN).series

    # Before both: 100 * 0.98 (dividend) * 0.25 (split) = 24.5 — the dividend haircut
    # uses the UNADJUSTED C_ref=100, so it is NOT rescaled by the later split.
    assert out[0].close == pytest.approx(24.5)
    assert out[1].close == pytest.approx(24.5)
    # Dividend ex-date bar: only the split applies (not its own dividend): 99 * 0.25.
    assert out[2].close == pytest.approx(24.75)
    # Between dividend and split: only the split: 400 * 0.25 = 100.
    assert out[3].close == pytest.approx(100.0)
    # Split ex-date and after: as-traded.
    assert out[4].close == pytest.approx(100.0)
    assert out[5].close == pytest.approx(101.0)


def test_multiple_actions_same_ex_date_compose_as_a_product() -> None:
    ex = date(2024, 1, 4)
    series = _series([(date(2024, 1, 2), 100, 100, 100, 100, 100), (ex, 25, 25, 25, 25, 100)])
    out = adjust_series(
        series, [_split(ex, 2), _split(ex, 2)], AdjustmentView.SPLIT_ADJUSTED
    ).series
    assert out[0].close == pytest.approx(25.0)  # 100 * (1/2) * (1/2)


# --- future-dated + determinism ---------------------------------------------


def test_future_dated_action_is_skipped_and_newest_bar_stays_as_traded() -> None:
    series = _series(
        [(date(2024, 1, 2), 100, 100, 100, 100, 100), (date(2024, 1, 3), 101, 101, 101, 101, 100)]
    )
    result = adjust_series(series, [_split(date(2024, 2, 1), 4)], AdjustmentView.SPLIT_ADJUSTED)
    assert [b.close for b in result.series] == [100, 101]  # unchanged
    assert any("future-dated" in w for w in result.warnings)


def test_adjustment_is_deterministic() -> None:
    ex = date(2024, 1, 4)
    series = _series(
        [
            (date(2024, 1, 2), 400, 400, 400, 400, 100),
            (ex, 100, 100, 100, 100, 400),
        ]
    )
    actions = [_split(ex, 4)]
    a = adjust_series(series, actions, AdjustmentView.TOTAL_RETURN)
    b = adjust_series(series, actions, AdjustmentView.TOTAL_RETURN)
    assert [bar.close for bar in a.series] == [bar.close for bar in b.series]
    assert a.warnings == b.warnings


def test_rounding_to_non_positive_price_fails_closed() -> None:
    # A deeply back-adjusted sub-microdollar price would round to 0.0; the view must
    # refuse rather than emit a non-positive price (ADR-0011 §11.10 / D1).
    ex = date(2024, 1, 3)
    tiny = 4e-7
    series = _series([(date(2024, 1, 2), tiny, tiny, tiny, tiny, 100), (ex, 1, 1, 1, 1, 100)])
    with pytest.raises(AdjustmentError):
        adjust_series(series, [_split(ex, 2)], AdjustmentView.SPLIT_ADJUSTED)


# --- model validation + round-trip ------------------------------------------


def test_corporate_action_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        CorporateAction(kind=ActionKind.SPLIT, ex_date=date(2024, 1, 1), value=0.0, source="x")
    with pytest.raises(ValueError):
        CorporateAction(
            kind=ActionKind.CASH_DIVIDEND, ex_date=date(2024, 1, 1), value=-1.0, source="x"
        )
    with pytest.raises(ValueError):
        CorporateAction(kind=ActionKind.SPLIT, ex_date=date(2024, 1, 1), value=2.0, source="")


def test_corporate_action_json_round_trip() -> None:
    action = CorporateAction(
        kind=ActionKind.CASH_DIVIDEND,
        ex_date=date(2024, 3, 15),
        value=0.24,
        source="ibkr:reqContractDetails",
        note="quarterly",
    )
    assert CorporateAction.from_mapping(action.to_mapping()) == action


def test_corporate_action_from_mapping_fails_closed() -> None:
    with pytest.raises(ValueError):
        CorporateAction.from_mapping({"kind": "not_a_kind", "ex_date": "2024-01-01", "value": 1})
    with pytest.raises(ValueError):
        CorporateAction.from_mapping({"kind": "split", "ex_date": "nope", "value": 1})
