"""Hand-computed reference values for the core indicator library.

Every expected value below is derived by hand with exact arithmetic and the
derivation is documented next to the assertion. Where the arithmetic is exact
in binary floating point (sums/halves of dyadic rationals) we assert exact
equality; otherwise we use pytest.approx with a tight tolerance.
"""

from __future__ import annotations

import pytest

from chronos.indicators.core import (
    atr,
    ema,
    highest,
    lowest,
    percentrank,
    rma,
    rsi,
    sma,
    stdev,
)

APPROX = 1e-12


class TestSma:
    def test_sma_length_two(self) -> None:
        # windows: [1,2] -> 1.5, [2,3] -> 2.5, [3,4] -> 3.5
        assert sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]

    def test_sma_length_three(self) -> None:
        # windows: [2,4,6] -> 4, [4,6,8] -> 6, [6,8,10] -> 8
        assert sma([2, 4, 6, 8, 10], 3) == [None, None, 4.0, 6.0, 8.0]

    def test_sma_warmup_none_prefix(self) -> None:
        out = sma([5.0] * 6, 4)
        assert out[:3] == [None, None, None]
        assert out[3:] == [5.0, 5.0, 5.0]

    def test_sma_rejects_non_positive_length(self) -> None:
        with pytest.raises(ValueError):
            sma([1.0], 0)


class TestEma:
    def test_ema_length_three_hand_computed(self) -> None:
        # alpha = 2/(3+1) = 0.5; seed at index 2 with SMA(3) of [1,2,3] = 2.
        # index 3: 0.5*4 + 0.5*2 = 3;  index 4: 0.5*5 + 0.5*3 = 4.
        assert ema([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]

    def test_ema_seed_equals_sma_of_first_length_values(self) -> None:
        source = [10.0, 12.0, 11.0, 13.0, 12.5]
        length = 4
        out = ema(source, length)
        assert out[: length - 1] == [None] * (length - 1)
        # seed = (10 + 12 + 11 + 13) / 4 = 11.5
        assert out[length - 1] == 11.5

    def test_ema_shorter_than_length_is_all_none(self) -> None:
        assert ema([1.0, 2.0], 3) == [None, None]


class TestRma:
    def test_rma_length_two_hand_computed(self) -> None:
        # alpha = 1/2; seed at index 1 with SMA(2) of [1,2] = 1.5.
        # index 2: 0.5*3 + 0.5*1.5 = 2.25
        # index 3: 0.5*4 + 0.5*2.25 = 3.125
        # index 4: 0.5*5 + 0.5*3.125 = 4.0625
        assert rma([1, 2, 3, 4, 5], 2) == [None, 1.5, 2.25, 3.125, 4.0625]

    def test_rma_seed_equals_sma_of_first_length_values(self) -> None:
        # seed at index 2 = (3 + 6 + 9)/3 = 6
        assert rma([3, 6, 9], 3) == [None, None, 6.0]


class TestRsi:
    def test_rsi_all_gains_window_is_100(self) -> None:
        # deltas: +1, +1, +1 -> losses all zero, gains positive -> RSI = 100.
        # First value appears at index `length` (=2): the leading synthetic
        # zero delta at index 0 is excluded, so the RMA seeds on deltas 1..2.
        assert rsi([1, 2, 3, 4], 2) == [None, None, 100.0, 100.0]

    def test_rsi_flat_window_is_50(self) -> None:
        # deltas all zero -> avg gain == avg loss == 0 -> implementation
        # defines RSI = 50.0 for the gain == loss == 0 case.
        assert rsi([5, 5, 5, 5], 2) == [None, None, 50.0, 50.0]

    def test_rsi_mixed_hand_computed(self) -> None:
        # source = [10, 11, 10.5, 11.5]; deltas: +1, -0.5, +1
        # gains (from first delta): [1, 0, 1]; losses: [0, 0.5, 0]
        # avg_gain = rma([1,0,1], 2): seed (1+0)/2 = 0.5; then 0.5*1+0.5*0.5 = 0.75
        # avg_loss = rma([0,0.5,0], 2): seed (0+0.5)/2 = 0.25; then 0.5*0+0.5*0.25 = 0.125
        # rsi[2] = 100 - 100/(1 + 0.5/0.25)  = 100 - 100/3 = 66.666...
        # rsi[3] = 100 - 100/(1 + 0.75/0.125) = 100 - 100/7 = 85.714...
        out = rsi([10.0, 11.0, 10.5, 11.5], 2)
        assert out[0] is None and out[1] is None
        assert out[2] == pytest.approx(100.0 - 100.0 / 3.0, abs=APPROX)
        assert out[3] == pytest.approx(100.0 - 100.0 / 7.0, abs=APPROX)

    def test_rsi_all_losses_window_is_0(self) -> None:
        # deltas: -1, -1, -1 -> gains all zero, losses positive -> rs = 0 -> RSI 0.
        assert rsi([4, 3, 2, 1], 2) == [None, None, 0.0, 0.0]


class TestAtr:
    def test_atr_hand_computed(self) -> None:
        highs = [10.0, 12.0, 11.0]
        lows = [9.0, 10.5, 9.5]
        closes = [9.5, 11.0, 10.0]
        # TR[0] = high-low = 10-9 = 1
        # TR[1] = max(12-10.5, |12-9.5|, |10.5-9.5|) = max(1.5, 2.5, 1.0) = 2.5
        # TR[2] = max(11-9.5, |11-11|, |9.5-11|)     = max(1.5, 0.0, 1.5) = 1.5
        # atr = rma([1, 2.5, 1.5], 2):
        #   seed at index 1: (1+2.5)/2 = 1.75
        #   index 2: 0.5*1.5 + 0.5*1.75 = 1.625
        assert atr(highs, lows, closes, 2) == [None, 1.75, 1.625]

    def test_atr_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            atr([1.0, 2.0], [0.5], [0.9, 1.5], 2)


class TestStdev:
    def test_stdev_population_hand_computed(self) -> None:
        # Population stdev (divide by n), matching TradingView:
        # [1,2]: mean 1.5, var (0.25+0.25)/2 = 0.25 -> 0.5 (same for [2,3], [3,4])
        assert stdev([1, 2, 3, 4], 2) == [None, 0.5, 0.5, 0.5]

    def test_stdev_varying_windows(self) -> None:
        # [2,4]: mean 3, var (1+1)/2 = 1 -> 1;  [4,8]: mean 6, var (4+4)/2 = 4 -> 2
        assert stdev([2, 4, 8], 2) == [None, 1.0, 2.0]

    def test_stdev_warmup_none_prefix(self) -> None:
        out = stdev([1.0, 2.0, 3.0, 4.0, 5.0], 4)
        assert out[:3] == [None, None, None]


class TestPercentRank:
    def test_percentrank_excludes_current_bar(self) -> None:
        # window is the PREVIOUS `length` values, current value excluded.
        # i=3: window [1,3,2], value 5 -> all 3 <= 5 -> 100.0
        # i=4: window [3,2,5], value 4 -> {3,2} <= 4 -> 2/3 * 100
        out = percentrank([1, 3, 2, 5, 4], 3)
        assert out[:3] == [None, None, None]
        assert out[3] == 100.0
        assert out[4] == pytest.approx(200.0 / 3.0, abs=APPROX)

    def test_percentrank_lowest_value_ranks_zero(self) -> None:
        # i=2: window [5,4], value 1 -> none <= 1 -> 0.0
        out = percentrank([5, 4, 1], 2)
        assert out == [None, None, 0.0]


class TestHighestLowest:
    def test_highest_hand_computed(self) -> None:
        # windows incl. current bar: [1,3]->3, [3,2]->3, [2,5]->5, [5,4]->5
        assert highest([1, 3, 2, 5, 4], 2) == [None, 3, 3, 5, 5]

    def test_lowest_hand_computed(self) -> None:
        # windows incl. current bar: [1,3]->1, [3,2]->2, [2,5]->2, [5,4]->4
        assert lowest([1, 3, 2, 5, 4], 2) == [None, 1, 2, 2, 4]

    def test_highest_lowest_warmup_none_prefix(self) -> None:
        source = [3.0, 1.0, 4.0, 1.0, 5.0]
        assert highest(source, 4)[:3] == [None, None, None]
        assert lowest(source, 4)[:3] == [None, None, None]
        # [3,1,4,1] -> hi 4 lo 1; [1,4,1,5] -> hi 5 lo 1
        assert highest(source, 4)[3:] == [4.0, 5.0]
        assert lowest(source, 4)[3:] == [1.0, 1.0]
