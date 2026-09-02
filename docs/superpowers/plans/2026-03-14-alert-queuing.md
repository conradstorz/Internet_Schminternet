# Alert Queuing & Startup Notification Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persistent alert queuing so SMTP failures during outages survive reboots, and emit a startup email on every boot that reports shutdown type and downtime.

**Architecture:** A new `alert_queue` SQLite table stores unsent emails. `EmailAlerter` enqueues on SMTP failure and gains `flush_alert_queue()`, called at startup, on monitor recovery, and every 5 minutes. A heartbeat/shutdown mechanism writes timestamps to the `state` table so each boot can calculate and report how long the service was offline.

**Tech Stack:** Python asyncio, aiosqlite, APScheduler, smtplib (in thread executor)

**Spec:** `docs/superpowers/specs/2026-03-14-alert-queuing-design.md`

---

## Chunk 1: DB Layer

### Task 1: alert_queue table DDL

**Files:**
- Modify: `storage/db.py` — append DDL to `_SCHEMA`
- Modify: `storage/schema.sql` — documentation copy

- [ ] **Step 1: Add DDL to `_SCHEMA` in `storage/db.py`**

Append inside the `_SCHEMA` string, after the `state` table block:

```sql
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
```

- [ ] **Step 2: Mirror in `storage/schema.sql`**

Append the same DDL block to `storage/schema.sql` (documentation only — never executed at runtime).

- [ ] **Step 3: Verify existing tests still pass**

```
uv run pytest tests/test_db.py -v
```
Expected: all PASS — `CREATE TABLE IF NOT EXISTS` is idempotent.

- [ ] **Step 4: Commit**

```
git add storage/db.py storage/schema.sql
git commit -m "feat: add alert_queue table schema"
```

---

### Task 2: enqueue_alert + get_pending_alerts

**Files:**
- Modify: `storage/db.py`
- Modify: `tests/test_db.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_db.py` (after the existing `test_insert_and_get_events` test):

```python
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
    assert rows[0]["subject"] == "first"
    assert rows[1]["subject"] == "second"
```

- [ ] **Step 2: Run — verify failure**

```
uv run pytest tests/test_db.py::test_enqueue_and_get_pending_alerts -v
```
Expected: `AttributeError: module 'storage.db' has no attribute 'enqueue_alert'`

- [ ] **Step 3: Implement in `storage/db.py`**

Add after `set_state`:

```python
async def enqueue_alert(created_at: str, subject: str, body: str) -> None:
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
```

- [ ] **Step 4: Run — verify pass**

```
uv run pytest tests/test_db.py::test_enqueue_and_get_pending_alerts tests/test_db.py::test_get_pending_alerts_is_non_destructive tests/test_db.py::test_get_pending_alerts_ordered_oldest_first -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add storage/db.py tests/test_db.py
git commit -m "feat: add enqueue_alert and get_pending_alerts"
```

---

### Task 3: delete_queued_alert + update_alert_attempt

**Files:**
- Modify: `storage/db.py`
- Modify: `tests/test_db.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_db.py`:

```python
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
```

- [ ] **Step 2: Run — verify failure**

```
uv run pytest tests/test_db.py::test_delete_queued_alert_removes_row -v
```
Expected: `AttributeError: module 'storage.db' has no attribute 'delete_queued_alert'`

- [ ] **Step 3: Implement in `storage/db.py`**

Add after `get_pending_alerts`:

```python
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
```

- [ ] **Step 4: Run — verify pass**

```
uv run pytest tests/test_db.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add storage/db.py tests/test_db.py
git commit -m "feat: add delete_queued_alert and update_alert_attempt"
```

---

### Task 4: Extend cleanup_old to prune alert_queue

**Files:**
- Modify: `storage/db.py`
- Modify: `tests/test_db.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_db.py`:

```python
async def test_cleanup_old_prunes_stale_alert_queue_rows():
    await db.init_db()
    # Insert a row with an old queued_at directly (enqueue_alert always uses NOW())
    old_ts = "2020-01-01T00:00:00+00:00"
    async with aiosqlite.connect(db._DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO alert_queue (created_at, queued_at, subject, body) "
            "VALUES (?,?,?,?)",
            (old_ts, old_ts, "old alert", "body"),
        )
        await conn.commit()
    # Insert a fresh row
    fresh_ts = datetime.now(timezone.utc).isoformat()
    await db.enqueue_alert(fresh_ts, "fresh alert", "body")

    await db.cleanup_old(days=30)

    rows = await db.get_pending_alerts()
    assert len(rows) == 1
    assert rows[0]["subject"] == "fresh alert"
```

- [ ] **Step 2: Run — verify failure**

```
uv run pytest tests/test_db.py::test_cleanup_old_prunes_stale_alert_queue_rows -v
```
Expected: FAIL — old row survives because `cleanup_old` doesn't touch `alert_queue` yet.

- [ ] **Step 3: Update `cleanup_old` in `storage/db.py`**

Replace the existing `cleanup_old` function:

```python
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
```

- [ ] **Step 4: Run — verify pass**

```
uv run pytest tests/test_db.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add storage/db.py tests/test_db.py
git commit -m "feat: extend cleanup_old to prune stale alert_queue rows"
```

---

## Chunk 2: EmailAlerter

### Task 5: Add created_at param and enqueue on failure

**Files:**
- Modify: `alerts/email_alert.py`
- Modify: `tests/test_email_alert.py` — update existing call sites to pass `created_at`
- Create: `tests/test_alert_queue.py`

- [ ] **Step 1: Create `tests/test_alert_queue.py` with failing tests**

```python
"""Tests for alert queuing — EmailAlerter enqueue-on-failure behaviour."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import storage.db as db
from alerts.email_alert import EmailAlerter


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db.configure(str(tmp_path / "test.db"))


def _cfg(**overrides) -> dict:
    base = {
        "enabled": True,
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "use_tls": True,
        "from_addr": "from@example.com",
        "to_addrs": ["to@example.com"],
        "username": "user",
        "password": "pass",
    }
    return {**base, **overrides}


def _smtp_success():
    smtp = MagicMock()
    smtp.__enter__ = MagicMock(return_value=smtp)
    smtp.__exit__ = MagicMock(return_value=False)
    return smtp


async def test_failed_send_alert_enqueues_row():
    await db.init_db()
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError):
        await alerter.send_alert("ping", "went down", "down", "ok", ts)
    rows = await db.get_pending_alerts()
    assert len(rows) == 1
    assert rows[0]["created_at"] == ts
    assert "PING" in rows[0]["subject"]


async def test_failed_send_recovery_enqueues_row():
    await db.init_db()
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError):
        await alerter.send_recovery("ping", "back up", ts)
    rows = await db.get_pending_alerts()
    assert len(rows) == 1
    assert "PING" in rows[0]["subject"]


async def test_successful_send_does_not_enqueue():
    await db.init_db()
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", return_value=_smtp_success()):
        await alerter.send_alert("ping", "went down", "down", "ok", ts)
    assert await db.get_pending_alerts() == []


async def test_body_includes_event_time():
    """The rendered email body must include the original event timestamp."""
    await db.init_db()
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError):
        await alerter.send_alert("ping", "went down", "down", "ok", ts)
    rows = await db.get_pending_alerts()
    assert ts in rows[0]["body"]
```

- [ ] **Step 2: Run — verify failure**

```
uv run pytest tests/test_alert_queue.py::test_failed_send_alert_enqueues_row -v
```
Expected: `TypeError: send_alert() takes 5 positional arguments but 6 were given` (or similar — the new `created_at` param doesn't exist yet).

- [ ] **Step 3: Update `alerts/email_alert.py`**

Add import at the top:
```python
import storage.db as db
```

Update `send_alert` signature and body rendering. Replace the existing method:

```python
async def send_alert(
    self,
    monitor: str,
    description: str,
    new_status: str,
    previous_status: str,
    created_at: str,
) -> None:
    """Emit a 'monitor is DOWN' email, respecting the cooldown window."""
    if not self._enabled():
        return
    if self._in_cooldown(monitor):
        logger.debug("Alert suppressed for %s (in cooldown)", monitor)
        return

    subject = f"[Schminternet] {monitor.upper()} is {new_status.upper()}"
    body = (
        f"Monitor:          {monitor}\n"
        f"Previous status:  {previous_status}\n"
        f"Current status:   {new_status}\n"
        f"Details:          {description}\n"
        f"Event time (UTC): {created_at}\n"
    )
    sent = await self._send(subject, body)
    if sent:
        self._cooldowns[monitor] = datetime.now(timezone.utc)
    else:
        await db.enqueue_alert(created_at, subject, body)
```

Update `send_recovery` signature and body rendering. Replace the existing method:

```python
async def send_recovery(self, monitor: str, description: str, created_at: str) -> None:
    """Emit a 'monitor has RECOVERED' email and clear the cooldown."""
    if not self._enabled():
        return

    subject = f"[Schminternet] {monitor.upper()} has RECOVERED"
    body = (
        f"Monitor:          {monitor}\n"
        f"Status:           OK (recovered)\n"
        f"Details:          {description}\n"
        f"Event time (UTC): {created_at}\n"
    )
    sent = await self._send(subject, body)
    self._cooldowns.pop(monitor, None)
    if not sent:
        await db.enqueue_alert(created_at, subject, body)
```

- [ ] **Step 4: Update `tests/test_email_alert.py`**

After the implementation change, any test that triggers a failed SMTP send will hit `db.enqueue_alert`, which requires a configured and initialized database. Add the following at the top of the file:

**Add import** (after the existing imports):
```python
from datetime import datetime, timezone
import storage.db as db
```

**Add `autouse` fixture** (after the `_mock_smtp_success` helper):
```python
@pytest.fixture(autouse=True)
async def isolated_db(tmp_path):
    db.configure(str(tmp_path / "test.db"))
    await db.init_db()
```

The `async def isolated_db` fixture is autouse and applies to every test in the file. With `pytest-asyncio >= 0.21` in `asyncio_mode = auto`, async fixtures cannot be injected into synchronous `def` test functions. Convert the two `inspect` tests to `async def` — they don't await anything, so no logic changes:

```python
async def test_send_alert_is_a_coroutine():
    alerter = EmailAlerter(_cfg())
    assert inspect.iscoroutinefunction(alerter.send_alert)


async def test_send_recovery_is_a_coroutine():
    alerter = EmailAlerter(_cfg())
    assert inspect.iscoroutinefunction(alerter.send_recovery)
```

Now update the remaining call sites:

**`test_cooldown_not_set_when_send_fails`** — add `ts`:
```python
async def test_cooldown_not_set_when_send_fails():
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
        await alerter.send_alert("ping", "went down", "down", "ok", ts)
    assert "ping" not in alerter._cooldowns
```

**`test_cooldown_set_when_send_succeeds`** — add `ts`:
```python
async def test_cooldown_set_when_send_succeeds():
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", return_value=_mock_smtp_success()):
        await alerter.send_alert("ping", "went down", "down", "ok", ts)
    assert "ping" in alerter._cooldowns
```

**`test_recovery_clears_cooldown_on_success`** — add `ts` to both calls:
```python
async def test_recovery_clears_cooldown_on_success():
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", return_value=_mock_smtp_success()):
        await alerter.send_alert("ping", "went down", "down", "ok", ts)
    assert "ping" in alerter._cooldowns
    with patch("smtplib.SMTP", return_value=_mock_smtp_success()):
        await alerter.send_recovery("ping", "back up", ts)
    assert "ping" not in alerter._cooldowns
```

**`test_disabled_alerter_sends_nothing`** — add `ts`:
```python
async def test_disabled_alerter_sends_nothing():
    alerter = EmailAlerter(_cfg(enabled=False))
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP") as mock_smtp:
        await alerter.send_alert("ping", "down", "down", "ok", ts)
        mock_smtp.assert_not_called()
```

- [ ] **Step 5: Run — verify pass**

```
uv run pytest tests/test_alert_queue.py tests/test_email_alert.py -v
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```
git add alerts/email_alert.py tests/test_email_alert.py tests/test_alert_queue.py
git commit -m "feat: enqueue alerts on SMTP failure, add created_at param"
```

---

### Task 6: flush_alert_queue

> **Schema note:** This task uses `queued_at` as a column name in `alert_queue`. That column was added in Task 1. The body appended by `flush_alert_queue` labels it `Queued at (UTC):` to match.
>
> **Spec Goals alignment:** The spec Goals section states: "Queued alert emails must include the original event time, the time they were queued, and the actual delivery time." `queued_at` exists only in the DB row and is not in the stored `body` — the only opportunity to include it in the delivered email is inside `flush_alert_queue`. Spec step 4a ("Append `Delivered at (UTC): <now>`") is shorthand; the implementation appends both `queued_at` and `delivered_at` to fulfil the full Goals requirement. The test `test_flush_appends_all_three_timestamps` validates this explicitly.

**Files:**
- Modify: `alerts/email_alert.py`
- Modify: `tests/test_alert_queue.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_alert_queue.py`:

```python
async def test_flush_delivers_queued_alert():
    await db.init_db()
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError):
        await alerter.send_alert("ping", "down", "down", "ok", ts)
    assert len(await db.get_pending_alerts()) == 1

    with patch("smtplib.SMTP", return_value=_smtp_success()):
        await alerter.flush_alert_queue()
    assert await db.get_pending_alerts() == []


async def test_flush_leaves_row_on_continued_failure():
    await db.init_db()
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError):
        await alerter.send_alert("ping", "down", "down", "ok", ts)
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError):
        await alerter.flush_alert_queue()
    rows = await db.get_pending_alerts()
    assert len(rows) == 1
    assert rows[0]["attempt_count"] == 1


async def test_flush_appends_all_three_timestamps():
    """Delivered email must contain event time, queued-at, and delivered-at."""
    await db.init_db()
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError):
        await alerter.send_alert("ping", "down", "down", "ok", ts)

    captured: list[str] = []

    def capturing_smtp(*_a, **_kw):
        server = MagicMock()
        server.sendmail = lambda *a: captured.append(a[2])
        smtp = MagicMock()
        smtp.__enter__ = MagicMock(return_value=server)
        smtp.__exit__ = MagicMock(return_value=False)
        return smtp

    with patch("smtplib.SMTP", side_effect=capturing_smtp):
        await alerter.flush_alert_queue()

    assert len(captured) == 1
    assert "Event time (UTC)" in captured[0]
    assert "Queued at (UTC)" in captured[0]
    assert "Delivered at (UTC)" in captured[0]


async def test_flush_is_noop_on_empty_queue():
    await db.init_db()
    alerter = EmailAlerter(_cfg())
    with patch("smtplib.SMTP") as mock_smtp:
        await alerter.flush_alert_queue()
    mock_smtp.assert_not_called()


async def test_flush_is_noop_when_disabled():
    await db.init_db()
    alerter = EmailAlerter(_cfg(enabled=False))
    ts = datetime.now(timezone.utc).isoformat()
    await db.enqueue_alert(ts, "subject", "body")
    with patch("smtplib.SMTP") as mock_smtp:
        await alerter.flush_alert_queue()
    mock_smtp.assert_not_called()
    assert len(await db.get_pending_alerts()) == 1


async def test_flush_skips_cooldown_checks():
    """Rows in the queue are delivered regardless of cooldown state."""
    await db.init_db()
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    await db.enqueue_alert(ts, "[Schminternet] PING is DOWN", f"Event time (UTC): {ts}\n")
    alerter._cooldowns["ping"] = datetime.now(timezone.utc)  # artificially in cooldown
    with patch("smtplib.SMTP", return_value=_smtp_success()):
        await alerter.flush_alert_queue()
    assert await db.get_pending_alerts() == []


async def test_flush_idempotent_delete_does_not_raise():
    """delete_queued_alert on an already-deleted row must be silent."""
    await db.init_db()
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    await db.enqueue_alert(ts, "subject", "body")
    rows = await db.get_pending_alerts()
    # Pre-delete the row so flush finds it gone
    await db.delete_queued_alert(rows[0]["id"])
    with patch("smtplib.SMTP", return_value=_smtp_success()):
        await alerter.flush_alert_queue()  # must not raise
```

- [ ] **Step 2: Run — verify failure**

```
uv run pytest tests/test_alert_queue.py::test_flush_delivers_queued_alert -v
```
Expected: `AttributeError: 'EmailAlerter' object has no attribute 'flush_alert_queue'`

- [ ] **Step 3: Implement `flush_alert_queue` in `alerts/email_alert.py`**

Add after `send_recovery`:

```python
async def flush_alert_queue(self) -> None:
    """Attempt to deliver all queued alerts. Safe to call at any time."""
    if not self._enabled():
        return
    rows = await db.get_pending_alerts()
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for row in rows:
        amended_body = (
            row["body"]
            + f"Queued at (UTC):   {row['queued_at']}\n"
            + f"Delivered at (UTC): {now}\n"
        )
        sent = await self._send(row["subject"], amended_body)
        if sent:
            await db.delete_queued_alert(row["id"])
            logger.info(
                "Delivered queued alert id=%d: %s", row["id"], row["subject"]
            )
        else:
            await db.update_alert_attempt(row["id"])
```

- [ ] **Step 4: Run — verify pass**

```
uv run pytest tests/test_alert_queue.py -v
```
Expected: all PASS.

- [ ] **Step 5: Run full suite**

```
uv run pytest tests/ -v
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```
git add alerts/email_alert.py tests/test_alert_queue.py
git commit -m "feat: add flush_alert_queue to EmailAlerter"
```

---

## Chunk 3: main.py

### Task 7: Extract _startup_sequence helper

**Files:**
- Modify: `main.py` — add module-level `_startup_sequence` function
- Create: `tests/test_startup.py`

The startup sequence is extracted as a standalone async function so it can be tested in isolation.

- [ ] **Step 1: Create `tests/test_startup.py` with failing tests**

```python
"""Tests for startup sequence — downtime classification and event/email emission."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import storage.db as db
from main import _startup_sequence


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db.configure(str(tmp_path / "test.db"))


def _alerter(enabled: bool = False) -> MagicMock:
    """Return a mock EmailAlerter. Disabled by default (no real SMTP)."""
    alerter = MagicMock()
    alerter._enabled = MagicMock(return_value=enabled)
    alerter._send = AsyncMock(return_value=True)
    alerter.flush_alert_queue = AsyncMock()
    return alerter


async def test_first_run_writes_event_and_sends_no_email():
    await db.init_db()
    alerter = _alerter(enabled=True)
    await _startup_sequence(alerter)
    events = await db.get_events()
    assert len(events) == 1
    assert events[0]["event_type"] == "startup"
    assert events[0]["description"] == "First run."
    alerter._send.assert_not_called()


async def test_clean_shutdown_calculates_downtime():
    await db.init_db()
    now = datetime(2026, 3, 14, 3, 0, 0, tzinfo=timezone.utc)
    shutdown = now - timedelta(minutes=32, seconds=35)
    await db.set_state("shutdown_at", shutdown.isoformat())
    await db.set_state("last_seen_at", shutdown.isoformat())

    alerter = _alerter(enabled=True)
    await _startup_sequence(alerter, now=now)

    events = await db.get_events()
    assert "Clean" in events[0]["description"]
    assert "32m" in events[0]["description"] or "32 m" in events[0]["description"]


async def test_unclean_shutdown_uses_last_seen_at():
    await db.init_db()
    now = datetime(2026, 3, 14, 3, 0, 0, tzinfo=timezone.utc)
    last_seen = now - timedelta(minutes=15)
    await db.set_state("last_seen_at", last_seen.isoformat())
    # No shutdown_at set

    alerter = _alerter(enabled=True)
    await _startup_sequence(alerter, now=now)

    events = await db.get_events()
    assert "Unclean" in events[0]["description"]
    assert "15m" in events[0]["description"] or "15 m" in events[0]["description"]


async def test_empty_string_shutdown_at_treated_as_unclean():
    await db.init_db()
    now = datetime(2026, 3, 14, 3, 0, 0, tzinfo=timezone.utc)
    last_seen = now - timedelta(minutes=5)
    await db.set_state("shutdown_at", "")   # empty string = not set
    await db.set_state("last_seen_at", last_seen.isoformat())

    alerter = _alerter(enabled=True)
    await _startup_sequence(alerter, now=now)

    events = await db.get_events()
    assert "Unclean" in events[0]["description"]


async def test_shutdown_at_cleared_after_startup():
    await db.init_db()
    now = datetime(2026, 3, 14, 3, 0, 0, tzinfo=timezone.utc)
    shutdown = now - timedelta(minutes=5)
    await db.set_state("shutdown_at", shutdown.isoformat())

    alerter = _alerter()
    await _startup_sequence(alerter, now=now)

    assert not (await db.get_state("shutdown_at"))


async def test_last_seen_at_not_cleared_after_startup():
    await db.init_db()
    now = datetime(2026, 3, 14, 3, 0, 0, tzinfo=timezone.utc)
    last_seen = now - timedelta(minutes=5)
    await db.set_state("shutdown_at", (now - timedelta(minutes=2)).isoformat())
    await db.set_state("last_seen_at", last_seen.isoformat())

    alerter = _alerter()
    await _startup_sequence(alerter, now=now)

    assert await db.get_state("last_seen_at") == last_seen.isoformat()


async def test_startup_email_includes_queued_count():
    await db.init_db()
    now = datetime(2026, 3, 14, 3, 0, 0, tzinfo=timezone.utc)
    shutdown = now - timedelta(minutes=10)
    await db.set_state("shutdown_at", shutdown.isoformat())
    # Pre-queue two alerts
    ts = shutdown.isoformat()
    await db.enqueue_alert(ts, "Alert 1", "body1")
    await db.enqueue_alert(ts, "Alert 2", "body2")

    alerter = _alerter(enabled=True)
    await _startup_sequence(alerter, now=now)

    # Verify the startup email body mentions 2 queued alerts
    call_args = alerter._send.call_args
    body = call_args[0][1]  # second positional arg to _send
    assert "2 alert" in body


async def test_startup_email_omits_queued_sentence_when_none():
    await db.init_db()
    now = datetime(2026, 3, 14, 3, 0, 0, tzinfo=timezone.utc)
    shutdown = now - timedelta(minutes=5)
    await db.set_state("shutdown_at", shutdown.isoformat())

    alerter = _alerter(enabled=True)
    await _startup_sequence(alerter, now=now)

    call_args = alerter._send.call_args
    body = call_args[0][1]
    assert "alert" not in body.lower()


async def test_flush_called_after_startup_email():
    await db.init_db()
    now = datetime(2026, 3, 14, 3, 0, 0, tzinfo=timezone.utc)
    shutdown = now - timedelta(minutes=5)
    await db.set_state("shutdown_at", shutdown.isoformat())

    alerter = _alerter(enabled=True)
    await _startup_sequence(alerter, now=now)

    alerter.flush_alert_queue.assert_called_once()
```

- [ ] **Step 2: Run — verify failure**

```
uv run pytest tests/test_startup.py::test_first_run_writes_event_and_sends_no_email -v
```
Expected: `ImportError: cannot import name '_startup_sequence' from 'main'`

- [ ] **Step 3: Implement `_startup_sequence` in `main.py`**

Add this module-level async function above `async def main()`. It requires `Optional` from `typing` (already imported in main.py) and `datetime, timezone` (already imported):

```python
async def _startup_sequence(
    alerter: "EmailAlerter",
    now: Optional[datetime] = None,
) -> None:
    """Classify startup type, emit event, send startup email, flush queue."""
    if now is None:
        now = datetime.now(timezone.utc)
    startup_ts = now.isoformat(timespec="seconds")

    shutdown_at  = await db.get_state("shutdown_at")  or ""
    last_seen_at = await db.get_state("last_seen_at") or ""

    # Classify
    if shutdown_at:
        shutdown_type = "clean"
        reference_ts  = shutdown_at
    elif last_seen_at:
        shutdown_type = "unclean"
        reference_ts  = last_seen_at
    else:
        shutdown_type = "first_run"
        reference_ts  = None

    # Format downtime
    downtime_str = ""
    if reference_ts:
        ref_dt   = datetime.fromisoformat(reference_ts)
        total_s  = max(0, int((now - ref_dt).total_seconds()))
        hours, remainder = divmod(total_s, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            downtime_str = f"{hours}h {minutes}m {seconds}s"
        elif minutes:
            downtime_str = f"{minutes}m {seconds}s"
        else:
            downtime_str = f"{seconds}s"

    # Build event description
    if shutdown_type == "first_run":
        description = "First run."
    elif shutdown_type == "clean":
        description = f"Clean shutdown. Downtime: {downtime_str}"
    else:
        description = f"Unclean shutdown (power loss or crash). Downtime (approx): {downtime_str}"

    await db.insert_event({
        "timestamp": startup_ts,
        "event_type": "startup",
        "monitor": "system",
        "description": description,
        "previous_status": "",
        "new_status": "unknown",
    })
    logger.info("Startup: %s", description)

    if shutdown_type == "first_run":
        return

    # Count pending alerts before flush (for email body accuracy)
    pending      = await db.get_pending_alerts()
    queued_count = len(pending)

    # Send startup email (best-effort — not queued on failure)
    if alerter._enabled():
        shutdown_label = (
            "Clean shutdown"
            if shutdown_type == "clean"
            else "Unclean (power loss or crash)"
        )
        body = (
            f"Service restarted at:  {startup_ts} UTC\n"
            f"Last seen at:          {reference_ts} UTC\n"
            f"Shutdown type:         {shutdown_label}\n"
            f"Downtime (approx):     {downtime_str}\n"
        )
        if queued_count > 0:
            body += f"\n{queued_count} alert(s) were queued during the outage and will follow this email.\n"
        await alerter._send("[Schminternet] Service restarted", body)

    # Flush queued alerts
    await alerter.flush_alert_queue()

    # Clear shutdown_at only (last_seen_at left for heartbeat to overwrite)
    await db.set_state("shutdown_at", "")
```

- [ ] **Step 4: Run — verify pass**

```
uv run pytest tests/test_startup.py -v
```
Expected: all PASS.

- [ ] **Step 5: Run full suite**

```
uv run pytest tests/ -v
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```
git add main.py tests/test_startup.py
git commit -m "feat: add _startup_sequence with downtime classification and startup email"
```

---

### Task 8: Heartbeat job + shutdown state

**Files:**
- Modify: `main.py`

No new tests needed — the heartbeat writes `last_seen_at` to the state table (already tested in `test_db.py`), and the shutdown write is a one-liner in the existing shutdown path.

- [ ] **Step 1: Add heartbeat job inside `main()`**

In the scheduler jobs section of `main()`, add after the existing job declarations:

```python
async def job_heartbeat() -> None:
    await db.set_state("last_seen_at", datetime.now(timezone.utc).isoformat())

async def job_flush_alerts() -> None:
    if _alerter:
        await _alerter.flush_alert_queue()
```

Then register both with the scheduler (after the existing `add_job` calls):

```python
scheduler.add_job(job_heartbeat,     "interval", seconds=60,  id="heartbeat")
scheduler.add_job(job_flush_alerts,  "interval", minutes=5,   id="alert_flush")
```

- [ ] **Step 2: Write shutdown_at before scheduler stops**

In `main()`, after `await stop_event.wait()` and before `scheduler.shutdown(wait=False)`:

```python
await db.set_state("shutdown_at", datetime.now(timezone.utc).isoformat())
```

- [ ] **Step 3: Verify existing tests still pass**

```
uv run pytest tests/ -v
```
Expected: all PASS.

- [ ] **Step 4: Commit**

```
git add main.py
git commit -m "feat: add heartbeat job, periodic alert flush, and shutdown state write"
```

---

### Task 9: Wire up startup sequence + flush-on-recovery + update call sites

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Call `_startup_sequence` inside `main()`**

In `main()`, after `_alerter = EmailAlerter(...)` and after `db.init_db()` (and before `scheduler.start()`), add:

```python
await _startup_sequence(_alerter)
```

- [ ] **Step 2: Add flush-on-recovery in `run_monitor()`**

In `run_monitor()`, the `elif new_status == "ok" and previous == "down":` block becomes:

```python
        elif new_status == "ok" and previous == "down":
            await _alerter.send_recovery(monitor_name, "Connection restored", ts)
            await _alerter.flush_alert_queue()
            await db.insert_event(
                {
                    "timestamp": ts,
                    "event_type": "status_change",
                    "monitor": monitor_name,
                    "description": f"Status recovered: {previous} → {new_status}",
                    "previous_status": previous,
                    "new_status": new_status,
                }
            )
```

The `flush_alert_queue` call is inside the `elif` block, at the same indentation as `send_recovery`.

- [ ] **Step 3: Update `send_alert` call site**

In `run_monitor()`, update the `send_alert` call to pass `ts` (already available as `ts = results[0].timestamp`):

```python
# Before:
await _alerter.send_alert(monitor_name, desc, new_status, previous)

# After:
await _alerter.send_alert(monitor_name, desc, new_status, previous, ts)
```

(`send_recovery` was already updated with `ts` in Step 2 above.)

- [ ] **Step 4: Run full suite**

```
uv run pytest tests/ -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add main.py
git commit -m "feat: wire startup sequence, flush-on-recovery, and update alert call sites"
```

---

## Final verification

- [ ] **Run complete test suite one last time**

```
uv run pytest tests/ -v
```
Expected: all PASS, no warnings about unknown config options.

- [ ] **Smoke-check imports**

```
uv run python -c "from main import _startup_sequence; from alerts.email_alert import EmailAlerter; print('OK')"
```
Expected: `OK`
