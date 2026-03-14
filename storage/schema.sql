-- Internet Schminternet — SQLite schema reference
-- The database is initialised automatically by storage/db.py.
-- This file is for documentation and manual inspection only.

CREATE TABLE IF NOT EXISTS metrics (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT    NOT NULL,          -- ISO-8601 UTC, e.g. "2026-03-13T12:00:00+00:00"
    monitor   TEXT    NOT NULL,          -- "ping" | "dns" | "http" | "speedtest" | "ip"
    target    TEXT    NOT NULL,          -- host, URL, or "external"
    metric    TEXT    NOT NULL,          -- "latency_ms" | "resolution_ms" | "download_mbps" | …
    value     REAL,                      -- numeric measurement; -1 = error
    status    TEXT                       -- "ok" | "degraded" | "down" | "unknown"
);

CREATE INDEX IF NOT EXISTS idx_metrics_timestamp       ON metrics (timestamp);
CREATE INDEX IF NOT EXISTS idx_metrics_monitor_ts      ON metrics (monitor, timestamp);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    event_type      TEXT NOT NULL,   -- "status_change" | "ip_change"
    monitor         TEXT NOT NULL,
    description     TEXT,
    previous_status TEXT,
    new_status      TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events (timestamp);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,          -- e.g. "external_ip"
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
