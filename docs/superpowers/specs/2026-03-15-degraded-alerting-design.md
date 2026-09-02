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

  dns:
    degraded_alert_minutes: 5

  speedtest:
    degraded_alert_cycles: 3
```

Either threshold triggers the alert independently. Both can be set simultaneously — the alert fires when the first is crossed.

`config.py` `DEFAULT_CONFIG` adds `degraded_alert_minutes: null` and `degraded_alert_cycles: null` to every monitor's defaults so the deep-merge works correctly and omitted keys are falsy. When both are `null` or absent, the degraded accumulation block still runs (to track state and log events) but no alert is ever sent.

---

### 2. State management

Two keys per monitor are stored in the existing `state` table:

| Key | Value | Written | Cleared |
|-----|-------|---------|---------|
| `degraded_since:{monitor}` | ISO-8601 UTC timestamp | First degraded poll (only if not already set) | When monitor leaves `degraded` |
| `degraded_cycles:{monitor}` | Integer string | Incremented every degraded poll; initialised to `"1"` on first | Same as above |

No new database functions are needed. The existing `db.get_state` / `db.set_state` handle all reads and writes.

**After a degraded alert fires:** both keys reset (`degraded_since` = current timestamp, `degraded_cycles` = `"1"`). The full threshold must be crossed again after the cooldown expires before another degraded alert fires.

**On restart:** stale keys left from before a restart are preserved intentionally. If the monitor comes back `degraded`, the original onset timestamp and cycle count are used (the degraded condition pre-dates the restart). If it comes back `ok` or `down`, the keys are cleared immediately.

---

### 3. `_monitor_configs` in `main.py`

A new module-level dict is added:

```python
_monitor_configs: dict[str, dict] = {}
```

It is populated in `main()` immediately after `config = load_config()`, before any job closures are defined:

```python
_monitor_configs = config.get("monitors", {})
```

This gives `run_monitor()` access to per-monitor config (e.g., `_monitor_configs.get("ping", {})`) without changing the function's signature.

---

### 4. `run_monitor()` logic (`main.py`)

#### Guard structure

The existing `if _alerter and previous != new_status:` guard covers only *transitions*. The degraded accumulation block must fire on **every poll where `new_status == "degraded"`** — including polls where the monitor remains degraded (`previous == "degraded"`). It therefore lives **outside and after** the transition guard.

The full updated alert section structure:

```python
# ── Transition-based alerts (existing guard, unchanged) ──────────────────────
if _alerter and previous != new_status:
    ts = results[0].timestamp
    if new_status == "down":
        if previous == "degraded":
            # Log that degraded episode ended (entered down)
            await db.insert_event({
                "timestamp": ts, "event_type": "status_change",
                "monitor": monitor_name,
                "description": f"Status changed: degraded → down",
                "previous_status": "degraded", "new_status": "down",
            })
        # Clear any degraded state
        await db.set_state(f"degraded_since:{monitor_name}", "")
        await db.set_state(f"degraded_cycles:{monitor_name}", "")
        # Existing down-alert path (send_alert + insert_event) unchanged
        ...

    elif new_status == "ok" and previous == "down":
        # Existing recovery path (send_recovery + flush + insert_event) unchanged
        ...

    elif new_status == "ok" and previous == "degraded":
        # Degraded resolved — clear state, log event, no alert
        await db.set_state(f"degraded_since:{monitor_name}", "")
        await db.set_state(f"degraded_cycles:{monitor_name}", "")
        await db.insert_event({
            "timestamp": ts, "event_type": "status_change",
            "monitor": monitor_name,
            "description": f"Status recovered: degraded → ok",
            "previous_status": "degraded", "new_status": "ok",
        })

    elif new_status == "degraded" and previous != "degraded":
        # Onset: log the transition event
        await db.insert_event({
            "timestamp": ts, "event_type": "status_change",
            "monitor": monitor_name,
            "description": f"Status changed: {previous} → degraded",
            "previous_status": previous, "new_status": "degraded",
        })

# ── Degraded accumulation (runs every poll where new_status == "degraded") ───
if _alerter and new_status == "degraded":
    ts = results[0].timestamp
    now = datetime.now(timezone.utc)
    monitor_cfg = _monitor_configs.get(monitor_name, {})

    since = await db.get_state(f"degraded_since:{monitor_name}") or ""
    cycles = int(await db.get_state(f"degraded_cycles:{monitor_name}") or "0") + 1

    if not since:
        since = ts   # first degraded poll for this episode

    await db.set_state(f"degraded_since:{monitor_name}", since)
    await db.set_state(f"degraded_cycles:{monitor_name}", str(cycles))

    threshold_minutes = monitor_cfg.get("degraded_alert_minutes")
    threshold_cycles  = monitor_cfg.get("degraded_alert_cycles")

    if threshold_minutes or threshold_cycles:
        elapsed_minutes = (now - datetime.fromisoformat(since)).total_seconds() / 60
        minutes_hit = threshold_minutes and elapsed_minutes >= float(threshold_minutes)
        cycles_hit  = threshold_cycles  and cycles >= int(threshold_cycles)

        if minutes_hit or cycles_hit:
            duration_str = f"{int(elapsed_minutes)}m ({cycles} polls)"
            desc = "; ".join(r.message for r in results if r.message) or "No detail"
            await _alerter.send_degraded_alert(monitor_name, desc, ts, duration_str)
            # Reset timer so full threshold must be crossed again after cooldown
            await db.set_state(f"degraded_since:{monitor_name}", ts)
            await db.set_state(f"degraded_cycles:{monitor_name}", "1")
```

#### `down → degraded` transition

When `previous == "down"` and `new_status == "degraded"`, the monitor has partially recovered. No recovery email is sent (the monitor is not yet `ok`). Because `"down" != "degraded"`, the `elif new_status == "degraded" and previous != "degraded"` branch fires and logs a `status_change` onset event. The degraded accumulation block then starts fresh. The existing down-alert cooldown remains active and will suppress the first degraded alert for up to `cooldown_minutes` — this is accepted behaviour (shared cooldown, user's explicit choice).

---

### 5. `EmailAlerter.send_degraded_alert()` (`alerts/email_alert.py`)

New public method added after `send_recovery`. The cooldown is checked **only inside this method** (consistent with `send_alert` — the call site in `run_monitor()` does not pre-check cooldown):

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

---

### 6. Files changed

| File | Change |
|------|--------|
| `alerts/email_alert.py` | Add `send_degraded_alert()` |
| `main.py` | Add `_monitor_configs` module-level dict; restructure alert block to add degraded accumulation section outside transition guard; add all degraded transition event logging |
| `config.py` | Add `degraded_alert_minutes: null` and `degraded_alert_cycles: null` to each monitor's defaults |
| `config.example.yaml` | Document the new config keys with example values |

---

### 7. Testing

**`tests/test_email_alert.py`** — new tests:
- `test_send_degraded_alert_sends_email` — enabled alerter, no cooldown → email sent, cooldown set
- `test_degraded_alert_respects_cooldown` — cooldown active → no send
- `test_degraded_alert_enqueues_on_failure` — SMTP fails → row in `alert_queue`
- `test_send_degraded_alert_disabled` — alerter disabled → no send, no enqueue

**`tests/test_degraded_alerting.py`** (new file) — `run_monitor()` integration tests using a real isolated DB and a mock `_alerter`:
- Minutes threshold not yet crossed → no alert, state written correctly
- Minutes threshold crossed → alert fires, timer resets to now
- Cycles threshold crossed → alert fires
- Both thresholds set: minutes crossed first → alert fires
- Both thresholds set: cycles crossed first → alert fires
- Neither threshold set (both null) → no alert ever fires, state still tracked
- `degraded → down`: degraded state cleared, `status_change` event logged for degraded episode end, no degraded alert, down path unaffected
- `down → degraded`: accumulation starts fresh; cooldown from prior down alert suppresses degraded alert
- `degraded → ok`: state cleared, `status_change` event logged, no alert
- Onset event logged on first degraded poll (before threshold crossed)
- Alert fires, timer resets: next immediate call (in cooldown) → no second alert
- Restart with stale keys: pre-populate `degraded_since` and `degraded_cycles` in DB, verify stale timestamp is used (not reset to current time)
- Disabled path smoke test: both thresholds null, monitor repeatedly degraded → no alert, no error

---

## Amendment — 2026-09-02: conditional reset + queue de-dup

Post-review, two consequences of the original **unconditional** reset in §4 and §5 were found and fixed. This section documents the deviation so the spec above (§4's code block and §5's `send_degraded_alert` signature) no longer matches the code; the code is authoritative.

**Problem observed:**
- (a) `send_degraded_alert` returns silently when cooldown-suppressed, but the call site reset the episode timer regardless. A down alert followed by a drop to degraded meant the next threshold crossing was suppressed by the shared cooldown *and* the clock restarted anyway, roughly doubling the wait for the alert the user configured.
- (b) A failed send correctly does not set the cooldown (queuing must keep working through an outage), but with no de-dup a multi-hour degraded episode at a short threshold queued dozens of near-identical emails, all delivered in a burst once SMTP recovered.

**Fix:**
- `send_degraded_alert()` now returns `bool` instead of `None`: `True` when an alert for the episode is now in flight (sent, queued, or an identical one already pending), `False` when nothing happened (disabled, or cooldown-suppressed).
- Before enqueueing a failed send, `send_degraded_alert()` reads `db.get_pending_alerts()` and skips the enqueue if a pending row already has this exact subject — logged at debug level — and still returns `True` (the pending row already covers this episode).
- The call site in `run_monitor()` resets `degraded_since` / `degraded_cycles` only when `send_degraded_alert()` returns truthy. When it returns `False`, the episode keeps accumulating and the alert goes out on the next poll after the cooldown expires.

Everything else in §1–§4 is unchanged: thresholds are still truthiness-checked, the cooldown is still shared with `down` alerts and still checked only inside `EmailAlerter`, a failed send still does not set the cooldown, and `degraded → down` still logs both of its events.
