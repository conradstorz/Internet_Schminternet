"""Tests for leds/quality.py — pure scoring/colour maths for the rank-sorted
LED strip. No hardware, no I/O: these must run identically with or without
rpi_ws281x installed.
"""

from __future__ import annotations

import colorsys

import pytest

from leds.quality import overall_score, quality_color, score_monitor
from monitors.base import MonitorResult


def _result(monitor: str, target: str, metric: str, value: float, status: str = "ok") -> MonitorResult:
    return MonitorResult(
        monitor=monitor,
        target=target,
        timestamp="2026-01-01T00:00:00+00:00",
        metric=metric,
        value=value,
        status=status,
    )


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------

PING_CFG = {
    "thresholds": {
        "degraded_ms": 100,
        "down_ms": 500,
        "loss_degraded_pct": 10,
        "loss_down_pct": 50,
    }
}


class TestScorePing:
    def test_healthy(self):
        # 10ms is not literally 0, so the linear curve from (0, 1.0) to
        # (degraded_ms, 0.6) puts it just under a perfect 1.0.
        results = [
            _result("ping", "1.1.1.1", "latency_ms", 10.0),
            _result("ping", "1.1.1.1", "packet_loss_pct", 0.0),
        ]
        assert score_monitor("ping", results, PING_CFG) == pytest.approx(0.96, abs=0.001)

    def test_at_degraded_knee(self):
        results = [
            _result("ping", "1.1.1.1", "latency_ms", 100.0),
            _result("ping", "1.1.1.1", "packet_loss_pct", 0.0),
        ]
        assert score_monitor("ping", results, PING_CFG) == pytest.approx(0.6, abs=0.01)

    def test_at_down_knee(self):
        results = [
            _result("ping", "1.1.1.1", "latency_ms", 500.0),
            _result("ping", "1.1.1.1", "packet_loss_pct", 0.0),
        ]
        assert score_monitor("ping", results, PING_CFG) == pytest.approx(0.2, abs=0.01)

    def test_beyond_down_knee(self):
        results = [
            _result("ping", "1.1.1.1", "latency_ms", 5000.0),
            _result("ping", "1.1.1.1", "packet_loss_pct", 0.0),
        ]
        assert score_monitor("ping", results, PING_CFG) == 0.0

    def test_no_reply_scores_zero(self):
        # No latency_ms result at all, and total packet loss.
        results = [_result("ping", "1.1.1.1", "packet_loss_pct", 100.0, status="down")]
        assert score_monitor("ping", results, PING_CFG) == 0.0

    def test_empty_results(self):
        assert score_monitor("ping", [], PING_CFG) == 0.0

    def test_per_target_averaging(self):
        # good.example: 5ms latency -> 0.98; bad.example: no reply -> 0.0.
        # Average of the two per-target scores, not a global blend.
        results = [
            _result("ping", "good.example", "latency_ms", 5.0),
            _result("ping", "good.example", "packet_loss_pct", 0.0),
            _result("ping", "bad.example", "packet_loss_pct", 100.0, status="down"),
        ]
        assert score_monitor("ping", results, PING_CFG) == pytest.approx(0.49, abs=0.001)

    def test_total_loss_scores_zero_even_with_good_latency(self):
        results = [
            _result("ping", "1.1.1.1", "latency_ms", 5.0),
            _result("ping", "1.1.1.1", "packet_loss_pct", 100.0),
        ]
        assert score_monitor("ping", results, PING_CFG) == 0.0

    def test_worse_of_latency_and_loss(self):
        # Good latency, degraded-level loss -> loss score (worse) wins.
        results = [
            _result("ping", "1.1.1.1", "latency_ms", 5.0),
            _result("ping", "1.1.1.1", "packet_loss_pct", 10.0),
        ]
        assert score_monitor("ping", results, PING_CFG) == pytest.approx(0.6, abs=0.01)


# ---------------------------------------------------------------------------
# dns
# ---------------------------------------------------------------------------

DNS_CFG = {"thresholds": {"degraded_ms": 200, "down_ms": 1000}}


class TestScoreDns:
    def test_healthy(self):
        results = [_result("dns", "8.8.8.8", "resolution_ms", 20.0)]
        assert score_monitor("dns", results, DNS_CFG) == pytest.approx(0.96, abs=0.001)

    def test_at_degraded_knee(self):
        results = [_result("dns", "8.8.8.8", "resolution_ms", 200.0)]
        assert score_monitor("dns", results, DNS_CFG) == pytest.approx(0.6, abs=0.01)

    def test_at_down_knee(self):
        results = [_result("dns", "8.8.8.8", "resolution_ms", 1000.0)]
        assert score_monitor("dns", results, DNS_CFG) == pytest.approx(0.2, abs=0.01)

    def test_beyond_down_knee(self):
        results = [_result("dns", "8.8.8.8", "resolution_ms", 5000.0)]
        assert score_monitor("dns", results, DNS_CFG) == 0.0

    def test_error_sentinel(self):
        results = [_result("dns", "8.8.8.8", "resolution_ms", -1.0, status="down")]
        assert score_monitor("dns", results, DNS_CFG) == 0.0

    def test_empty_results(self):
        assert score_monitor("dns", [], DNS_CFG) == 0.0

    def test_averaged_across_resolvers(self):
        results = [
            _result("dns", "8.8.8.8", "resolution_ms", 20.0),
            _result("dns", "9.9.9.9", "resolution_ms", -1.0, status="down"),
        ]
        assert score_monitor("dns", results, DNS_CFG) == pytest.approx(0.48, abs=0.001)


# ---------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------

HTTP_CFG = {"thresholds": {"degraded_ms": 1000}}


class TestScoreHttp:
    def test_healthy(self):
        results = [_result("http", "https://a", "response_ms", 50.0)]
        assert score_monitor("http", results, HTTP_CFG) == pytest.approx(0.98, abs=0.001)

    def test_at_degraded_knee(self):
        results = [_result("http", "https://a", "response_ms", 1000.0)]
        assert score_monitor("http", results, HTTP_CFG) == pytest.approx(0.6, abs=0.01)

    def test_at_down_knee_is_twice_degraded(self):
        results = [_result("http", "https://a", "response_ms", 2000.0)]
        assert score_monitor("http", results, HTTP_CFG) == pytest.approx(0.2, abs=0.01)

    def test_beyond_down_knee(self):
        results = [_result("http", "https://a", "response_ms", 10000.0)]
        assert score_monitor("http", results, HTTP_CFG) == 0.0

    def test_unreachable_scores_zero(self):
        results = [_result("http", "https://a", "response_ms", -1.0, status="down")]
        assert score_monitor("http", results, HTTP_CFG) == 0.0

    def test_empty_results(self):
        assert score_monitor("http", [], HTTP_CFG) == 0.0

    def test_averaged_across_targets(self):
        results = [
            _result("http", "https://a", "response_ms", 50.0),
            _result("http", "https://b", "response_ms", -1.0, status="down"),
        ]
        assert score_monitor("http", results, HTTP_CFG) == pytest.approx(0.5, abs=0.02)


# ---------------------------------------------------------------------------
# speedtest
# ---------------------------------------------------------------------------

class TestScoreSpeedtest:
    def test_healthy_at_expectation(self):
        cfg = {"expected_download_mbps": 50}
        results = [_result("speedtest", "ookla", "download_mbps", 50.0)]
        assert score_monitor("speedtest", results, cfg) == pytest.approx(1.0, abs=0.01)

    def test_above_expectation_caps_at_one(self):
        cfg = {"expected_download_mbps": 50}
        results = [_result("speedtest", "ookla", "download_mbps", 500.0)]
        assert score_monitor("speedtest", results, cfg) == 1.0

    def test_halfway_to_expectation(self):
        cfg = {"expected_download_mbps": 50}
        results = [_result("speedtest", "ookla", "download_mbps", 25.0)]
        assert score_monitor("speedtest", results, cfg) == pytest.approx(0.5, abs=0.01)

    def test_zero_throughput(self):
        cfg = {"expected_download_mbps": 50}
        results = [_result("speedtest", "ookla", "download_mbps", 0.0)]
        assert score_monitor("speedtest", results, cfg) == pytest.approx(0.0, abs=0.01)

    def test_error_sentinel(self):
        cfg = {"expected_download_mbps": 50}
        results = [_result("speedtest", "ookla", "download_mbps", -1.0, status="down")]
        assert score_monitor("speedtest", results, cfg) == 0.0

    def test_empty_results(self):
        assert score_monitor("speedtest", [], {"expected_download_mbps": 50}) == 0.0

    def test_no_expectation_configured_success_scores_one(self):
        cfg = {"expected_download_mbps": 0}
        results = [_result("speedtest", "ookla", "download_mbps", 12.3)]
        assert score_monitor("speedtest", results, cfg) == 1.0

    def test_no_expectation_configured_failure_scores_zero(self):
        cfg = {"expected_download_mbps": 0}
        results = [_result("speedtest", "ookla", "download_mbps", -1.0, status="down")]
        assert score_monitor("speedtest", results, cfg) == 0.0


# ---------------------------------------------------------------------------
# overall_score
# ---------------------------------------------------------------------------

class TestOverallScore:
    def test_equal_weights_is_plain_mean(self):
        scores = {"ping": 1.0, "dns": 0.5, "http": 0.0}
        weights = {"ping": 1.0, "dns": 1.0, "http": 1.0}
        assert overall_score(scores, weights) == pytest.approx(0.5)

    def test_weighting_is_applied(self):
        scores = {"ping": 1.0, "dns": 0.0}
        weights = {"ping": 3.0, "dns": 1.0}
        # (1.0*3 + 0.0*1) / 4 = 0.75
        assert overall_score(scores, weights) == pytest.approx(0.75)

    def test_missing_monitor_is_skipped_not_zero(self):
        # speedtest hasn't reported yet -> absent from `scores` entirely.
        scores = {"ping": 1.0, "dns": 1.0, "http": 1.0}
        weights = {"ping": 1.0, "dns": 1.0, "http": 1.0, "speedtest": 0.25}
        assert overall_score(scores, weights) == pytest.approx(1.0)

    def test_speedtest_low_weight_limits_its_pull(self):
        # speedtest is terrible but heavily downweighted -> overall stays high.
        scores = {"ping": 1.0, "dns": 1.0, "http": 1.0, "speedtest": 0.0}
        weights = {"ping": 1.0, "dns": 1.0, "http": 1.0, "speedtest": 0.25}
        result = overall_score(scores, weights)
        assert result > 0.85
        # ...but it still tints the colour: not a perfect 1.0.
        assert result < 1.0

    def test_empty_scores(self):
        assert overall_score({}, {"ping": 1.0}) == 0.0


# ---------------------------------------------------------------------------
# quality_color
# ---------------------------------------------------------------------------

class TestQualityColor:
    def test_perfect_score_is_green(self):
        r, g, b = quality_color(1.0)
        assert r == 0
        assert g == 255
        assert b == 0

    def test_zero_score_is_exactly_pure_red(self):
        assert quality_color(0.0) == (255, 0, 0)

    def test_half_score_is_yellow_orange(self):
        r, g, b = quality_color(0.5)
        assert r > 200
        assert g > 200
        assert b < 50

    def test_monotone_hue_across_sweep(self):
        scores = [i / 10 for i in range(11)]  # 0.0 .. 1.0
        hues = []
        for s in scores:
            r, g, b = quality_color(s)
            h, _s, _v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            hues.append(h)
        # Hue must be non-decreasing as score rises from 0 -> 1 (red -> green).
        for a, b_ in zip(hues, hues[1:]):
            assert b_ >= a - 1e-9

    def test_returns_8bit_ints(self):
        for s in (0.0, 0.25, 0.5, 0.75, 1.0):
            r, g, b = quality_color(s)
            for channel in (r, g, b):
                assert isinstance(channel, int)
                assert 0 <= channel <= 255

    def test_clamps_out_of_range_scores(self):
        assert quality_color(1.5) == quality_color(1.0)
        assert quality_color(-0.5) == quality_color(0.0)
