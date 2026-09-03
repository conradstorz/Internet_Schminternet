"""Tests for the scheduler wiring in main.py.

`main()` itself can't be called in a test (it serves uvicorn forever), so the
executor configuration is built by a helper that is verified directly here.
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from main import _build_executors


def test_scheduler_accepts_the_configured_executors():
    """AsyncIOScheduler must accept the executors main() hands it.

    Passing the alias string "asyncio" instead of an executor instance raises
    TypeError at construction — which crashed the service on every start.
    """
    scheduler = AsyncIOScheduler(executors=_build_executors())
    assert set(scheduler._executors) >= {"default", "threadpool"}
