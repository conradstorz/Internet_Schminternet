# Degraded-Condition Alerting — Design Spec

**Date:** 2026-03-15
**Project:** Internet Schminternet
**Scope:** Email alerts when a monitor sustains a `degraded` status beyond a configurable threshold

---

## Problem

All five monitors can produce `degraded` status (slow ping, high DNS latency, slow HTTP response, below-expected speedtest), but `run_monitor()` currently ignores it. Degraded conditions are never logged as events and never trigger alerts. A slow internet connection that never fully drops produces no signal.

---

## Goals

- Log a `status_change` event whenever a monitor enters or leaves `degraded`.
- Send an email alert when a monitor sustains `degraded` status beyond a configurable time or cycle threshold.
- Degraded alerts share the existing per-monitor cooldown with `down` alerts.
- Failed degraded alert emails are queued and retried via the existing `alert_queue` mechanism.
- Threshold configuration is opt-in per monitor; omitting both threshold keys disables degraded alerting for that monitor.

---

## Out of Scope

- New database tables or schema changes.
- Separate cooldown for degraded vs. down alerts.
- Dashboard UI changes.

---

## Design

### 1. Configuration

Each monitor gains two optional keys in `config.yaml`:

```yaml
monitors:
  ping:
    degraded_alert_minutes: 10   # alert if degraded for 10+ continuous minutes
    degraded_alert_cycles: 5     # alert if degraded for 5+ consecutive polls
    # omit both to disable degraded alerting for this monitor
```

Either threshold triggers the alert independently. Both can be set simultaneously — the alert fires when the first threshold is crossed.

`config.py` `DEFAULT_CONFIG` adds `degraded_alert_minutes: null` and `degraded_alert_cycles: null` to every monitor's defaults so the deep-merge works correctly and omitted keys are falsy.

---

### 2. State management

Two keys per monitor are stored in the existing `state` table:

| Key | Value | Written | Cleared |
|-----|-------|---------|---------|
| `degraded_since:{monitor}` | ISO-8601 UTC timestamp | First degraded poll (only if not already set) | When monitor leaves `degraded` |
| `degraded_cycles:{monitor}` | Integer string | Incremented every degraded poll; initialised to `"1"` on first | Same as above |

No new database functions are needed. The existing `db.get_state` / `db.set_state` handle all reads and writes.

**After a degraded alert fires:** both keys reset (`degraded_since` = current timestamp, `degraded_cycles` = `"1"`). The full threshold must be crossed again after the cooldown expires before another degraded alert fires.

**On restart:** stale keys left from before a restart are preserved intentionally. If the monitor comes back `degraded`, the original onset timestamp is used (the degraded condition pre-dates the restart). If it comes back `ok` or `down`, the keys are cleared immediately.

---

### 3. `run_monitor()` logic (`main.py`)

The monitor config dict for each monitor is stored in a module-level `_monitor_configs: dict[str, dict]` populated during `main()` setup, so `run_monitor()` can look up thresholds without a config parameter change.

The degraded check is inserted into the existing alert block:

```
if new_status == "down" and previous != "down":
    # Clear degraded state — down path handles the alert
    clear degraded_since and degraded_cycles for monitor
    # ... existing send_alert + insert_event (unchanged)

elif new_status == "ok" and previous == "down":
    # ... existing send_recovery + flush_alert_queue + insert_event (unchanged)

elif new_status == "ok" and previous == "degraded":
    # Degraded condition resolved — clear state, no alert, log event
    clear degraded_since and degraded_cycles for monitor
    await db.insert_event(status_change event)

elif new_status == "degraded":
    since = await db.get_state(f"degraded_since:{monitor_name}") or ""
    cycles = int(await db.get_state(f"degraded_cycles:{monitor_name}") or "0") + 1

    if not since:
        since = ts   # first degraded poll for this episode

    await db.set_state(f"degraded_since:{monitor_name}", since)
    await db.set_state(f"degraded_cycles:{monitor_name}", str(cycles))

    threshold_minutes = monitor_cfg.get("degraded_alert_minutes")
    threshold_cycles  = monitor_cfg.get("degraded_alert_cycles")
    elapsed_minutes   = (now - datetime.fromisoformat(since)).total_seconds() / 60
    minutes_hit = threshold_minutes and elapsed_minutes >= threshold_minutes
    cycles_hit  = threshold_cycles  and cycles >= threshold_cycles

    if (minutes_hit or cycles_hit) and not _alerter._in_cooldown(monitor_name):
        duration_str = f"{int(elapsed_minutes)}m ({cycles} polls)"
        desc = "; ".join(r.message for r in results if r.message) or "No detail"
        await _alerter.send_degraded_alert(monitor_name, desc, ts, duration_str)
        # Reset timer so threshold must be crossed again after cooldown expires
        await db.set_state(f"degraded_since:{monitor_name}", ts)
        await db.set_state(f"degraded_cycles:{monitor_name}", "1")
        await db.insert_event(status_change event)
```

The `degraded → ok` branch fires even when `previous == "degraded"` but `new_status == "ok"` in cases where the monitor recovered without ever triggering an alert (threshold never crossed). The event is always logged; the alert is only sent when the threshold was crossed.

---

### 4. `EmailAlerter.send_degraded_alert()` (`alerts/email_alert.py`)

New public method added after `send_recovery`:

```python
async def send_degraded_alert(
    self,
    monitor: str,
    description: str,
    created_at: str,
    duration_str: str,
) -> None:
    """Emit a 'monitor is DEGRADED' email, respecting the cooldown window."""
    if not self._enabled():
        return
    if self._in_cooldown(monitor):
        return

    subject = f"[Schminternet] {monitor.upper()} is DEGRADED"
    body = (
        f"Monitor:          {monitor}\n"
        f"Status:           degraded\n"
        f"Details:          {description}\n"
        f"Degraded for:     {duration_str}\n"
        f"Event time (UTC): {created_at}\n"
    )
    sent = await self._send(subject, body)
    if sent:
        self._cooldowns[monitor] = datetime.now(timezone.utc)
    else:
        await db.enqueue_alert(created_at, subject, body)
```

Follows the exact pattern of `send_alert`: cooldown check, render, send, set cooldown on success, enqueue on failure.

---

### 5. Files changed

| File | Change |
|------|--------|
| `alerts/email_alert.py` | Add `send_degraded_alert()` |
| `main.py` | Add `_monitor_configs` module-level dict; add degraded tracking logic in `run_monitor()`; add `degraded → ok` event logging |
| `config.py` | Add `degraded_alert_minutes: null` and `degraded_alert_cycles: null` to each monitor's defaults |
| `config.example.yaml` | Document the new config keys with example values |

---

### 6. Testing

**`tests/test_email_alert.py`** — new tests:
- `test_send_degraded_alert_sends_email` — enabled alerter, no cooldown → email sent, cooldown set
- `test_degraded_alert_respects_cooldown` — cooldown active → no send
- `test_degraded_alert_enqueues_on_failure` — SMTP fails → row in `alert_queue`

**`tests/test_degraded_alerting.py`** (new file) — `run_monitor()` integration tests using a real isolated DB:
- Minutes threshold not yet crossed → no alert, state written
- Minutes threshold crossed → alert fires, state resets
- Cycles threshold crossed → alert fires
- Both thresholds set: either crossing fires the alert
- `degraded → down`: degraded state cleared, no degraded alert, down path takes over
- `degraded → ok`: state cleared, no alert, `status_change` event logged
- Alert fires, timer resets: second immediate call (in cooldown) → no second alert
- State persists across calls: simulated multi-poll degraded episode
