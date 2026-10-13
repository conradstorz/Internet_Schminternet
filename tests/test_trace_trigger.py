"""Tests for main._maybe_trace — the path-trace trigger on ping episode onset —
and the fault-message de-dup in run_monitor().

The trace runner (``monitors.path_trace.run_trace``) is always stubbed; these
tests never run mtr.
"""

from __future__ import annotations

import logging
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

import main
import storage.db as db
from monitors import path_trace
from monitors.base import MonitorResult
from tests.test_path_trace import REAL_REPORT


@pytest.fixture(autouse=True)
async def env(tmp_path):
    db.configure(str(tmp_path / "test.db"))
    await db.init_db()
    main._monitor_status = {}
    main._monitor_configs = {"ping": {"targets": ["1.1.1.1", "wikipedia.org"], "trace": {}}}
    main._led = None
    main._alerter = None
    yield
    main._monitor_status = {}
    main._monitor_configs = {}
    main._alerter = None


@pytest.fixture
def runner(monkeypatch):
    calls: list[tuple] = []

    def fake(target, cycles):
        calls.append((target, cycles))
        return REAL_REPORT

    monkeypatch.setattr(path_trace, "run_trace", fake)
    return calls


async def _trace_events() -> list[dict]:
    return [e for e in await db.get_events(100) if e["event_type"] == "path_trace"]


async def test_onset_fires_and_records_event(runner, caplog):
    with caplog.at_level(logging.WARNING, logger="main"):
        task = main._maybe_trace("ping", "ok", "degraded")
        assert task is not None
        await task

    assert runner == [("1.1.1.1", 20)]
    events = await _trace_events()
    assert len(events) == 1
    ev = events[0]
    assert ev["monitor"] == "ping"
    assert ev["previous_status"] == "ok"
    assert ev["new_status"] == "degraded"
    lines = ev["description"].split("\n")
    assert len(lines) == 2
    assert lines[0] == "Loss begins at hop 2 (142.254.146.181, 40%); destination 40% loss"
    assert lines[1].startswith("1 192.168.86.1 0% · 2 142.254.146.181 40%")
    assert any("Loss begins at hop 2" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)


@pytest.mark.parametrize("previous,new", [("unknown", "down"), ("ok", "down"), ("unknown", "degraded")])
async def test_onset_variants_fire(runner, previous, new):
    task = main._maybe_trace("ping", previous, new)
    assert task is not None
    await task


@pytest.mark.parametrize("previous,new", [
    ("degraded", "down"), ("down", "degraded"), ("ok", "ok"),
    ("degraded", "ok"), ("down", "ok"), ("degraded", "degraded"),
])
async def test_non_onset_transitions_do_not_fire(runner, previous, new):
    assert main._maybe_trace("ping", previous, new) is None
    assert runner == []


async def test_other_monitors_never_trace(runner):
    for name in ("dns", "http", "speedtest", "ip"):
        assert main._maybe_trace(name, "ok", "down") is None
    assert runner == []


async def test_disabled(runner):
    main._monitor_configs["ping"]["trace"] = {"enabled": False}
    assert main._maybe_trace("ping", "ok", "down") is None


async def test_configured_target_and_cycles(runner):
    main._monitor_configs["ping"]["trace"] = {"target": "9.9.9.9", "cycles": 25}
    await main._maybe_trace("ping", "ok", "down")
    assert runner == [("9.9.9.9", 25)]


async def test_cooldown_from_last_start(runner):
    await main._maybe_trace("ping", "ok", "down")
    # New episode onset inside the default 15 min cooldown: suppressed.
    assert main._maybe_trace("ping", "ok", "degraded") is None
    # Wind the clock back past the cooldown.
    main._last_trace_started -= 16 * 60
    task = main._maybe_trace("ping", "ok", "degraded")
    assert task is not None
    await task
    assert len(runner) == 2


async def test_single_trace_in_flight(monkeypatch):
    release = threading.Event()
    calls = []

    def blocking(target, cycles):
        calls.append(target)
        release.wait(5)
        return REAL_REPORT

    monkeypatch.setattr(path_trace, "run_trace", blocking)
    main._monitor_configs["ping"]["trace"] = {"cooldown_minutes": 0}

    first = main._maybe_trace("ping", "ok", "down")
    assert first is not None
    assert main._maybe_trace("ping", "ok", "down") is None
    release.set()
    await first
    assert calls == ["1.1.1.1"]
    # Once finished (and with no cooldown) a new onset may trace again.
    second = main._maybe_trace("ping", "ok", "down")
    assert second is not None
    await second


async def test_unavailable_trace_records_nothing(monkeypatch):
    monkeypatch.setattr(path_trace, "run_trace", lambda *_a: None)
    await main._maybe_trace("ping", "ok", "down")
    assert await _trace_events() == []


async def test_runner_exception_is_logged_not_raised(monkeypatch, caplog):
    def boom(*_a):
        raise RuntimeError("mtr exploded")

    monkeypatch.setattr(path_trace, "run_trace", boom)
    with caplog.at_level(logging.ERROR, logger="main"):
        await main._maybe_trace("ping", "ok", "down")  # must not raise
    assert await _trace_events() == []
    assert any("mtr exploded" in (r.getMessage() + str(r.exc_info)) for r in caplog.records)


def _ping_rows(status: str, message: str) -> list[MonitorResult]:
    ts = "2026-10-13T18:45:00+00:00"
    return [
        MonitorResult("ping", "1.1.1.1", ts, "latency_ms", 25.0, status, message),
        MonitorResult("ping", "1.1.1.1", ts, "packet_loss_pct", 60.0, status, message),
        MonitorResult("ping", "8.8.8.8", ts, "packet_loss_pct", 60.0, status, message),
    ]


async def test_run_monitor_triggers_trace_without_alerter(runner):
    main._monitor_status["ping"] = "ok"
    await main.run_monitor("ping", _ping_rows("down", "x"))
    assert main._trace_task is not None
    await main._trace_task
    assert len(await _trace_events()) == 1


async def test_run_monitor_dedupes_repeated_fault_messages(runner):
    alerter = MagicMock()
    alerter.send_alert = AsyncMock()
    alerter.flush_alert_queue = AsyncMock()
    main._alerter = alerter
    main._monitor_status["ping"] = "ok"
    msg = "LAN gateway 192.168.86.1 ok (0% loss) — fault is beyond your router (modem/ISP)"

    await main.run_monitor("ping", _ping_rows("down", msg))

    desc = alerter.send_alert.call_args.args[1]
    assert desc == msg
    await main._trace_task
