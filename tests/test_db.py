"""Tests for storage/db.py — schema init, insert, query, state, events."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import aiosqlite

import storage.db as db
from monitors.base import MonitorResult


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Each test gets its own fresh database file."""
    db.configure(str(tmp_path / "test.db"))


async def test_init_creates_file(tmp_path):
    path = str(tmp_path / "new.db")
    db.configure(path)
    await db.init_db()
    import os
    assert os.path.exists(path)


async def test_insert_and_query_recent():
    await db.init_db()
    ts = datetime.now(timezone.utc).isoformat()
    results = [
        MonitorResult(
            monitor="ping", target="8.8.8.8",
            timestamp=ts, metric="latency_ms",
            value=42.5, status="ok",
        )
    ]
    await db.insert_metrics(results)
    rows = await db.query_recent("ping", hours=1)
    assert len(rows) == 1
    assert rows[0]["value"] == pytest.approx(42.5)
    assert rows[0]["status"] == "ok"


async def test_query_different_monitor_returns_empty():
    await db.init_db()
    ts = datetime.now(timezone.utc).isoformat()
    await db.insert_metrics([
        MonitorResult(monitor="ping", target="1.1.1.1", timestamp=ts,
                      metric="latency_ms", value=10.0, status="ok")
    ])
    rows = await db.query_recent("dns", hours=1)
    assert rows == []


async def test_get_current_status_groups_by_monitor_metric():
    await db.init_db()
    ts = datetime.now(timezone.utc).isoformat()
    await db.insert_metrics([
        MonitorResult(monitor="ping", target="8.8.8.8", timestamp=ts,
                      metric="latency_ms", value=30.0, status="ok"),
        MonitorResult(monitor="ping", target="8.8.8.8", timestamp=ts,
                      metric="packet_loss_pct", value=0.0, status="ok"),
    ])
    status = await db.get_current_status()
    metrics = {r["metric"] for r in status}
    assert "latency_ms" in metrics
    assert "packet_loss_pct" in metrics


async def test_get_events_range_returns_only_rows_inside_window():
    await db.init_db()
    await db.insert_event({
        "timestamp": "2026-01-01T00:00:00+00:00",
        "event_type": "status_change", "monitor": "ping",
        "description": "before window", "previous_status": "ok", "new_status": "down",
    })
    await db.insert_event({
        "timestamp": "2026-01-02T12:00:00+00:00",
        "event_type": "status_change", "monitor": "ping",
        "description": "inside window", "previous_status": "down", "new_status": "ok",
    })
    await db.insert_event({
        "timestamp": "2026-01-05T00:00:00+00:00",
        "event_type": "status_change", "monitor": "ping",
        "description": "after window", "previous_status": "ok", "new_status": "down",
    })
    rows = await db.get_events_range(
        "2026-01-02T00:00:00+00:00", "2026-01-03T00:00:00+00:00"
    )
    assert len(rows) == 1
    assert rows[0]["description"] == "inside window"


async def test_get_events_range_ordered_oldest_first():
    await db.init_db()
    await db.insert_event({
        "timestamp": "2026-01-02T00:00:00+00:00",
        "event_type": "status_change", "monitor": "ping",
        "description": "second", "previous_status": "ok", "new_status": "down",
    })
    await db.insert_event({
        "timestamp": "2026-01-01T00:00:00+00:00",
        "event_type": "status_change", "monitor": "ping",
        "description": "first", "previous_status": "ok", "new_status": "down",
    })
    rows = await db.get_events_range(
        "2026-01-01T00:00:00+00:00", "2026-01-03T00:00:00+00:00"
    )
    assert [r["description"] for r in rows] == ["first", "second"]


async def test_get_events_range_respects_cap():
    await db.init_db()
    for i in range(10):
        await db.insert_event({
            "timestamp": f"2026-01-01T00:00:{i:02d}+00:00",
            "event_type": "status_change", "monitor": "ping",
            "description": f"event{i}", "previous_status": "ok", "new_status": "down",
        })
    rows = await db.get_events_range(
        "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00", limit=5
    )
    assert len(rows) == 5


async def test_get_events_range_boundaries_inclusive():
    await db.init_db()
    await db.insert_event({
        "timestamp": "2026-01-01T00:00:00+00:00",
        "event_type": "status_change", "monitor": "ping",
        "description": "at start", "previous_status": "ok", "new_status": "down",
    })
    await db.insert_event({
        "timestamp": "2026-01-02T00:00:00+00:00",
        "event_type": "status_change", "monitor": "ping",
        "description": "at end", "previous_status": "ok", "new_status": "down",
    })
    rows = await db.get_events_range(
        "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"
    )
    assert {r["description"] for r in rows} == {"at start", "at end"}


async def test_state_get_set():
    await db.init_db()
    assert await db.get_state("external_ip") is None
    await db.set_state("external_ip", "1.2.3.4")
    assert await db.get_state("external_ip") == "1.2.3.4"
    # Overwrite
    await db.set_state("external_ip", "5.6.7.8")
    assert await db.get_state("external_ip") == "5.6.7.8"


async def test_insert_and_get_events():
    await db.init_db()
    ts = datetime.now(timezone.utc).isoformat()
    await db.insert_event({
        "timestamp": ts,
        "event_type": "status_change",
        "monitor": "ping",
        "description": "Went down",
        "previous_status": "ok",
        "new_status": "down",
    })
    events = await db.get_events()
    assert len(events) == 1
    assert events[0]["monitor"] == "ping"
    assert events[0]["new_status"] == "down"


async def test_enqueue_and_get_pending_alerts():
    await db.init_db()
    ts = datetime.now(timezone.utc).isoformat()
    await db.enqueue_alert(ts, "Test subject", "Test body")
    rows = await db.get_pending_alerts()
    assert len(rows) == 1
    assert rows[0]["created_at"] == ts
    assert rows[0]["subject"] == "Test subject"
    assert rows[0]["body"] == "Test body"
    assert rows[0]["attempt_count"] == 0
    assert rows[0]["queued_at"] is not None


async def test_get_pending_alerts_is_non_destructive():
    """Calling get_pending_alerts twice returns the same rows both times."""
    await db.init_db()
    ts = datetime.now(timezone.utc).isoformat()
    await db.enqueue_alert(ts, "subject", "body")
    await db.get_pending_alerts()
    rows = await db.get_pending_alerts()
    assert len(rows) == 1


async def test_get_pending_alerts_ordered_oldest_first():
    await db.init_db()
    ts = datetime.now(timezone.utc).isoformat()
    await db.enqueue_alert(ts, "first", "body1")
    await db.enqueue_alert(ts, "second", "body2")
    rows = await db.get_pending_alerts()
    assert rows[0]["id"] < rows[1]["id"]
    assert rows[0]["subject"] == "first"
    assert rows[1]["subject"] == "second"


async def test_delete_queued_alert_removes_row():
    await db.init_db()
    ts = datetime.now(timezone.utc).isoformat()
    await db.enqueue_alert(ts, "subject", "body")
    rows = await db.get_pending_alerts()
    await db.delete_queued_alert(rows[0]["id"])
    assert await db.get_pending_alerts() == []


async def test_delete_queued_alert_missing_row_does_not_raise():
    await db.init_db()
    await db.delete_queued_alert(9999)  # no such row — must be a silent no-op


async def test_update_alert_attempt_increments_count():
    await db.init_db()
    ts = datetime.now(timezone.utc).isoformat()
    await db.enqueue_alert(ts, "subject", "body")
    rows = await db.get_pending_alerts()
    await db.update_alert_attempt(rows[0]["id"])
    rows = await db.get_pending_alerts()
    assert rows[0]["attempt_count"] == 1
    assert rows[0]["last_attempt_at"] is not None


async def test_update_alert_attempt_accumulates():
    await db.init_db()
    ts = datetime.now(timezone.utc).isoformat()
    await db.enqueue_alert(ts, "subject", "body")
    rows = await db.get_pending_alerts()
    row_id = rows[0]["id"]
    await db.update_alert_attempt(row_id)
    await db.update_alert_attempt(row_id)
    rows = await db.get_pending_alerts()
    assert rows[0]["attempt_count"] == 2


async def test_cleanup_old_prunes_alert_queue():
    await db.init_db()
    # Insert a row with a very old queued_at (simulate aged-out row)
    async with aiosqlite.connect(db._DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO alert_queue (created_at, queued_at, subject, body) "
            "VALUES (?,?,?,?)",
            ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", "old", "old body"),
        )
        await conn.commit()
    # Also insert a fresh row that should survive
    ts = datetime.now(timezone.utc).isoformat()
    await db.enqueue_alert(ts, "fresh", "fresh body")
    await db.cleanup_old(days=30)
    rows = await db.get_pending_alerts()
    assert len(rows) == 1
    assert rows[0]["subject"] == "fresh"
