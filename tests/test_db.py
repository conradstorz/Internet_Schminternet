"""Tests for storage/db.py — schema init, insert, query, state, events."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

import storage.db as db
from monitors.base import MonitorResult


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Each test gets its own fresh database file."""
    db.configure(str(tmp_path / "test.db"))


@pytest.mark.asyncio
async def test_init_creates_file(tmp_path):
    path = str(tmp_path / "new.db")
    db.configure(path)
    await db.init_db()
    import os
    assert os.path.exists(path)


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_query_different_monitor_returns_empty():
    await db.init_db()
    ts = datetime.now(timezone.utc).isoformat()
    await db.insert_metrics([
        MonitorResult(monitor="ping", target="1.1.1.1", timestamp=ts,
                      metric="latency_ms", value=10.0, status="ok")
    ])
    rows = await db.query_recent("dns", hours=1)
    assert rows == []


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_state_get_set():
    await db.init_db()
    assert await db.get_state("external_ip") is None
    await db.set_state("external_ip", "1.2.3.4")
    assert await db.get_state("external_ip") == "1.2.3.4"
    # Overwrite
    await db.set_state("external_ip", "5.6.7.8")
    assert await db.get_state("external_ip") == "5.6.7.8"


@pytest.mark.asyncio
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
