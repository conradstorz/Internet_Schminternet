"""Tests for leds/animation.py — the pure breathe brightness curve.

No hardware, no threads: breathe_factor is a plain function of elapsed
time, so it is fully testable on any machine.
"""

from __future__ import annotations

import pytest

from leds.animation import breathe_factor


class TestBreatheFactorKeyPoints:
    def test_starts_at_full_brightness(self):
        assert breathe_factor(0.0, period=4.0, floor=0.7) == pytest.approx(1.0)

    def test_dips_to_floor_at_half_period(self):
        assert breathe_factor(2.0, period=4.0, floor=0.7) == pytest.approx(0.7)

    def test_returns_to_full_at_end_of_period(self):
        assert breathe_factor(4.0, period=4.0, floor=0.7) == pytest.approx(1.0)

    def test_quarter_period_sits_midway(self):
        # Raised cosine: cos(pi/2) == 0 -> exactly halfway between floor and 1.
        assert breathe_factor(1.0, period=4.0, floor=0.7) == pytest.approx(0.85)


class TestBreatheFactorIsPeriodic:
    def test_value_repeats_every_period(self):
        for t in (0.0, 0.37, 1.1, 2.5, 3.9):
            assert breathe_factor(t, 4.0, 0.7) == pytest.approx(
                breathe_factor(t + 4.0, 4.0, 0.7)
            )

    def test_never_leaves_the_floor_to_one_band(self):
        # Sweep several periods at fine resolution.
        for i in range(1000):
            t = i * 0.013
            value = breathe_factor(t, 4.0, 0.7)
            assert 0.7 - 1e-9 <= value <= 1.0 + 1e-9


class TestBreatheFactorGuards:
    def test_zero_period_degenerates_to_static(self):
        assert breathe_factor(1.23, period=0.0, floor=0.7) == pytest.approx(1.0)

    def test_negative_period_degenerates_to_static(self):
        assert breathe_factor(1.23, period=-4.0, floor=0.7) == pytest.approx(1.0)

    def test_floor_of_one_is_constant(self):
        for t in (0.0, 1.0, 2.0, 3.0):
            assert breathe_factor(t, 4.0, 1.0) == pytest.approx(1.0)

    def test_floor_above_one_is_clamped(self):
        assert breathe_factor(2.0, 4.0, 1.8) == pytest.approx(1.0)

    def test_negative_floor_is_clamped_to_zero(self):
        assert breathe_factor(2.0, 4.0, -0.5) == pytest.approx(0.0)

    def test_negative_elapsed_is_still_in_band(self):
        # A clock that has not been reset yet must not produce a wild value.
        assert 0.7 - 1e-9 <= breathe_factor(-1.0, 4.0, 0.7) <= 1.0 + 1e-9
