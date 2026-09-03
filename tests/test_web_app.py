"""Tests for web/app.py — the events API's windowed (start/end) query support."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import storage.db as db
from web.app import app


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Each test gets its own fresh database file."""
    db.configure(str(tmp_path / "test.db"))


@pytest.fixture
def client():
    return TestClient(app)


async def _seed_events():
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
        "event_type": "status_change", "monitor": "dns",
        "description": "after window", "previous_status": "ok", "new_status": "down",
    })


async def test_api_events_window_returns_only_events_in_range(client):
    await _seed_events()
    res = client.get(
        "/api/events",
        params={"start": "2026-01-02T00:00:00+00:00", "end": "2026-01-03T00:00:00+00:00"},
    )
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["description"] == "inside window"


async def test_api_events_window_ordered_oldest_first(client):
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
    res = client.get(
        "/api/events",
        params={"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-03T00:00:00+00:00"},
    )
    assert res.status_code == 200
    data = res.json()
    assert [d["description"] for d in data] == ["first", "second"]


async def test_api_events_malformed_start_returns_400(client):
    await _seed_events()
    res = client.get(
        "/api/events",
        params={"start": "not-a-timestamp", "end": "2026-01-03T00:00:00+00:00"},
    )
    assert res.status_code == 400


async def test_api_events_malformed_end_returns_400(client):
    await _seed_events()
    res = client.get(
        "/api/events",
        params={"start": "2026-01-01T00:00:00+00:00", "end": "also-not-a-timestamp"},
    )
    assert res.status_code == 400


async def test_api_events_start_without_end_returns_400(client):
    await _seed_events()
    res = client.get("/api/events", params={"start": "2026-01-01T00:00:00+00:00"})
    assert res.status_code == 400


async def test_api_events_limit_only_still_works_as_before(client):
    await _seed_events()
    res = client.get("/api/events", params={"limit": 2})
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 2
    # Existing behaviour: newest first, no window filtering.
    assert data[0]["description"] == "after window"


async def test_api_events_no_params_defaults_to_50(client):
    await _seed_events()
    res = client.get("/api/events")
    assert res.status_code == 200
    assert len(res.json()) == 3
