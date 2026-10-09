"""Tests for monitors/speedtest_policy.py — the pure good/poor verdict and
the intensity-ladder transition. No I/O, no scheduler."""

from __future__ import annotations

from monitors.speedtest_policy import next_level, verdict


class TestVerdict:
    def test_failed_run_is_poor(self):
        assert verdict(-1.0, 0, 0.5, [], 0.8, 3) == "poor"

    def test_no_expectation_no_history_is_good(self):
        assert verdict(10.0, 0, 0.5, [], 0.8, 3) == "good"

    def test_below_fixed_threshold_is_poor(self):
        # 20 < 50 * 0.5
        assert verdict(20.0, 50, 0.5, [], 0.8, 3) == "poor"

    def test_at_fixed_threshold_is_good(self):
        assert verdict(25.0, 50, 0.5, [], 0.8, 3) == "good"

    def test_below_rolling_mean_ratio_is_poor(self):
        # mean(history) = 300; 0.8 * 300 = 240; 200 < 240
        assert verdict(200.0, 0, 0.5, [300.0, 300.0, 300.0], 0.8, 3) == "poor"

    def test_at_rolling_mean_ratio_is_good(self):
        assert verdict(240.0, 0, 0.5, [300.0, 300.0, 300.0], 0.8, 3) == "good"

    def test_rolling_mean_ignored_below_min_samples(self):
        # Only two samples: the rolling rule must not apply.
        assert verdict(200.0, 0, 0.5, [300.0, 300.0], 0.8, 3) == "good"

    def test_empty_history_never_divides_by_zero(self):
        # min_samples 0 satisfies len(history) >= min_samples with no
        # readings at all; the rolling rule must stay off, not divide by 0.
        assert verdict(100.0, 0, 0.5, [], 0.8, 0) == "good"

    def test_fixed_threshold_still_applies_below_min_samples(self):
        assert verdict(20.0, 50, 0.5, [300.0], 0.8, 3) == "poor"

    def test_either_rule_can_trigger(self):
        # Passes the fixed threshold (100 >= 25) but fails the rolling rule.
        assert verdict(100.0, 50, 0.5, [300.0, 300.0, 300.0], 0.8, 3) == "poor"


class TestNextLevel:
    def test_poor_rises_one_level_and_resets_streak(self):
        assert next_level(1, "poor", 1, 2, 3) == (2, 0)

    def test_poor_is_capped_at_max_level(self):
        assert next_level(3, "poor", 0, 2, 3) == (3, 0)

    def test_good_increments_streak_without_moving(self):
        assert next_level(2, "good", 0, 2, 3) == (2, 1)

    def test_good_streak_reaching_calm_after_drops_one_level(self):
        assert next_level(2, "good", 1, 2, 3) == (1, 0)

    def test_good_at_level_zero_stays_at_zero(self):
        assert next_level(0, "good", 1, 2, 3) == (0, 0)

    def test_calm_after_one_drops_immediately(self):
        assert next_level(3, "good", 0, 1, 3) == (2, 0)
