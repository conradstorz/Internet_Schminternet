"""Async SQLite storage layer."""

from __future__ import annotations

import os
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
        await db.commit()
        await db.execute("VACUUM")
