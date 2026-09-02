"""Tests for degraded-condition handling in main.run_monitor().

Drives the real main.run_monitor() against an isolated SQLite DB, with
main._alerter patched to a mock and main._led left None (matching the
pattern established in tests/test_startup.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import main
import storage.db as db
from monitors.base import MonitorResult


@pytest.fixture(autouse=True)
async def env(tmp_path):
    """Isolated DB + reset module globals + mock alerter for every test."""
    db.configure(str(tmp_path / "test.db"))
    await db.init_db()

    main._monitor_status = {}
    main._monitor_configs = {}
    main._led = None

    alerter = MagicMock()
    alerter.send_alert = AsyncMock()
    alerter.send_recovery = AsyncMock()
    alerter.flush_alert_queue = AsyncMock()
    alerter.send_degraded_alert = AsyncMock(return_value=True)
    main._alerter = alerter

    yield alerter

    main._alerter = None
    main._monitor_status = {}
    main._monitor_configs = {}


def _results(monitor: str, status: str, message: str = "slow", ts: str | None = None) -> list[MonitorResult]:
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    return [
        MonitorResult(
            monitor=monitor,
            target="1.1.1.1",
            timestamp=ts,
            metric="latency_ms",
            value=250.0,
            status=status,
            message=message,
        )
    ]


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------------
# 1. Minutes threshold not yet crossed
# ---------------------------------------------------------------------------

async def test_minutes_threshold_not_yet_crossed(env):
    main._monitor_status["ping"] = "degraded"
    main._monitor_configs["ping"] = {"degraded_alert_minutes": 10, "degraded_alert_cycles": None}

    ts = datetime.now(timezone.utc).isoformat()
    await main.run_monitor("ping", _results("ping", "degraded", ts=ts))

    env.send_degraded_alert.assert_not_called()
    since = await db.get_state("degraded_since:ping")
    cycles = await db.get_state("degraded_cycles:ping")
    assert since == ts
    assert cycles == "1"


# ---------------------------------------------------------------------------
# 2. Minutes threshold crossed
# ---------------------------------------------------------------------------

async def test_minutes_threshold_crossed(env):
    main._monitor_status["ping"] = "degraded"
    main._monitor_configs["ping"] = {"degraded_alert_minutes": 10, "degraded_alert_cycles": None}

    old_since = _iso(datetime.now(timezone.utc) - timedelta(minutes=15))
    await db.set_state("degraded_since:ping", old_since)
    await db.set_state("degraded_cycles:ping", "3")

    ts = datetime.now(timezone.utc).isoformat()
    await main.run_monitor("ping", _results("ping", "degraded", ts=ts))

    env.send_degraded_alert.assert_called_once()
    call = env.send_degraded_alert.call_args
    assert call[0][0] == "ping"
    assert call[0][2] == ts

    # Derive the expected duration string from this test's own seeded elapsed
    # time (old_since was set 15 minutes before "now") and cycle count
    # (seeded at "3", +1 for this poll), mirroring main.py's computation
    # instead of hardcoding a value.
    expected_cycles = 3 + 1
    elapsed_minutes = (datetime.now(timezone.utc) - datetime.fromisoformat(old_since)).total_seconds() / 60
    expected_duration = f"{int(elapsed_minutes)}m ({expected_cycles} polls)"
    assert call[0][3] == expected_duration

    expected_desc = "; ".join(r.message for r in _results("ping", "degraded", ts=ts) if r.message) or "No detail"
    assert call[0][1] == expected_desc

    since = await db.get_state("degraded_since:ping")
    cycles = await db.get_state("degraded_cycles:ping")
    assert since == ts
    assert cycles == "1"


# ---------------------------------------------------------------------------
# 3. Cycles threshold crossed
# ---------------------------------------------------------------------------

async def test_cycles_threshold_crossed(env):
    main._monitor_status["ping"] = "degraded"
    main._monitor_configs["ping"] = {"degraded_alert_minutes": None, "degraded_alert_cycles": 5}

    recent_since = datetime.now(timezone.utc).isoformat()
    await db.set_state("degraded_since:ping", recent_since)
    await db.set_state("degraded_cycles:ping", "4")

    await main.run_monitor("ping", _results("ping", "degraded"))

    env.send_degraded_alert.assert_called_once()
    cycles = await db.get_state("degraded_cycles:ping")
    assert cycles == "1"


# ---------------------------------------------------------------------------
# 4. Both thresholds set, minutes crossed first
# ---------------------------------------------------------------------------

async def test_both_thresholds_minutes_crossed_first(env):
    main._monitor_status["ping"] = "degraded"
    main._monitor_configs["ping"] = {"degraded_alert_minutes": 10, "degraded_alert_cycles": 100}

    old_since = _iso(datetime.now(timezone.utc) - timedelta(minutes=15))
    await db.set_state("degraded_since:ping", old_since)
    await db.set_state("degraded_cycles:ping", "1")

    await main.run_monitor("ping", _results("ping", "degraded"))

    env.send_degraded_alert.assert_called_once()


# ---------------------------------------------------------------------------
# 5. Both thresholds set, cycles crossed first
# ---------------------------------------------------------------------------

async def test_both_thresholds_cycles_crossed_first(env):
    main._monitor_status["ping"] = "degraded"
    main._monitor_configs["ping"] = {"degraded_alert_minutes": 1000, "degraded_alert_cycles": 3}

    recent_since = datetime.now(timezone.utc).isoformat()
    await db.set_state("degraded_since:ping", recent_since)
    await db.set_state("degraded_cycles:ping", "2")

    await main.run_monitor("ping", _results("ping", "degraded"))

    env.send_degraded_alert.assert_called_once()


# ---------------------------------------------------------------------------
# 6. Neither threshold set — no alert ever, state still tracked
# ---------------------------------------------------------------------------

async def test_no_thresholds_configured_tracks_state_without_alerting(env):
    main._monitor_status["ping"] = "degraded"
    main._monitor_configs["ping"] = {"degraded_alert_minutes": None, "degraded_alert_cycles": None}

    for _ in range(3):
        await main.run_monitor("ping", _results("ping", "degraded"))

    env.send_degraded_alert.assert_not_called()
    cycles = await db.get_state("degraded_cycles:ping")
    assert cycles == "3"


# ---------------------------------------------------------------------------
# 7. degraded -> down
# ---------------------------------------------------------------------------

async def test_degraded_to_down_clears_state_and_logs_event(env):
    main._monitor_status["ping"] = "degraded"
    main._monitor_configs["ping"] = {}

    await db.set_state("degraded_since:ping", datetime.now(timezone.utc).isoformat())
    await db.set_state("degraded_cycles:ping", "4")

    await main.run_monitor("ping", _results("ping", "down", message="timeout"))

    assert await db.get_state("degraded_since:ping") == ""
    assert await db.get_state("degraded_cycles:ping") == ""

    events = await db.get_events()
    ping_events = [e for e in events if e["monitor"] == "ping"]
    descriptions = [e["description"] for e in ping_events]

    # The episode-end event (logged only when previous == "degraded", inside
    # the new_status == "down" branch) has this exact, message-independent
    # description. A status-pair assertion would not distinguish it from the
    # down-alert's own event (previous_status/new_status are identical for
    # both), so pin the literal string main.py writes.
    assert "Status changed: degraded → down" in descriptions
    assert len(ping_events) == 2

    env.send_degraded_alert.assert_not_called()
    env.send_alert.assert_called_once()


# ---------------------------------------------------------------------------
# 8. down -> degraded
# ---------------------------------------------------------------------------

async def test_down_to_degraded_onset_and_fresh_accumulation(env):
    main._monitor_status["ping"] = "down"
    main._monitor_configs["ping"] = {}

    await main.run_monitor("ping", _results("ping", "degraded"))

    events = await db.get_events()
    assert any(e["previous_status"] == "down" and e["new_status"] == "degraded" for e in events)

    cycles = await db.get_state("degraded_cycles:ping")
    assert cycles == "1"


# ---------------------------------------------------------------------------
# 9. degraded -> ok
# ---------------------------------------------------------------------------

async def test_degraded_to_ok_clears_state_no_email(env):
    main._monitor_status["ping"] = "degraded"
    main._monitor_configs["ping"] = {}

    await db.set_state("degraded_since:ping", datetime.now(timezone.utc).isoformat())
    await db.set_state("degraded_cycles:ping", "2")

    await main.run_monitor("ping", _results("ping", "ok", message=""))

    assert await db.get_state("degraded_since:ping") == ""
    assert await db.get_state("degraded_cycles:ping") == ""

    events = await db.get_events()
    assert any(e["previous_status"] == "degraded" and e["new_status"] == "ok" for e in events)

    env.send_degraded_alert.assert_not_called()
    env.send_recovery.assert_not_called()
    env.send_alert.assert_not_called()


# ---------------------------------------------------------------------------
# 10. Onset event logged before threshold crossed
# ---------------------------------------------------------------------------

async def test_onset_event_logged_before_threshold_crossed(env):
    main._monitor_status["ping"] = "ok"
    main._monitor_configs["ping"] = {"degraded_alert_minutes": 10, "degraded_alert_cycles": 5}

    await main.run_monitor("ping", _results("ping", "degraded"))

    events = await db.get_events()
    assert any(e["previous_status"] == "ok" and e["new_status"] == "degraded" for e in events)
    env.send_degraded_alert.assert_not_called()


# ---------------------------------------------------------------------------
# 11. After alert fires, immediate second poll does not re-fire
# ---------------------------------------------------------------------------

async def test_no_second_alert_immediately_after_reset(env):
    main._monitor_status["ping"] = "degraded"
    main._monitor_configs["ping"] = {"degraded_alert_minutes": 10, "degraded_alert_cycles": None}

    old_since = _iso(datetime.now(timezone.utc) - timedelta(minutes=15))
    await db.set_state("degraded_since:ping", old_since)
    await db.set_state("degraded_cycles:ping", "1")

    await main.run_monitor("ping", _results("ping", "degraded"))
    env.send_degraded_alert.assert_called_once()

    # Immediately poll again — reset timer means minutes threshold no longer met.
    await main.run_monitor("ping", _results("ping", "degraded"))
    env.send_degraded_alert.assert_called_once()  # still only one call


# ---------------------------------------------------------------------------
# 12. Restart with stale keys
# ---------------------------------------------------------------------------

async def test_restart_with_stale_keys_preserves_onset(env):
    main._monitor_status["ping"] = "unknown"
    main._monitor_configs["ping"] = {"degraded_alert_minutes": None, "degraded_alert_cycles": None}

    stale_since = _iso(datetime.now(timezone.utc) - timedelta(hours=2))
    await db.set_state("degraded_since:ping", stale_since)
    await db.set_state("degraded_cycles:ping", "5")

    await main.run_monitor("ping", _results("ping", "degraded"))

    since = await db.get_state("degraded_since:ping")
    cycles = await db.get_state("degraded_cycles:ping")
    assert since == stale_since
    assert cycles == "6"

    events = await db.get_events()
    assert any(e["previous_status"] == "unknown" and e["new_status"] == "degraded" for e in events)


# ---------------------------------------------------------------------------
# 13. Restart that comes back ok — stale degraded keys must be cleared
# ---------------------------------------------------------------------------

async def test_restart_then_ok_clears_stale_degraded_keys(env):
    main._monitor_status["ping"] = "unknown"
    main._monitor_configs["ping"] = {"degraded_alert_minutes": None, "degraded_alert_cycles": None}

    stale_since = _iso(datetime.now(timezone.utc) - timedelta(hours=2))
    await db.set_state("degraded_since:ping", stale_since)
    await db.set_state("degraded_cycles:ping", "5")

    await main.run_monitor("ping", _results("ping", "ok", message=""))

    since = await db.get_state("degraded_since:ping")
    cycles = await db.get_state("degraded_cycles:ping")
    assert since == ""
    assert cycles == ""


# ---------------------------------------------------------------------------
# 14. send_degraded_alert returns False (cooldown-suppressed) — episode
#     state must NOT be reset, so the clock keeps running toward the
#     threshold instead of restarting it.
# ---------------------------------------------------------------------------

async def test_false_return_does_not_reset_episode_state(env):
    env.send_degraded_alert = AsyncMock(return_value=False)

    main._monitor_status["ping"] = "degraded"
    main._monitor_configs["ping"] = {"degraded_alert_minutes": 10, "degraded_alert_cycles": None}

    old_since = _iso(datetime.now(timezone.utc) - timedelta(minutes=15))
    await db.set_state("degraded_since:ping", old_since)
    await db.set_state("degraded_cycles:ping", "3")

    ts = datetime.now(timezone.utc).isoformat()
    await main.run_monitor("ping", _results("ping", "degraded", ts=ts))

    env.send_degraded_alert.assert_called_once()

    since = await db.get_state("degraded_since:ping")
    cycles = await db.get_state("degraded_cycles:ping")
    # Episode keeps its original onset time and keeps counting cycles up
    # (3 seeded + 1 for this poll), rather than being reset to ts / "1".
    assert since == old_since
    assert cycles == "4"


# ---------------------------------------------------------------------------
# 15. send_degraded_alert returns True — reset still happens exactly as
#     before (explicit contract test, distinct from the fixture default).
# ---------------------------------------------------------------------------

async def test_true_return_resets_episode_state(env):
    env.send_degraded_alert = AsyncMock(return_value=True)

    main._monitor_status["ping"] = "degraded"
    main._monitor_configs["ping"] = {"degraded_alert_minutes": 10, "degraded_alert_cycles": None}

    old_since = _iso(datetime.now(timezone.utc) - timedelta(minutes=15))
    await db.set_state("degraded_since:ping", old_since)
    await db.set_state("degraded_cycles:ping", "3")

    ts = datetime.now(timezone.utc).isoformat()
    await main.run_monitor("ping", _results("ping", "degraded", ts=ts))

    env.send_degraded_alert.assert_called_once()

    since = await db.get_state("degraded_since:ping")
    cycles = await db.get_state("degraded_cycles:ping")
    assert since == ts
    assert cycles == "1"


# ---------------------------------------------------------------------------
# 16. degraded -> unknown clears state (unreachable today — no monitor emits
#     "unknown" — but the invariant must hold by construction, same shape as
#     the restart-to-ok bug fixed for case 13).
# ---------------------------------------------------------------------------

async def test_degraded_to_unknown_clears_stale_degraded_keys(env):
    main._monitor_status["ping"] = "degraded"
    main._monitor_configs["ping"] = {}

    await db.set_state("degraded_since:ping", datetime.now(timezone.utc).isoformat())
    await db.set_state("degraded_cycles:ping", "4")

    await main.run_monitor("ping", _results("ping", "unknown", message=""))

    assert await db.get_state("degraded_since:ping") == ""
    assert await db.get_state("degraded_cycles:ping") == ""
