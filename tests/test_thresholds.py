"""Tests for status threshold determination logic across monitors."""

import pytest

from monitors.ping import _determine_status as ping_status
from monitors.dns import _determine_status as dns_status
from monitors.http_check import _determine_status as http_status
from monitors.speedtest import _determine_status as speed_status


# ---------------------------------------------------------------------------
# Ping thresholds
# ---------------------------------------------------------------------------

class TestPingStatus:
    def test_ok(self):
        assert ping_status(20.0, 0.0, {}) == "ok"

    def test_degraded_by_latency(self):
        assert ping_status(150.0, 0.0, {}) == "degraded"

    def test_down_by_latency(self):
        assert ping_status(600.0, 0.0, {}) == "down"

    def test_degraded_by_packet_loss(self):
        assert ping_status(20.0, 15.0, {}) == "degraded"

    def test_down_by_packet_loss(self):
        assert ping_status(20.0, 60.0, {}) == "down"

    def test_down_when_no_reply(self):
        assert ping_status(None, 100.0, {}) == "down"

    def test_custom_thresholds(self):
        t = {"degraded_ms": 50, "down_ms": 200, "loss_degraded_pct": 5, "loss_down_pct": 20}
        assert ping_status(60.0, 0.0, t) == "degraded"
        assert ping_status(30.0, 0.0, t) == "ok"
        assert ping_status(250.0, 0.0, t) == "down"


# ---------------------------------------------------------------------------
# DNS thresholds
# ---------------------------------------------------------------------------

class TestDnsStatus:
    def test_ok(self):
        assert dns_status(50.0, {}) == "ok"

    def test_degraded(self):
        assert dns_status(250.0, {}) == "degraded"

    def test_down_by_threshold(self):
        assert dns_status(1200.0, {}) == "down"

    def test_error_value(self):
        assert dns_status(-1.0, {}) == "down"


# ---------------------------------------------------------------------------
# HTTP thresholds
# ---------------------------------------------------------------------------

class TestHttpStatus:
    def test_ok(self):
        assert http_status(200.0, 200, {}) == "ok"

    def test_degraded_slow(self):
        assert http_status(1500.0, 200, {}) == "degraded"

    def test_down_5xx(self):
        assert http_status(100.0, 503, {}) == "down"

    def test_down_4xx(self):
        assert http_status(100.0, 404, {}) == "down"

    def test_down_error_value(self):
        assert http_status(-1.0, 0, {}) == "down"


# ---------------------------------------------------------------------------
# Speedtest thresholds
# ---------------------------------------------------------------------------

class TestSpeedStatus:
    def test_ok_no_expectation(self):
        # expected_mbps=0 means "no expectation" → always ok
        assert speed_status(10.0, 0, 0.5) == "ok"

    def test_ok_above_threshold(self):
        assert speed_status(60.0, 50, 0.5) == "ok"

    def test_degraded_below_ratio(self):
        # 20 < 50 * 0.5 = 25
        assert speed_status(20.0, 50, 0.5) == "degraded"

    def test_error_value(self):
        assert speed_status(-1.0, 50, 0.5) == "down"
