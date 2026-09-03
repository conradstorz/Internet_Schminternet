"""Async SQLite storage layer."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from monitors.base import MonitorResult

_DB_PATH: str = "data/schminternet.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT    NOT NULL,
    monitor   TEXT    NOT NULL,
    target    TEXT    NOT NULL,
    metric    TEXT    NOT NULL,
    value     REAL,
    status    TEXT
);
CREATE INDEX IF NOT EXISTS idx_metrics_timestamp  ON metrics (timestamp);
CREATE INDEX IF NOT EXISTS idx_metrics_monitor_ts ON metrics (monitor, timestamp);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    monitor         TEXT NOT NULL,
    description     TEXT,
    previous_status TEXT,
    new_status      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events (timestamp);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS alert_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    queued_at       TEXT NOT NULL,
    last_attempt_at TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_queue_queued_at ON alert_queue (queued_at);
"""


def configure(path: str) -> None:
    global _DB_PATH
    _DB_PATH = path


async def init_db() -> None:
    os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


async def insert_metrics(results: list[MonitorResult]) -> None:
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.executemany(
            "INSERT INTO metrics (timestamp, monitor, target, metric, value, status) "
            "VALUES (?,?,?,?,?,?)",
            [
                (r.timestamp, r.monitor, r.target, r.metric, r.value, r.status)
                for r in results
            ],
        )
        await db.commit()


async def query_recent(monitor: str, hours: int = 24) -> list[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT timestamp, monitor, target, metric, value, status
            FROM   metrics
            WHERE  monitor = ?
              AND  timestamp > datetime('now', ? || ' hours')
            ORDER BY timestamp ASC
            """,
            (monitor, f"-{hours}"),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_current_status() -> list[dict]:
    """Return the latest row for every (monitor, target, metric) combination."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT monitor, target, metric, value, status, MAX(timestamp) AS timestamp
            FROM   metrics
            GROUP  BY monitor, target, metric
            """
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def insert_event(event: dict) -> None:
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO events
                (timestamp, event_type, monitor, description, previous_status, new_status)
            VALUES (?,?,?,?,?,?)
            """,
            (
                event["timestamp"],
                event["event_type"],
                event["monitor"],
                event.get("description", ""),
                event.get("previous_status", ""),
                event.get("new_status", ""),
            ),
        )
        await db.commit()


async def get_events(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM events ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_events_range(start: str, end: str, limit: int = 1000) -> list[dict]:
    """Return events with ``start <= timestamp <= end``, oldest first.

    ``start``/``end`` are ISO-8601 UTC timestamp strings, compared as text
    against the stored ``timestamp`` column (the same convention already used
    elsewhere in this module). Capped at ``limit`` rows to bound response size
    for a caller panning across a large history.
    """
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM events WHERE timestamp >= ? AND timestamp <= ? "
            "ORDER BY timestamp ASC LIMIT ?",
            (start, end, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_state(key: str) -> Optional[str]:
    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM state WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_state(key: str, value: str) -> None:
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO state (key, value) VALUES (?,?)", (key, value)
        )
        await db.commit()


async def enqueue_alert(created_at: str, subject: str, body: str) -> None:
    """Insert a new row into alert_queue.

    ``created_at`` is the original event timestamp (caller-supplied).
    ``queued_at`` is set internally to the current UTC time (queue insertion time).
    """
    queued_at = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "INSERT INTO alert_queue (created_at, queued_at, subject, body) "
            "VALUES (?,?,?,?)",
            (created_at, queued_at, subject, body),
        )
        await db.commit()


async def get_pending_alerts() -> list[dict]:
    """Non-destructive read — returns all queued rows ordered oldest first."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM alert_queue ORDER BY id ASC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def delete_queued_alert(alert_id: int) -> None:
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute("DELETE FROM alert_queue WHERE id = ?", (alert_id,))
        await db.commit()


async def update_alert_attempt(alert_id: int) -> None:
    last_attempt = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "UPDATE alert_queue SET attempt_count = attempt_count + 1, "
            "last_attempt_at = ? WHERE id = ?",
            (last_attempt, alert_id),
        )
        await db.commit()


async def cleanup_old(days: int = 30) -> None:
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "DELETE FROM metrics WHERE timestamp < datetime('now', ? || ' days')",
            (f"-{days}",),
        )
        await db.execute(
            "DELETE FROM events WHERE timestamp < datetime('now', ? || ' days')",
            (f"-{days}",),
        )
        await db.execute(
            "DELETE FROM alert_queue WHERE queued_at < datetime('now', ? || ' days')",
            (f"-{days}",),
        )
        await db.commit()
        await db.execute("VACUUM")
