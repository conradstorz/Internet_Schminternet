# Alert Queuing & Startup Notification — Design Spec

**Date:** 2026-03-14
**Project:** Internet Schminternet
**Scope:** Persistent alert queuing for SMTP failures, startup event logging, and downtime notification

---

## Problem

When the internet goes completely down, `EmailAlerter._send()` fails because SMTP is unreachable. The alert is currently logged as an error and silently dropped. The user never receives the "down" notification. On power loss or crash, the service restarts with no record of how long it was offline.

---

## Goals

- Alerts that fail to send must be queued persistently (survives restarts) and delivered as soon as connectivity returns.
- Every restart must emit a startup event with the shutdown type (clean vs. unclean) and approximate downtime.
- Queued alert emails must include the original event time, the time they were queued, and the actual delivery time.

---

## Out of Scope

- Degraded-condition alerting (separate spec)
- Pluggable notification channels (separate spec)

---

## Design

### 1. Database — `alert_queue` table

A new table in the existing SQLite database (`storage/db.py`):

```sql
CREATE TABLE IF NOT EXISTS alert_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,   -- ISO-8601 UTC: when the event occurred
    queued_at       TEXT NOT NULL,   -- ISO-8601 UTC: when the send first failed (set to NOW() by enqueue_alert)
    last_attempt_at TEXT,            -- ISO-8601 UTC: most recent retry attempt
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL    -- pre-rendered body including original timestamps
);
```

**New `storage/db.py` functions:**

- `enqueue_alert(created_at, subject, body) -> None` — inserts a new row; `queued_at` is set to `datetime.now(UTC)` internally
- `get_pending_alerts() -> list[dict]` — **non-destructive read**; returns all rows ordered by `id ASC`; does not remove or modify any rows
- `delete_queued_alert(id) -> None` — removes a successfully delivered row
- `update_alert_attempt(id) -> None` — bumps `attempt_count` and sets `last_attempt_at` to NOW()

**Queue age limit:** `cleanup_old()` is extended to also delete `alert_queue` rows where `queued_at < datetime('now', '-N days')`, using the same `retention_days` config value as `metrics` and `events`. This is the sole discard mechanism — there is no `attempt_count`-based cap. Rows that are undeliverable for longer than the retention window are silently discarded; this handles the case of permanently misconfigured SMTP.

`schema.sql` is documentation only (not executed at runtime). Update it to include the new table DDL to keep it in sync with the in-code `_SCHEMA` string in `db.py`.

---

### 2. EmailAlerter changes (`alerts/email_alert.py`)

**`db` access:** `alerts/email_alert.py` imports `storage.db as db` at the module level (same pattern as `monitors/ip_tracker.py`).

**`created_at` parameter:** Both `send_alert` and `send_recovery` gain a `created_at: str` parameter (ISO-8601 UTC string). The caller — `run_monitor()` in `main.py` — passes `results[0].timestamp`, which is the timestamp of the monitor result that triggered the alert. This value is baked into the rendered email body and stored in `alert_queue.created_at`.

**Where enqueuing happens:** Enqueuing is done in `send_alert` and `send_recovery` (not in `_send()`), because `created_at` is available in the callers. Both alert and recovery emails are queued on failure so the user receives notification of both events when connectivity returns.

**Updated `send_alert` signature and flow:**
```
async def send_alert(self, monitor, description, new_status, previous_status, created_at) -> None:
1. Check enabled + cooldown
2. Render subject and body (including "Event time (UTC): <created_at>")
3. sent = await _send(subject, body)
4. If sent:  set cooldown
5. If !sent: await db.enqueue_alert(created_at, subject, body)
   # No cooldown set on failure; next direct attempt will go through
```

**Updated `send_recovery` signature and flow:**
```
async def send_recovery(self, monitor, description, created_at) -> None:
1. Check enabled
2. Render subject and body (including "Event time (UTC): <created_at>")
3. sent = await _send(subject, body)
4. Clear cooldown regardless of send result
5. If !sent: await db.enqueue_alert(created_at, subject, body)
```

**Out-of-order delivery:** If SMTP is down during both a `down` transition and the subsequent `ok` recovery, the queue will contain an alert email followed by a recovery email for the same monitor. `flush_alert_queue` delivers them in insertion order (`id ASC`), which matches the chronological order they were queued. This is accepted behaviour — the user sees both emails in the correct sequence when connectivity returns.

**New method `flush_alert_queue()`:**

```python
async def flush_alert_queue(self) -> None:
```

1. If not `_enabled()`, return immediately.
2. Call `db.get_pending_alerts()` to fetch all rows (non-destructive read).
3. If empty, return (no-op).
4. For each row:
   a. Append `Delivered at (UTC): <now>` to the stored body.
   b. Attempt `_send(subject, amended_body)`.
   c. On success: call `db.delete_queued_alert(id)` and log delivery.
   d. On failure: call `db.update_alert_attempt(id)` and continue to the next item.

**No cooldown checks in `flush_alert_queue`:** Rows in the queue represent committed intent to deliver. The cooldown is a rate-limit on new alert generation, not on delivery of already-queued items. Applying cooldown logic inside flush would require a structured `monitor` column and would risk suppressing legitimate queued notifications.

**Concurrency safety:** All callers of `flush_alert_queue` run on the same asyncio event loop. Because `get_pending_alerts` is non-destructive and `await` points ensure cooperative scheduling, concurrent flush invocations serialize naturally. If flush A reads rows 1–3 and flush B reads rows 1–3 before A deletes them, the worst case is two delivery attempts for the same row. The second `_send` will succeed and call `delete_queued_alert` on an already-deleted row — this must be handled gracefully (no error if the row is already gone). `delete_queued_alert` should use `DELETE WHERE id = ?` which is a no-op if the row is absent.

---

### 3. Flush triggers (`main.py`)

Three places call `await _alerter.flush_alert_queue()`:

| Trigger | Location | Notes |
|---------|----------|-------|
| **Startup** | `main()`, after the startup email (§5 Step 5) | Best-effort; if internet is still down at boot, items stay queued for the recovery trigger |
| **Monitor recovery** | `run_monitor()`, when `new_status == "ok" and previous == "down"` | Primary delivery path; fires within one poll cycle of connectivity returning. Multiple monitors recovering simultaneously will trigger multiple flush calls — safe and idempotent |
| **Periodic fallback** | New scheduler job: `interval`, every 5 minutes | Catches partial-connectivity edge cases where monitors never fully transition through `down` |

---

### 4. Heartbeat & shutdown tracking

Two values written to the existing `state` table:

| Key | Written | Value |
|-----|---------|-------|
| `last_seen_at` | Every 60 seconds by a heartbeat scheduler job | ISO-8601 UTC timestamp |
| `shutdown_at` | On graceful shutdown, before `scheduler.shutdown()` | ISO-8601 UTC timestamp |

**New scheduler job:**
```python
scheduler.add_job(job_heartbeat, "interval", seconds=60, id="heartbeat")
```

**Graceful shutdown** (in `_handle_signal` / after `stop_event` is set, before `scheduler.shutdown()`):
```python
await db.set_state("shutdown_at", datetime.now(timezone.utc).isoformat())
```

---

### 5. Startup sequence (`main.py`)

**Ordering requirement:** This sequence runs after `db.init_db()`, after `_alerter = EmailAlerter(...)`, and before `scheduler.start()`.

**Step 1 — Read prior state:**
```python
shutdown_at  = await db.get_state("shutdown_at")   # non-empty ISO string, "" or None
last_seen_at = await db.get_state("last_seen_at")   # non-empty ISO string, "" or None
```

**Step 2 — Classify startup:**

`""` and `None` are treated identically (both mean "not set").

| Condition | Type | Downtime |
|-----------|------|----------|
| `shutdown_at` is a non-empty ISO string | Clean shutdown | `now - shutdown_at` |
| `shutdown_at` absent/empty, `last_seen_at` is non-empty | Unclean (power loss/crash) | `now - last_seen_at` (±60s) |
| Both absent/empty | First run | N/A — no startup email sent |

**Step 3 — Insert startup event into `events` table** (always, including first run):
```python
await db.insert_event({
    "timestamp": startup_ts,
    "event_type": "startup",
    "monitor": "system",
    "description": "<shutdown_type>. Downtime: <formatted_duration>",
    "previous_status": "",
    "new_status": "unknown",
})
```
On first run, description is `"First run."` with no downtime line.

**Step 4 — Count queued alerts** (before the flush, so the count in the startup email is accurate):
```python
pending = await db.get_pending_alerts()
queued_count = len(pending)
```

**Step 5 — Send startup alert email** (if alerts enabled AND not first run):

```
Subject: [Schminternet] Service restarted

Service restarted at:  2026-03-14T02:47:33 UTC
Last seen at:          2026-03-14T02:14:58 UTC
Shutdown type:         Unclean (power loss or crash)
Downtime (approx):     32 minutes 35 seconds

2 alert(s) were queued during the outage and will follow this email.
```

If `queued_count == 0`, omit the final sentence. The startup email is sent directly via `_send()` and is **not** queued on failure (it is best-effort; the queued alerts that follow are the primary notification payload).

**Step 6 — Flush queue:**
```python
await _alerter.flush_alert_queue()
```

**Step 7 — Clear `shutdown_at`:**
```python
await db.set_state("shutdown_at", "")
```
Only `shutdown_at` is cleared here. `last_seen_at` is **not** cleared at this point — it is left stale and will be overwritten by the heartbeat job once the scheduler starts. This avoids the window where clearing `last_seen_at` before `scheduler.start()` would cause a subsequent crash to be classified as a "first run."

---

## Data Flow

```
SMTP fails during send_alert / send_recovery
        │
        ▼
db.enqueue_alert() → alert_queue table (SQLite, persists across restarts)
        │
        ├── On startup        → flush_alert_queue() [best-effort]
        ├── On monitor ok recovery → flush_alert_queue() [primary path]
        └── Every 5 min (scheduler) → flush_alert_queue() [fallback]
                │
                ▼
        _send() succeeds → delete_queued_alert() [no-op if already deleted]
        _send() fails   → update_alert_attempt()
                              │
                              └── row age > retention_days → cleaned by daily cleanup_old()
```

---

## Files Changed

| File | Change |
|------|--------|
| `storage/db.py` | Add `alert_queue` DDL to `_SCHEMA`; add `enqueue_alert`, `get_pending_alerts`, `delete_queued_alert`, `update_alert_attempt`; extend `cleanup_old()` to prune `alert_queue` |
| `storage/schema.sql` | Add `alert_queue` DDL (documentation only) |
| `alerts/email_alert.py` | Add `import storage.db as db`; add `created_at` param to `send_alert` and `send_recovery`; enqueue on failure; add `flush_alert_queue()` |
| `main.py` | Update all `send_alert`/`send_recovery` call sites to pass `created_at`; add startup sequence (§5); add heartbeat job; add flush-on-recovery in `run_monitor()`; write `shutdown_at` on graceful shutdown |

---

## Testing

**`tests/test_alert_queue.py`** (new):
- Failed `send_alert` enqueues the alert with correct `created_at`, `subject`, `body`
- Failed `send_recovery` is also enqueued
- `flush_alert_queue` delivers queued alerts when SMTP recovers
- `flush_alert_queue` leaves failed rows in queue and increments `attempt_count`
- Delivered email body includes all three timestamps (event time, queued at, delivered at)
- `flush_alert_queue` is a no-op when queue is empty
- `delete_queued_alert` on an already-deleted row does not raise
- Multiple concurrent `flush_alert_queue` calls are safe (asyncio serializes; idempotent deletes handle any overlap)
- `flush_alert_queue` skips cooldown checks and delivers all queued rows

**`tests/test_startup.py`** (new):
- Clean shutdown: downtime calculated from `shutdown_at`
- Unclean shutdown: downtime calculated from `last_seen_at`
- First run: no startup email sent; event description is `"First run."`
- `shutdown_at = ""` treated identically to `shutdown_at = None` (unclean/first-run path)
- `shutdown_at` cleared after startup sequence; `last_seen_at` is NOT cleared
- Startup event written to `events` table on every boot
- Queued alert count in startup email matches `get_pending_alerts()` result at send time
- `queued_count == 0`: final sentence omitted from startup email body
