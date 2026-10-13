"""run_monitor()-level tests for recovery hysteresis (flap damping).

Drives the real main.run_monitor() against an isolated SQLite DB with a mock
alerter, the same pattern as tests/test_degraded_alerting.py. Every test sets
`recover_polls` explicitly: a missing key means no damping.
See docs/superpowers/specs/2026-10-13-status-flap-damping-design.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import main
import storage.db as db
from monitors.base import MonitorResult


@pytest.fixture(autouse=True)
async def env(tmp_path, monkeypatch):
    db.configure(str(tmp_path / "test.db"))
    await db.init_db()

    main._monitor_status = {}
    main._monitor_pending = {}
    main._monitor_configs = {}
    main._led = None

    alerter = MagicMock()
    alerter.send_alert = AsyncMock()
    alerter.send_recovery = AsyncMock()
    alerter.flush_alert_queue = AsyncMock()
    alerter.send_degraded_alert = AsyncMock(return_value=True)
    main._alerter = alerter

    broadcasts: list[dict] = []
    monkeypatch.setattr(main, "broadcast_status", broadcasts.append)

    traces: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        main, "_maybe_trace", lambda name, prev, new: traces.append((name, prev, new))
    )

    yield {"alerter": alerter, "broadcasts": broadcasts, "traces": traces}

    main._alerter = None
    main._monitor_status = {}
    main._monitor_pending = {}
    main._monitor_configs = {}


def _rows(status: str, monitor: str = "ping", message: str = "") -> list[MonitorResult]:
    if not message and status != "ok":
        message = f"{status} detail"
    return [
        MonitorResult(
            monitor=monitor,
            target="1.1.1.1",
            timestamp=datetime.now(timezone.utc).isoformat(),
            metric="packet_loss_pct",
            value=60.0 if status == "down" else (40.0 if status == "degraded" else 0.0),
            status=status,
            message=message,
        )
    ]


async def _status_changes(monitor: str = "ping") -> list[dict]:
    """status_change rows for *monitor*, oldest first (get_events is newest first)."""
    events = await db.get_events(500)
    rows = [e for e in events if e["monitor"] == monitor and e["event_type"] == "status_change"]
    return sorted(rows, key=lambda e: (e["timestamp"], e["id"]))


# ---------------------------------------------------------------------------
# The first down transition is never delayed
# ---------------------------------------------------------------------------

async def test_first_down_fires_immediately(env):
    main._monitor_status["ping"] = "ok"
    main._monitor_configs["ping"] = {"recover_polls": 3}

    await main.run_monitor("ping", _rows("down", message="60% loss"))

    assert main._monitor_status["ping"] == "down"
    env["alerter"].send_alert.assert_called_once()
    args = env["alerter"].send_alert.call_args.args
    assert args[0] == "ping"
    assert args[2] == "down"
    assert args[3] == "ok"

    changes = await _status_changes()
    assert len(changes) == 1
    assert changes[0]["previous_status"] == "ok"
    assert changes[0]["new_status"] == "down"
    assert "60% loss" in changes[0]["description"]
    assert env["broadcasts"][-1]["status"] == "down"


async def test_first_poll_from_unknown_is_immediate(env):
    main._monitor_configs["ping"] = {"recover_polls": 3}

    await main.run_monitor("ping", _rows("ok"))

    assert main._monitor_status["ping"] == "ok"
    assert env["broadcasts"][-1]["status"] == "ok"


# ---------------------------------------------------------------------------
# Alternating down/degraded raw statuses produce ONE down transition
# ---------------------------------------------------------------------------

async def test_alternating_down_degraded_is_one_down_transition(env):
    main._monitor_status["ping"] = "ok"
    main._monitor_configs["ping"] = {"recover_polls": 3}

    for raw in ["down", "degraded"] * 6:
        await main.run_monitor("ping", _rows(raw))

    changes = await _status_changes()
    assert len(changes) == 1
    assert changes[0]["previous_status"] == "ok"
    assert changes[0]["new_status"] == "down"
    assert not any(e["new_status"] == "degraded" for e in changes)

    env["alerter"].send_alert.assert_called_once()
    env["alerter"].send_recovery.assert_not_called()
    env["alerter"].send_degraded_alert.assert_not_called()
    assert main._monitor_status["ping"] == "down"
    assert [b["status"] for b in env["broadcasts"]] == ["down"] * 12


async def test_alternating_ok_down_dns_is_one_down_transition(env):
    main._monitor_status["dns"] = "ok"
    main._monitor_configs["dns"] = {"recover_polls": 3}

    for raw in ["down", "ok"] * 5:
        await main.run_monitor("dns", _rows(raw, monitor="dns"))

    changes = await _status_changes("dns")
    assert [(e["previous_status"], e["new_status"]) for e in changes] == [("ok", "down")]
    env["alerter"].send_alert.assert_called_once()
    env["alerter"].send_recovery.assert_not_called()


# ---------------------------------------------------------------------------
# Recovery to ok after N ok polls
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", [2, 3])
async def test_recovery_to_ok_after_n_ok_polls(env, n):
    main._monitor_status["ping"] = "ok"
    main._monitor_configs["ping"] = {"recover_polls": n}

    await main.run_monitor("ping", _rows("down"))
    env["alerter"].send_alert.assert_called_once()

    for _ in range(n - 1):
        await main.run_monitor("ping", _rows("ok"))
        env["alerter"].send_recovery.assert_not_called()
        env["alerter"].flush_alert_queue.assert_not_called()
        assert main._monitor_status["ping"] == "down"
        assert env["broadcasts"][-1]["status"] == "down"

    await main.run_monitor("ping", _rows("ok"))

    env["alerter"].send_recovery.assert_called_once()
    env["alerter"].flush_alert_queue.assert_called_once()
    assert main._monitor_status["ping"] == "ok"
    assert env["broadcasts"][-1]["status"] == "ok"

    changes = await _status_changes()
    assert [(e["previous_status"], e["new_status"]) for e in changes] == [
        ("ok", "down"),
        ("down", "ok"),
    ]


async def test_recovery_lands_on_worst_confirming_status(env):
    main._monitor_status["ping"] = "down"
    main._monitor_configs["ping"] = {"recover_polls": 3}

    for raw in ("degraded", "ok", "ok"):
        await main.run_monitor("ping", _rows(raw))

    assert main._monitor_status["ping"] == "degraded"
    changes = await _status_changes()
    assert [(e["previous_status"], e["new_status"]) for e in changes] == [("down", "degraded")]
    env["alerter"].send_recovery.assert_not_called()


async def test_interrupted_recovery_restarts_count(env):
    main._monitor_status["ping"] = "down"
    main._monitor_configs["ping"] = {"recover_polls": 3}

    for raw in ("ok", "ok", "down", "ok", "ok"):
        await main.run_monitor("ping", _rows(raw))
    assert main._monitor_status["ping"] == "down"
    env["alerter"].send_recovery.assert_not_called()
    # The repeated down is not a transition: no second alert, no new row.
    env["alerter"].send_alert.assert_not_called()
    assert await _status_changes() == []

    await main.run_monitor("ping", _rows("ok"))
    assert main._monitor_status["ping"] == "ok"
    env["alerter"].send_recovery.assert_called_once()


async def test_recover_polls_one_is_undamped(env):
    main._monitor_status["ping"] = "down"
    main._monitor_configs["ping"] = {"recover_polls": 1}

    await main.run_monitor("ping", _rows("ok"))

    assert main._monitor_status["ping"] == "ok"
    env["alerter"].send_recovery.assert_called_once()


async def test_missing_recover_polls_is_undamped(env):
    main._monitor_status["ping"] = "down"
    main._monitor_configs["ping"] = {}

    await main.run_monitor("ping", _rows("ok"))

    assert main._monitor_status["ping"] == "ok"


# ---------------------------------------------------------------------------
# Trace trigger and degraded accumulation see the effective status
# ---------------------------------------------------------------------------

async def test_trace_trigger_sees_effective_status(env):
    main._monitor_status["ping"] = "ok"
    main._monitor_configs["ping"] = {"recover_polls": 3}

    # ok -> down: genuine onset.
    await main.run_monitor("ping", _rows("down"))
    # A held ok poll, then degraded: raw says ok -> degraded (which would be
    # an onset), but the effective status never left down.
    for raw in ("ok", "degraded"):
        await main.run_monitor("ping", _rows(raw))

    assert env["traces"] == [
        ("ping", "ok", "down"),
        ("ping", "down", "down"),
        ("ping", "down", "down"),
    ]


async def test_trace_not_triggered_by_held_ok_then_down(env):
    """A flap through raw ok back to down must not look like a new onset."""
    traces = env["traces"]
    main._monitor_status["ping"] = "down"
    main._monitor_configs["ping"] = {"recover_polls": 3}

    for raw in ("ok", "down"):
        await main.run_monitor("ping", _rows(raw))

    assert all(prev == "down" for _name, prev, _new in traces)


async def test_degraded_accumulation_counts_held_polls(env):
    main._monitor_status["ping"] = "degraded"
    main._monitor_configs["ping"] = {
        "recover_polls": 3,
        "degraded_alert_minutes": None,
        "degraded_alert_cycles": None,
    }

    # Raw ok twice: effective stays degraded, so each poll still accumulates.
    await main.run_monitor("ping", _rows("degraded"))
    await main.run_monitor("ping", _rows("ok"))
    await main.run_monitor("ping", _rows("ok"))

    assert main._monitor_status["ping"] == "degraded"
    assert await db.get_state("degraded_cycles:ping") == "3"

    # Third ok confirms recovery: degraded state cleared, one recovery row.
    await main.run_monitor("ping", _rows("ok"))
    assert main._monitor_status["ping"] == "ok"
    assert await db.get_state("degraded_cycles:ping") == ""
    assert await db.get_state("degraded_since:ping") == ""
    assert await db.get_state("degraded_onset:ping") == ""
    changes = await _status_changes()
    assert [(e["previous_status"], e["new_status"]) for e in changes] == [("degraded", "ok")]


async def test_degraded_alert_fires_while_raw_is_held_ok(env):
    main._monitor_status["ping"] = "degraded"
    main._monitor_configs["ping"] = {
        "recover_polls": 3,
        "degraded_alert_minutes": None,
        "degraded_alert_cycles": 2,
    }

    await main.run_monitor("ping", _rows("degraded"))
    env["alerter"].send_degraded_alert.assert_not_called()
    await main.run_monitor("ping", _rows("ok"))  # held: effective degraded, cycle 2

    env["alerter"].send_degraded_alert.assert_called_once()


# ---------------------------------------------------------------------------
# Raw metric rows keep their own statuses
# ---------------------------------------------------------------------------

async def test_metric_rows_keep_raw_status(env):
    main._monitor_status["ping"] = "down"
    main._monitor_configs["ping"] = {"recover_polls": 3}

    await main.run_monitor("ping", _rows("ok"))

    rows = await db.query_recent("ping", hours=1)
    assert [r["status"] for r in rows] == ["ok"]
    assert main._monitor_status["ping"] == "down"


# ---------------------------------------------------------------------------
# Summary log line: raw status, plus "(holding <effective>)" when they differ
# ---------------------------------------------------------------------------

def _summaries(records, monitor="ping"):
    return [
        r for r in records
        if r.name == "main" and r.getMessage().startswith(monitor) and "EVENT" not in r.getMessage()
    ]


async def test_summary_shows_raw_and_holding_suffix(env, caplog):
    main._monitor_status["ping"] = "down"
    main._monitor_configs["ping"] = {"recover_polls": 3}

    with caplog.at_level(logging.INFO, logger="main"):
        await main.run_monitor("ping", _rows("ok"))

    summaries = _summaries(caplog.records)
    assert len(summaries) == 1
    msg = summaries[0].getMessage()
    assert msg.split()[1] == "ok"
    assert msg.endswith(" (holding down)")


async def test_summary_has_no_suffix_when_statuses_match(env, caplog):
    main._monitor_status["ping"] = "ok"
    main._monitor_configs["ping"] = {"recover_polls": 3}

    with caplog.at_level(logging.INFO, logger="main"):
        await main.run_monitor("ping", _rows("down"))

    msg = _summaries(caplog.records)[0].getMessage()
    assert "holding" not in msg
