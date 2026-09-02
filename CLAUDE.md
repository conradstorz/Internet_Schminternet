# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Internet Schminternet** is a Raspberry Pi service that monitors internet health across five dimensions (ping, DNS, HTTP, speedtest, and external-IP tracking) and displays status on a WS2812B LED strip and a local web dashboard, with SMTP alerts on state changes.

## Commands

```bash
uv sync                                  # install dependencies (Python >=3.13)
uv run python main.py                    # run the app; dashboard at http://localhost:8080
uv run pytest tests/                     # full test suite
uv run pytest tests/test_db.py           # one file
uv run pytest tests/test_db.py::test_insert_and_query_recent   # one test
```

Run `main.py` from the repo root — `web/app.py` mounts `web/static` and `web/templates` by relative path.

`rpi-ws281x` is a Pi-only dependency installed separately (`sudo apt install python3-dev swig gcc`, then `uv pip install rpi-ws281x`); the LED controller silently no-ops everywhere else. `requirements.txt` exists for the Pi/pip install path documented in the README; `pyproject.toml` + `uv.lock` are the source of truth for development.

There is no linter or formatter configured.

## Bash Command Style
- Never chain bash commands with `&&` or `;`.
- Always run one logical command per Bash tool call.
- If you need multiple commands, don't ask the user to run them until you have tried to run them yourself as separate Bash calls.

## Architecture

`main.py` is a single asyncio process hosting an APScheduler `AsyncIOScheduler` and a uvicorn server **on the same event loop**. Nothing runs in a second process.

**Per-poll data flow:**
1. A scheduler job calls `monitors/<name>.run(config)` → `list[MonitorResult]` (`monitors/base.py`)
2. `run_monitor()` in `main.py` persists them (`storage/db.py`), collapses them to one status per monitor ("worst wins": `down` > `degraded` > `ok` > `unknown`), updates the LED segment, fires alerts on transition, and pushes an SSE payload via `web.app.broadcast_status`

**Scheduled jobs** (`main.py`): the five monitors on their configured intervals, plus `cleanup` (cron 03:00, retention purge + VACUUM), `heartbeat` (60 s, writes `last_seen_at`), and `alert_flush` (5 min, retries queued email). Ping/DNS/HTTP/IP also fire once immediately at startup — speedtest deliberately does not.

**Conventions that matter:**
- All monitors are `async def run(config: dict) -> list[MonitorResult]`; `value = -1.0` is the error sentinel; timestamps are ISO-8601 UTC strings.
- Blocking work (ping subprocess, `speedtest-cli`, LED `show()`) goes through `loop.run_in_executor`, and `loop` must come from `asyncio.get_running_loop()` inside the coroutine — never captured at construction time.
- Ping and DNS fan out across their targets with `asyncio.gather`, so total poll time is one target's latency, not the sum. `tests/test_monitor_concurrency.py` asserts this with wall-clock timing.
- Each monitor exposes a pure `_determine_status(...)` tested directly in `tests/test_thresholds.py`.
- Config is deep-merged: `config.yaml` (gitignored) overlays `DEFAULT_CONFIG` in `config.py`, so user config only holds overrides.
- LED segments are keyed by monitor name in `leds.segments`; `leds.enabled: false` disables the strip on dev machines.

**Alerting (`alerts/email_alert.py`) — read before touching:**
- Alerts fire on transitions **to and from `down`**, and `degraded` transitions are now logged as `status_change` events. A sustained `degraded` episode sends an email once `degraded_alert_minutes` or `degraded_alert_cycles` (per-monitor, opt-in, both `None` by default — falsy means disabled) is crossed. Degraded alerts share the per-monitor cooldown with `down` alerts and are queued on send failure like any other alert. `send_degraded_alert()` returns `bool` — `True` when an alert for the episode is now in flight (sent, queued, or an identical one is already pending), `False` when nothing happened (disabled or cooldown-suppressed) — and `run_monitor()` resets the episode timer (`degraded_since`/`degraded_cycles`) **only when that return value is truthy**. A cooldown-suppressed call therefore leaves the clock running instead of restarting it, and a long episode with SMTP down is not queued twice while an identical degraded alert for that monitor is still pending — `send_degraded_alert()` checks `db.get_pending_alerts()` for a matching subject before enqueueing. Design spec: `docs/superpowers/specs/2026-03-15-degraded-alerting-design.md`.
- `_send()` returns a bool. Because SMTP is unreachable during exactly the outage being reported, a failed send is **queued** to the `alert_queue` table rather than dropped. The queue is drained on recovery (`down` → `ok`), at startup, and by the 5-minute flush job; failed retries bump `attempt_count`. Preserve this — a "down" notification must still arrive late rather than never.
- Per-monitor cooldown (`cooldown_minutes`) suppresses repeat `down` alerts; a recovery clears it. The cooldown clock only starts on a *successful* send.
- `send_best_effort()` bypasses both cooldown and queueing (used for the startup email).

**Startup/shutdown lifecycle (`_startup_sequence` in `main.py`):**
Two `state` keys classify the previous stop: `shutdown_at` (written on clean SIGINT/SIGTERM) and `last_seen_at` (heartbeat). Present `shutdown_at` → clean; only `last_seen_at` → unclean (power loss/crash); neither → first run. The classification produces a `startup` event row and, except on first run, a restart email quoting approximate downtime and the count of alerts queued during the outage.

**Web API (`web/app.py`):**
- `GET /api/status` — latest row per (monitor, target, metric)
- `GET /api/metrics/{monitor}?hours=24` — time-series for one monitor
- `GET /api/events?limit=50` — status-change/IP-change/startup event log
- `GET /partials/events` — same data as a Jinja fragment
- `GET /stream` — SSE; one JSON object (`monitor`, `status`, `results[]`) per poll cycle, 15 s keepalive comments, per-client bounded queue that drops on overflow

The dashboard loads Chart.js and Luxon from a CDN, so it degrades during the outages this tool exists to observe. Vendor them into `web/static/` if that matters.

**SQLite (`storage/db.py`):** four tables — `metrics` (time-series), `events` (change log), `state` (key-value: `external_ip`, `shutdown_at`, `last_seen_at`, `degraded_since:{monitor}`, `degraded_cycles:{monitor}`), `alert_queue` (undelivered email). The `degraded_since`/`degraded_cycles` keys are cleared by writing `""`, not by deleting the row. `storage/schema.sql` is documentation only; the authoritative DDL is the `_SCHEMA` string in `db.py`, so change both together. Every function opens its own short-lived `aiosqlite` connection; there is no shared pool. Module-level `_DB_PATH` is set by `db.configure(path)` — tests point it at `tmp_path`.

**Tests:** `pytest.ini` sets `asyncio_mode = auto`, so async tests need no decorator. Tests import `main` directly (e.g. `_startup_sequence`), so keep `main.py` import-safe — no side effects outside `main()`.

**Adding a monitor:**
1. `monitors/your_monitor.py` with `async def run(config) -> list[MonitorResult]` and a pure `_determine_status(...)`
2. Register a closure-wrapped job in `main.py`'s scheduler block
3. Add defaults to `DEFAULT_CONFIG` and an LED segment in `config.example.yaml`
4. Add threshold tests to `tests/test_thresholds.py`

## Deployment

```bash
sudo cp systemd/internet-schminternet.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now internet-schminternet
sudo journalctl -u schminternet -f
```

The service runs as root (required for LED DMA access on GPIO18/PWM0, which conflicts with the onboard audio jack — see README).

## Docs

`plan.md` is the original design document. `docs/superpowers/specs/` and `docs/superpowers/plans/` hold per-feature design specs and implementation plans; check them before designing something that may already be specced.
