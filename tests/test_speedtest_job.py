"""Tests for main.SpeedtestJob — the adaptive-cadence wrapper around the
speedtest monitor. The monitor, the DB history and run_monitor() are
stubbed; a MagicMock stands in for the APScheduler instance so the test can
assert reschedule_job() calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import main
from monitors.base import MonitorResult

LADDER = [
    {"name": "calm",        "interval_seconds": 1800, "download_bytes": 10, "upload_bytes": 4},
    {"name": "watch",       "interval_seconds": 300,  "download_bytes": 10, "upload_bytes": 4},
    {"name": "alert",       "interval_seconds": 120,  "download_bytes": 25, "upload_bytes": 10},
    {"name": "investigate", "interval_seconds": 60,   "download_bytes": 25, "upload_bytes": 10},
]


def _config() -> dict:
    return {"monitors": {"speedtest": {
        "expected_download_mbps": 50,
        "thresholds": {"degraded_ratio": 0.5},
        "adaptive": {"avg_ratio": 0.8, "min_samples": 3, "calm_after": 2, "ladder": LADDER},
    }}}


def _download(value: float) -> list[MonitorResult]:
    status = "down" if value < 0 else "ok"
    return [MonitorResult("speedtest", "cloudflare", "2026-01-01T00:00:00+00:00",
                          "download_mbps", value, status)]


@pytest.fixture
def stubs(monkeypatch):
    run = AsyncMock(return_value=_download(300.0))
    history = AsyncMock(return_value=[])
    record = AsyncMock()
    monkeypatch.setattr(main.speedtest_monitor, "run", run)
    monkeypatch.setattr(main.db, "recent_values", history)
    monkeypatch.setattr(main, "run_monitor", record)
    return run, history, record


async def test_starts_at_level_one_with_its_interval():
    job = main.SpeedtestJob(_config(), MagicMock())
    assert job.level == 1
    assert job.good_streak == 0
    assert job.interval_seconds == 300


async def test_passes_current_rung_and_level_to_monitor(stubs):
    run, _, record = stubs
    scheduler = MagicMock()
    job = main.SpeedtestJob(_config(), scheduler)
    await job()
    _, params = run.call_args.args
    assert params["level"] == 1
    assert params["name"] == "watch"
    assert params["download_bytes"] == 10
    record.assert_awaited_once()
    assert record.call_args.args[0] == "speedtest"


async def test_poor_run_escalates_and_reschedules(stubs):
    run, _, _ = stubs
    run.return_value = _download(10.0)   # below 50 * 0.5
    scheduler = MagicMock()
    job = main.SpeedtestJob(_config(), scheduler)
    await job()
    assert job.level == 2
    scheduler.reschedule_job.assert_called_once_with("speedtest", trigger="interval", seconds=120)


async def test_good_run_without_level_change_does_not_reschedule(stubs):
    scheduler = MagicMock()
    job = main.SpeedtestJob(_config(), scheduler)
    await job()                      # streak 1 of 2
    assert job.level == 1
    assert job.good_streak == 1
    scheduler.reschedule_job.assert_not_called()


async def test_two_good_runs_calm_down(stubs):
    scheduler = MagicMock()
    job = main.SpeedtestJob(_config(), scheduler)
    await job()
    await job()
    assert job.level == 0
    scheduler.reschedule_job.assert_called_once_with("speedtest", trigger="interval", seconds=1800)


async def test_rolling_mean_from_db_feeds_verdict(stubs):
    run, history, _ = stubs
    history.return_value = [300.0, 300.0, 300.0]
    run.return_value = _download(100.0)  # passes 25 Mbps floor, fails 0.8 * 300
    scheduler = MagicMock()
    job = main.SpeedtestJob(_config(), scheduler)
    await job()
    assert job.level == 2
    history.assert_awaited_once_with("speedtest", "cloudflare", "download_mbps", hours=24)


async def test_history_window_is_read_before_the_run(monkeypatch):
    """The rolling mean must not include the reading being judged, so the
    history query has to happen before the monitor writes this run's row."""
    order: list[str] = []

    async def fake_run(config, params):
        order.append("run")
        return _download(300.0)

    async def fake_history(*args, **kwargs):
        order.append("history")
        return []

    monkeypatch.setattr(main.speedtest_monitor, "run", fake_run)
    monkeypatch.setattr(main.db, "recent_values", fake_history)
    monkeypatch.setattr(main, "run_monitor", AsyncMock())

    job = main.SpeedtestJob(_config(), MagicMock())
    await job()
    assert order == ["history", "run"]


async def test_failed_run_escalates(stubs):
    run, _, _ = stubs
    run.return_value = _download(-1.0)
    job = main.SpeedtestJob(_config(), MagicMock())
    await job()
    assert job.level == 2


async def test_escalation_caps_at_top_rung(stubs):
    run, _, _ = stubs
    run.return_value = _download(-1.0)
    scheduler = MagicMock()
    job = main.SpeedtestJob(_config(), scheduler)
    for _ in range(5):
        await job()
    assert job.level == 3
    # Rescheduled on 1->2 and 2->3 only.
    assert scheduler.reschedule_job.call_count == 2
