"""Tests for main.py's rank-sorted LED wiring: run_monitor() rescoring every
monitor it has seen so far, publishing the /api/quality snapshot, and calling
LEDController.render_quality — following the pattern in
tests/test_degraded_alerting.py (drive the real main.run_monitor() against an
isolated DB, with main._alerter/._led patched).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import main
import storage.db as db
import web.app as web_app
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


@pytest.fixture(autouse=True)
def isolated_state(tmp_path):
    """Fresh DB, no alerter, a mock LED controller, and clean module state —
    module-level globals in main.py persist across the whole test session
    otherwise (same pattern as test_degraded_alerting.py / test_logging.py)."""
    db.configure(str(tmp_path / "test.db"))

    main._monitor_status = {}
    main._monitor_configs = {
        "ping": {"thresholds": {"degraded_ms": 100, "down_ms": 500,
                                 "loss_degraded_pct": 10, "loss_down_pct": 50}},
        "dns": {"thresholds": {"degraded_ms": 200, "down_ms": 1000}},
        "http": {"thresholds": {"degraded_ms": 1000}},
        "speedtest": {"expected_download_mbps": 50},
    }
    main._monitor_results = {}
    main._led_weights = {"ping": 1.0, "dns": 1.0, "http": 1.0, "speedtest": 0.25}
    main._alerter = None

    mock_led = AsyncMock()
    main._led = mock_led

    web_app.set_quality_snapshot({"scores": {}, "overall": 0.0, "ranking": [], "colors": {}})

    yield mock_led

    main._monitor_status = {}
    main._monitor_results = {}
    main._led = None


async def test_run_monitor_publishes_quality_snapshot(isolated_state):
    await db.init_db()
    await main.run_monitor("ping", [
        _result("ping", "1.1.1.1", "latency_ms", 10.0),
        _result("ping", "1.1.1.1", "packet_loss_pct", 0.0),
    ])

    snapshot = web_app._quality_snapshot
    assert snapshot["ranking"] == ["ping"]
    assert snapshot["scores"]["ping"] == pytest.approx(0.96, abs=0.001)
    assert snapshot["colors"]["ping"].startswith("#")
    assert "overall" in snapshot["colors"]


async def test_run_monitor_calls_render_quality_with_current_scores(isolated_state):
    mock_led = isolated_state
    await db.init_db()
    await main.run_monitor("ping", [
        _result("ping", "1.1.1.1", "latency_ms", 10.0),
        _result("ping", "1.1.1.1", "packet_loss_pct", 0.0),
    ])

    mock_led.render_quality.assert_awaited_once()
    scores_arg, overall_arg = mock_led.render_quality.await_args.args
    assert scores_arg["ping"] == pytest.approx(0.96, abs=0.001)
    assert overall_arg == pytest.approx(0.96, abs=0.001)


async def test_rescoring_accumulates_across_monitors(isolated_state):
    """A monitor that hasn't reported yet is absent from scoring; once it
    reports, it's rescored alongside everything already seen."""
    await db.init_db()

    await main.run_monitor("ping", [
        _result("ping", "1.1.1.1", "latency_ms", 10.0),
        _result("ping", "1.1.1.1", "packet_loss_pct", 0.0),
    ])
    assert web_app._quality_snapshot["ranking"] == ["ping"]

    await main.run_monitor("dns", [_result("dns", "8.8.8.8", "resolution_ms", -1.0, status="down")])

    snapshot = web_app._quality_snapshot
    assert set(snapshot["ranking"]) == {"ping", "dns"}
    assert snapshot["ranking"][0] == "ping"  # ping (0.96) ranks above dns (0.0)
    assert snapshot["scores"]["dns"] == 0.0


async def test_ip_monitor_is_excluded_from_scoring(isolated_state):
    mock_led = isolated_state
    await db.init_db()

    await main.run_monitor("ip", [_result("ip", "self", "ip_changed", 1.0, status="degraded")])

    assert web_app._quality_snapshot["ranking"] == []
    mock_led.render_quality.assert_not_awaited()
