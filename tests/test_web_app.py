"""Tests for web/app.py — the events API's windowed (start/end) query support."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import storage.db as db
from web.app import app, set_quality_snapshot


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


# ---------------------------------------------------------------------------
# Asset cache-busting: a deployed change must reach the browser without the
# user having to force-reload.
# ---------------------------------------------------------------------------

def test_index_script_url_is_versioned(client):
    body = client.get("/").text
    assert "/static/main.js?v=" in body
    assert '/static/main.js"' not in body


def test_index_is_not_cached(client):
    """A cached page would keep serving the previous asset version after a deploy."""
    assert client.get("/").headers["cache-control"] == "no-store"


def test_asset_version_changes_when_a_static_file_changes(tmp_path, monkeypatch):
    import web.app as web_app

    static = tmp_path / "static"
    static.mkdir()
    asset = static / "main.js"
    asset.write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(web_app, "_STATIC_DIR", static)

    before = web_app.asset_version()
    asset.write_text("console.log(2) // longer content", encoding="utf-8")
    after = web_app.asset_version()

    assert before != after


def test_asset_version_survives_a_missing_static_dir(tmp_path, monkeypatch):
    import web.app as web_app

    monkeypatch.setattr(web_app, "_STATIC_DIR", tmp_path / "nope")
    assert web_app.asset_version() == "dev"


# ---------------------------------------------------------------------------
# External IP: metrics rows carry no message, so the address the ip monitor
# found is only readable from the state table.
# ---------------------------------------------------------------------------

async def test_api_ip_returns_the_stored_address(client):
    await db.init_db()
    await db.set_state("external_ip", "203.0.113.7")

    body = client.get("/api/ip").json()

    assert body["ip"] == "203.0.113.7"


async def test_api_ip_is_null_before_the_first_poll(client):
    await db.init_db()

    body = client.get("/api/ip").json()

    assert body["ip"] is None


# ---------------------------------------------------------------------------
# /api/quality — the rank-sorted LED strip's state, readable without hardware.
# ---------------------------------------------------------------------------

def test_api_quality_returns_scores_ranking_and_colors(client):
    set_quality_snapshot(
        {
            "scores": {"ping": 0.94, "http": 0.71, "dns": 0.55, "speedtest": 0.10},
            "overall": 0.72,
            "ranking": ["ping", "http", "dns", "speedtest"],
            "colors": {
                "ping": "#7fff00",
                "http": "#...placeholder",
                "dns": "#...placeholder",
                "speedtest": "#ff0000",
                "overall": "#...placeholder",
            },
        }
    )

    body = client.get("/api/quality").json()

    assert body["ranking"] == ["ping", "http", "dns", "speedtest"]
    assert body["scores"]["ping"] == 0.94
    assert body["overall"] == 0.72
    assert body["colors"]["speedtest"] == "#ff0000"
    assert "overall" in body["colors"]


def test_api_quality_defaults_empty_before_any_poll(client):
    set_quality_snapshot({"scores": {}, "overall": 0.0, "ranking": [], "colors": {}})

    body = client.get("/api/quality").json()

    assert body["scores"] == {}
    assert body["ranking"] == []
