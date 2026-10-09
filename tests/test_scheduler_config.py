"""Tests for the scheduler wiring in main.py.

`main()` itself can't be called in a test (it serves uvicorn forever), so the
executor configuration is built by a helper that is verified directly here.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import main
from config import load_config
from main import _build_executors


def test_scheduler_accepts_the_configured_executors():
    """AsyncIOScheduler must accept the executors main() hands it.

    Passing the alias string "asyncio" instead of an executor instance raises
    TypeError at construction — which crashed the service on every start.
    """
    AsyncIOScheduler(executors=_build_executors())


async def test_speedtest_job_is_dispatched_as_coroutine(monkeypatch):
    """APScheduler must await SpeedtestJob.__call__; registering the bare
    instance sends it to a thread and silently drops the coroutine."""
    calls = []

    async def fake_run(config, params):
        calls.append(params["level"])
        return []

    monkeypatch.setattr(main.speedtest_monitor, "run", fake_run)
    monkeypatch.setattr(main.db, "recent_values", AsyncMock(return_value=[]))
    monkeypatch.setattr(main, "run_monitor", AsyncMock())

    scheduler = AsyncIOScheduler(executors=main._build_executors())
    job = main.SpeedtestJob(load_config("/nonexistent/config.yaml"), scheduler)
    # misfire_grace_time mirrors main(); APScheduler's default of 1 s would
    # drop this 1 s-interval job whenever the loop is briefly contended.
    scheduler.add_job(job.__call__, "interval", seconds=1, id="speedtest", misfire_grace_time=60, max_instances=1, coalesce=True)
    scheduler.start()
    try:
        await asyncio.sleep(2.5)
    finally:
        scheduler.shutdown(wait=False)
    assert calls, "speedtest job body never executed under the real scheduler"
