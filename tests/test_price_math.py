from orb_topstepx.price_math import compute_pair, prices_equal, round_to_tick

import pytest


class TestRoundToTick:
    def test_exact_tick_passes_through(self):
        assert round_to_tick(21010.00, 0.25) == 21010.00

    def test_half_tick_rounds_away_from_zero(self):
        # 21010.125 is exactly half-way between 21010.00 and 21010.25 → rounds up.
        assert round_to_tick(21010.125, 0.25) == 21010.25

    def test_rounds_down_below_half(self):
        assert round_to_tick(21010.10, 0.25) == 21010.00

    def test_rounds_up_above_half(self):
        assert round_to_tick(21010.13, 0.25) == 21010.25

    def test_penny_tick_size(self):
        assert round_to_tick(100.003, 0.01) == 100.00
        assert round_to_tick(100.005, 0.01) == 100.01

    def test_negative_price_round(self):
        # Shouldn't occur in practice, but math should still work.
        assert round_to_tick(-21010.13, 0.25) == -21010.25

    def test_rejects_zero_tick(self):
        with pytest.raises(ValueError):
            round_to_tick(100.0, 0.0)

    def test_rejects_negative_tick(self):
        with pytest.raises(ValueError):
            round_to_tick(100.0, -0.25)


class TestComputePair:
    def test_basic_nq(self):
        buy, sell = compute_pair(21000.0, 10.0, 0.25)
        assert buy == 21010.00
        assert sell == 20990.00

    def test_offset_that_needs_rounding(self):
        # 10.1 offset on NQ should snap to nearest 0.25
        buy, sell = compute_pair(21000.0, 10.1, 0.25)
        assert buy == 21010.00  # 21010.1 → 21010.00
        assert sell == 20990.00  # 20989.9 → 20990.00

    def test_fractional_reference(self):
        buy, sell = compute_pair(21000.13, 10.0, 0.25)
        # reference itself rounds: buy=21010.13→21010.25, sell=20990.13→20990.25
        assert buy == 21010.25
        assert sell == 20990.25


class TestPricesEqual:
    def test_identical(self):
        assert prices_equal(21000.0, 21000.0, 0.25)

    def test_within_half_tick(self):
        # 0.001 diff, tolerance is half a tick (0.125 for 0.25 tick)
        assert prices_equal(21000.001, 21000.0, 0.25)

    def test_outside_half_tick(self):
        assert not prices_equal(21000.13, 21000.0, 0.25)

    def test_exactly_half_tick_not_equal(self):
        # Half-tick diff is on the edge; strict < means "not equal"
        assert not prices_equal(21000.125, 21000.0, 0.25)
