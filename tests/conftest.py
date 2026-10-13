"""Suite-wide isolation for the fault-localisation feature.

Any test that drives ``main.run_monitor("ping", ...)`` through an
ok/unknown -> degraded/down transition would otherwise spawn a real
background path trace (``mtr`` on Linux) that can outlive the test and write
an event into the next test's database. And the gateway auto-detect cache is
module-level, so one test's detection result would leak into the next.

Every test therefore gets:
  * ``monitors.path_trace.run_trace`` stubbed to return ``None`` instantly
    (tests that exercise the trigger re-stub it with their own fake);
  * the gateway detection cache cleared before and after;
  * ``main``'s trace bookkeeping (in-flight task, cooldown clock) reset;
  * ``main``'s hysteresis pending state (``_monitor_pending``) cleared.
"""

from __future__ import annotations

import sys

import pytest

from monitors import gateway, path_trace


def _reset_trace_state() -> None:
    # Look main up lazily rather than importing it here: importing main at
    # conftest-load time runs its logging bootstrap before pytest's capture
    # is in place, which breaks tests/test_logging.py's handler accounting.
    main = sys.modules.get("main")
    if main is None:
        return
    task = main._trace_task
    if task is not None and not task.done():
        task.cancel()
    main._trace_task = None
    main._last_trace_started = None
    # Hysteresis pending state is in-memory per monitor; a confirmation count
    # left over from one test must not leak into the next test's first poll.
    main._monitor_pending = {}


@pytest.fixture(autouse=True)
def _isolate_fault_localisation(monkeypatch):
    monkeypatch.setattr(path_trace, "run_trace", lambda *_a, **_kw: None)
    # gateway.py's only subprocess call: on Linux, any test reaching ping.run
    # with the default gateway "auto" would otherwise run a real TTL walk.
    monkeypatch.setattr(gateway, "_ping_once", lambda *_a, **_kw: "")
    gateway.reset()
    _reset_trace_state()
    yield
    gateway.reset()
    _reset_trace_state()
