# Degraded-Condition Alerting — Implementation Plan

**Spec:** `docs/superpowers/specs/2026-03-15-degraded-alerting-design.md`
**Date:** 2026-09-02

Three tasks, executed in order. Task 3 depends on the method added in Task 1
and the config defaults added in Task 2.

---

## Global Constraints

Binding requirements from the spec. These hold for every task.

- **No schema changes.** No new tables, no new functions in `storage/db.py`.
  Degraded state uses the existing `state` table via `db.get_state` /
  `db.set_state`.
- **State keys are exactly** `degraded_since:{monitor}` (ISO-8601 UTC string)
  and `degraded_cycles:{monitor}` (integer as a string). Cleared by writing
  the empty string `""`, not by deleting the row.
  > Superseded: a third key, `degraded_onset:{monitor}`, was added — see the
  > corrected key table in the spec's "episode-onset key fix" amendment
  > (`docs/superpowers/specs/2026-03-15-degraded-alerting-design.md`).
- **Config keys are exactly** `degraded_alert_minutes` and
  `degraded_alert_cycles`, nested under `monitors.<name>` (siblings of
  `interval_seconds`, not inside `thresholds`). Both default to `None` in
  `DEFAULT_CONFIG` for all five monitors.
- **Opt-in:** when both threshold keys are falsy for a monitor, degraded state
  is still tracked and transition events are still logged, but no degraded
  alert is ever sent.
- **Either threshold fires independently.** With both set, whichever is crossed
  first triggers the alert.
- **Shared cooldown.** Degraded alerts use the same per-monitor `_cooldowns`
  dict and `cooldown_minutes` window as down alerts. There is no separate
  degraded cooldown. A down-alert cooldown suppressing an early degraded alert
  is accepted behaviour.
- **Cooldown is checked inside `EmailAlerter`, never at the call site** —
  consistent with `send_alert`.
- **Failed sends are queued,** not dropped: `db.enqueue_alert(created_at,
  subject, body)` on failure, exactly as `send_alert` does. A successful send
  sets the cooldown; a failed send must not.
- **After a degraded alert fires,** reset `degraded_since` to the current event
  timestamp and `degraded_cycles` to `"1"`, so the full threshold must be
  crossed again.
- **Stale state survives restart on purpose.** Keys are cleared when the
  monitor leaves `degraded`, not at startup. A monitor that comes back
  `degraded` after a restart keeps its original onset timestamp and cycle
  count.
- **No dashboard/UI changes.**
- Follow existing repo conventions: `uv run pytest`, `asyncio_mode = auto`
  (no `@pytest.mark.asyncio` decorators), ISO-8601 UTC timestamp strings, one
  logical command per shell call, no `&&` chaining.

---

## Task 1: `EmailAlerter.send_degraded_alert()`

**Files:** `alerts/email_alert.py`, `tests/test_email_alert.py`

Add one new public method to `EmailAlerter`, placed after `send_recovery`.
Use TDD: write the four tests below first, watch them fail, then implement.

The spec gives the implementation verbatim — follow it exactly, including the
body's field labels and column alignment:

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

**Tests** (add to `tests/test_email_alert.py`, reusing the existing `_cfg()`,
`_mock_smtp_success()` and `isolated_db` helpers in that file):

1. `test_send_degraded_alert_sends_email` — enabled alerter, no cooldown:
   asserts SMTP was used, the cooldown is now set for that monitor, and the
   subject line is `[Schminternet] PING is DEGRADED`.
2. `test_degraded_alert_respects_cooldown` — pre-set
   `alerter._cooldowns["ping"]` to now, then call: asserts no SMTP call and no
   row added to `alert_queue`.
3. `test_degraded_alert_enqueues_on_failure` — patch `smtplib.SMTP` to raise
   `ConnectionRefusedError`: asserts exactly one row in `alert_queue` (read it
   back with `db.get_pending_alerts()`) whose subject is the degraded subject,
   and that no cooldown was set.
4. `test_send_degraded_alert_disabled` — `_cfg(enabled=False)`: asserts no SMTP
   call and no queued row.

**Acceptance:** `uv run pytest tests/test_email_alert.py` passes with pristine
output; the full suite still passes.

---

## Task 2: Config defaults and documentation

**Files:** `config.py`, `config.example.yaml`, `tests/test_config.py`

Add `"degraded_alert_minutes": None` and `"degraded_alert_cycles": None` to
each of the five monitor sections in `DEFAULT_CONFIG` (`ping`, `dns`,
`speedtest`, `http`, `ip`). They are siblings of `interval_seconds` — do not
nest them under `thresholds`. No other change to `config.py`; the existing
`_deep_merge` already handles overrides.

Document the keys in `config.example.yaml` under each monitor, commented out so
the shipped example keeps degraded alerting off by default, with a short
comment explaining that omitting both disables degraded alerting for that
monitor and that either threshold fires independently. Use the spec's example
values as the illustrative ones (`ping`: 10 minutes / 5 cycles, `dns`: 5
minutes, `speedtest`: 3 cycles).

**Tests** (add to `tests/test_config.py`, matching its existing style):

1. Every monitor in `DEFAULT_CONFIG["monitors"]` has both keys, defaulting to
   `None`.
2. A user config setting `monitors.ping.degraded_alert_minutes: 10` deep-merges
   over the default without disturbing `degraded_alert_cycles` (still `None`)
   or the sibling `thresholds` dict.

**Acceptance:** `uv run pytest tests/test_config.py` passes; the full suite
still passes.

---

## Task 3: Degraded handling in `run_monitor()`

**Files:** `main.py`, `tests/test_degraded_alerting.py` (new)

This is the integration task. The task brief reproduces the spec's
"4. `run_monitor()` logic" section — follow its guard structure exactly.

**`_monitor_configs`:** add the module-level dict beside `_monitor_status`, and
populate it in `main()` immediately after `config = load_config()`. `main()`
must rebind the module-level name, not a local: extend the existing
`global _led, _alerter` statement to include `_monitor_configs` (the spec's
snippet omits this — it is required for `run_monitor()` to see the value).

**Transition guard** (`if _alerter and previous != new_status:`) gains branches
so that:

- `new_status == "down"`: if `previous == "degraded"`, log a `status_change`
  event for the end of the degraded episode; clear both degraded state keys;
  then the existing down path (`send_alert` + its `insert_event`) runs
  unchanged.
- `new_status == "ok" and previous == "down"`: unchanged (recovery email, queue
  flush, event).
- `new_status == "ok" and previous == "degraded"`: clear both state keys, log a
  `status_change` event, send no email.
- `new_status == "degraded" and previous != "degraded"`: log the onset
  `status_change` event. (Reached from `ok`, `unknown`, and `down`.)

**Degraded accumulation block** — a separate `if _alerter and new_status ==
"degraded":` block placed *after* the transition guard, so it runs on every
degraded poll including `degraded → degraded`. It reads the two state keys,
increments the cycle count, seeds `degraded_since` from `results[0].timestamp`
when unset, writes both keys back, and — only when at least one threshold is
configured — compares elapsed minutes and cycles against the thresholds. On a
crossing it builds `duration_str` as `f"{int(elapsed_minutes)}m ({cycles} polls)"`,
joins the non-empty result messages as the description (falling back to
`"No detail"`, matching the down path), calls `_alerter.send_degraded_alert(...)`,
then resets `degraded_since` to the event timestamp and `degraded_cycles` to
`"1"`.

**Tests:** new file `tests/test_degraded_alerting.py`, driving the real
`main.run_monitor` against an isolated database (`db.configure(tmp_path / ...)`
then `db.init_db()`, as `tests/test_startup.py` does) with `main._alerter`
patched to a mock exposing `AsyncMock` methods, `main._led` left `None`, and
`main._monitor_configs` / `main._monitor_status` set per test. Assert on real
rows in `state` and `events` and on the mock's calls. Cover:

1. Minutes threshold not yet crossed → no alert; `degraded_since` and
   `degraded_cycles` written correctly.
2. Minutes threshold crossed (seed `degraded_since` far enough in the past) →
   alert fires; timer resets to the event timestamp and cycles to `"1"`.
3. Cycles threshold crossed → alert fires.
4. Both thresholds set, minutes crossed first → alert fires.
5. Both thresholds set, cycles crossed first → alert fires.
6. Neither threshold set (both `None`) → no alert ever; state still tracked
   across repeated degraded polls.
7. `degraded → down` → both state keys cleared, a `status_change` event logged
   for the degraded episode end, no degraded alert, and the existing down alert
   still sent.
8. `down → degraded` → onset event logged and accumulation starts fresh
   (`degraded_cycles == "1"`).
9. `degraded → ok` → state cleared, `status_change` event logged, no email.
10. Onset event logged on the first degraded poll, before any threshold is
    crossed.
11. After an alert fires, an immediate second degraded poll does not fire a
    second alert (the reset timer means the threshold is no longer met).
12. Restart with stale keys: pre-populate `degraded_since` with an old
    timestamp and `degraded_cycles`, run a degraded poll with
    `previous == "unknown"`, and assert the stale onset timestamp is used
    rather than reset to now.

**Acceptance:** `uv run pytest` — full suite green, output pristine.
