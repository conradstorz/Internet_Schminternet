# Plan: Internet Schminternet — Pi Internet Health Monitor

**Repo:** `git@github.com:conradstorz/Internet_Schminternet.git`

## TL;DR
A Python async service on Raspberry Pi 3B that monitors five facets of internet health
(ping, DNS, speedtest, HTTP reachability, external IP), stores time-series data in SQLite,
drives a WS2812B LED status bar with color-coded status, serves a FastAPI web dashboard,
and sends SMTP email alerts on state changes.

---

## Stack

| Layer | Choice | Why |
|---|---|---|
| Ping/loss | `subprocess` + system `ping` | No root dependency, kernel-accurate |
| DNS timing | `dnspython` | Test specific servers independently, asyncio support |
| Speedtest | `speedtest-cli` in thread executor | Standard; ≤30 min intervals on Pi 3B |
| HTTP checks | `httpx` async client | Unified sync/async API |
| External IP | `api.ipify.org` via httpx | Simple, reliable |
| Storage | SQLite + `aiosqlite` | Zero extra RAM, no services, sufficient for time-series |
| Dashboard | FastAPI + HTMX + Chart.js + Jinja2 | Async-native, ~50 MB idle on Pi |
| LEDs | `rpi_ws281x` (WS2812B, GPIO18) | DMA-accurate, Pi 3B proven |
| Scheduler | APScheduler 3.x AsyncIOScheduler | Mixed sync/async jobs, misfire/coalescing built in |
| Alerting | Python `smtplib` (stdlib) | Zero extra dependency |
| **Estimated idle RAM** | **~80–120 MB** | Well within Pi 3B 1 GB |

---

## Project Structure

```
internet_schminternet/
├── config.yaml                  # Runtime config (gitignored — contains SMTP secrets)
├── config.example.yaml          # Committed template with all keys documented
├── config.py                    # Config loader: deep-merges user config onto defaults
├── main.py                      # Entry point: init + asyncio event loop
├── monitors/
│   ├── __init__.py
│   ├── base.py                  # MonitorResult dataclass, Status literal, shared types
│   ├── ping.py                  # Latency + packet loss (subprocess ping)
│   ├── dns.py                   # DNS resolution timing (dnspython async resolver)
│   ├── speedtest.py             # Bandwidth test (speedtest-cli, run in thread executor)
│   ├── http_check.py            # HTTP reachability (httpx async)
│   └── ip_tracker.py            # External IP change detection (ipify + httpx)
├── storage/
│   ├── __init__.py
│   ├── db.py                    # Async DB init, insert, query, retention cleanup
│   └── schema.sql               # DDL reference
├── leds/
│   ├── __init__.py
│   └── controller.py            # WS2812B segment/color abstraction; silent fallback
├── web/
│   ├── __init__.py
│   ├── app.py                   # FastAPI routes + SSE stream endpoint
│   ├── templates/
│   │   ├── index.html           # Dashboard: status cards, Chart.js charts, events log
│   │   └── partials/
│   │       └── events.html      # HTMX-swapped events table fragment
│   └── static/
│       └── main.js              # Chart.js init + status card JS + SSE handler
├── alerts/
│   ├── __init__.py
│   └── email_alert.py           # SMTP alerter with cooldown + recovery emails
├── tests/
│   ├── __init__.py
│   ├── test_config.py
│   ├── test_db.py
│   └── test_thresholds.py
├── requirements.txt
├── .gitignore
├── README.md
└── systemd/
    └── internet-schminternet.service
```

---

## Implementation Phases

### Phase 1 — Core Infrastructure
*Steps 1–3 are independent; step 4 depends on them.*

1. **Scaffold** — folders, `__init__.py` files; `.gitignore` created first so secrets are never committed.
2. **Config** — `config.example.yaml` with all keys; `config.py` loads `config.yaml` via PyYAML
   and deep-merges it onto a `DEFAULT_CONFIG` dict.
3. **Database layer** (`storage/db.py`):
   - `metrics(id, timestamp, monitor, target, metric, value, status)` — indexed on `(timestamp)` and `(monitor, timestamp)`
   - `events(id, timestamp, event_type, monitor, description, previous_status, new_status)`
   - `state(key PRIMARY KEY, value)` — one-row store for external IP etc.
   - Async: `init_db`, `insert_metrics`, `query_recent`, `get_current_status`, `insert_event`, `get_events`,
     `get_state`, `set_state`, `cleanup_old`
4. **`monitors/base.py`** — `MonitorResult` dataclass and `Status = Literal["ok","degraded","down","unknown"]`.

### Phase 2 — Monitor Implementations
*All five are independent of each other.*

5. **`monitors/ping.py`** — `subprocess.run` ping (cross-platform: Linux `-c`/`-W`, Windows `-n`);
   parse avg RTT and loss %; `_determine_status(latency_ms, loss_pct, thresholds)` helper;
   default interval 30 s.
6. **`monitors/dns.py`** — `dns.asyncresolver.Resolver` per configured nameserver; measure wall time;
   emit `resolution_ms`; default interval 60 s.
7. **`monitors/speedtest.py`** — `asyncio.run_in_executor` wrapping `speedtest.Speedtest(secure=True)`;
   emit `download_mbps`, `upload_mbps`, `ping_ms`; default interval 1800 s.
8. **`monitors/http_check.py`** — `httpx.AsyncClient` parallel checks; emit `response_ms` + `status_code`;
   default interval 120 s.
9. **`monitors/ip_tracker.py`** — ipify primary / icanhazip fallback; compare to `state` table;
   emit change event; default interval 300 s.

### Phase 3 — LED Controller
*Depends on Phase 1 (config); independent of Phase 2.*

10. **`leds/controller.py`** — wrap `rpi_ws281x.PixelStrip`; `update_segment(monitor, status)` +
    `set_all(color)` + `blackout()`; LED writes via `run_in_executor`; silent fallback if
    hardware absent; color map: ok=green, degraded=amber, down=red, measuring=blue, unknown=dim white.

### Phase 4 — Web Dashboard
*Depends on Phase 1.*

11. **`web/app.py`** — FastAPI: `GET /`, `GET /api/status`, `GET /api/metrics/{monitor}?hours=24`,
    `GET /api/events?limit=50`, `GET /partials/events`, `GET /stream` (SSE).
12. **`web/templates/index.html`** — dark theme dashboard; status cards (JS-updated via SSE + poll);
    Chart.js line charts (latency, DNS, download speed); events log (HTMX polling `/partials/events`).
13. **`web/static/main.js`** — `fetchStatus()` every 10 s; Chart.js with Luxon time adapter;
    SSE listener with auto-reconnect.

### Phase 5 — Alerting
*Depends on Phase 1; independent of Phases 2–4.*

14. **`alerts/email_alert.py`** — `smtplib` + STARTTLS; alert on `→down`, recovery on `→ok`;
    per-monitor in-memory cooldown timer.

### Phase 6 — Integration
*Depends on all phases above.*

15. **`main.py`** — config load → `init_db` → LED init → `AsyncIOScheduler` with closure-wrapped jobs
    for all monitors + nightly cleanup; shared `_monitor_status` dict drives LEDs + SSE broadcasts;
    FastAPI + uvicorn co-hosted in same asyncio loop via `uvicorn.Server.serve()` task;
    SIGINT/SIGTERM → graceful shutdown (scheduler stop, LED blackout, uvicorn stop).

### Phase 7 — Deployment
*Independent; `.gitignore` created first.*

16. **`.gitignore`** — `config.yaml`, `*.db`, `__pycache__/`, `.env`, `*.pyc` *(first file created)*.
17. **`systemd/internet-schminternet.service`** — `User=root`, `Restart=on-failure`.
18. **`requirements.txt`** — `fastapi`, `uvicorn[standard]`, `aiosqlite`, `dnspython`, `httpx`,
    `speedtest-cli`, `APScheduler<4`, `PyYAML`, `jinja2`, `pytest`, `pytest-asyncio`;
    `rpi-ws281x` noted separately (Pi-only).
19. **`README.md`** — install, GPIO18 wiring, audio PWM conflict note, systemd setup.
20. **Git bootstrap** — `git init`, add remote, initial commit, `git push -u origin main`.

---

## Configuration Schema (`config.example.yaml` key sections)

```yaml
monitors:
  ping:
    targets: ["8.8.8.8", "1.1.1.1"]
    interval_seconds: 30
    count: 5
    thresholds: { degraded_ms: 100, down_ms: 500, loss_degraded_pct: 10, loss_down_pct: 50 }
  dns:
    servers: ["8.8.8.8", "1.1.1.1"]
    test_domain: "google.com"
    interval_seconds: 60
    thresholds: { degraded_ms: 200, down_ms: 1000 }
  speedtest:
    interval_seconds: 1800
    expected_download_mbps: 50
    expected_upload_mbps: 10
    thresholds: { degraded_ratio: 0.5 }
  http:
    targets: [{ url: "https://www.google.com" }, { url: "https://www.cloudflare.com" }]
    interval_seconds: 120
    timeout_seconds: 10
    thresholds: { degraded_ms: 1000 }
  ip:
    interval_seconds: 300
database: { path: "data/schminternet.db", retention_days: 30 }
web: { host: "0.0.0.0", port: 8080 }
leds:
  enabled: true
  pin: 18
  count: 16
  brightness: 0.4
  segments:
    ping:      [0, 3]
    dns:       [4, 6]
    http:      [7, 9]
    speedtest: [10, 12]
    ip:        [13, 13]
    overall:   [14, 15]
alerts:
  email:
    enabled: true
    smtp_host: "smtp.gmail.com"
    smtp_port: 587
    use_tls: true
    username: "you@gmail.com"
    password: "app-password-here"   # use an app password, NOT your account password
    from_addr: "you@gmail.com"
    to_addrs: ["you@gmail.com"]
    cooldown_minutes: 15
```

---

## LED Color Mapping

| Status | Color | RGB |
|---|---|---|
| `ok` | Green | `(0, 150, 0)` |
| `degraded` | Amber | `(200, 100, 0)` |
| `down` | Red | `(180, 0, 0)` |
| `measuring` | Blue | `(0, 0, 180)` |
| `unknown` | Dim white | `(20, 20, 20)` |

---

## Verification

1. `python -m pytest tests/` — unit tests for config, DB, threshold logic
2. `python main.py` on dev machine (LED fallback active) — all monitors run, DB populates,
   dashboard loads at `http://localhost:8080`
3. Block port 53 → LED turns red, alert email fires; unblock → recovery email + green LED
4. On Pi: `sudo systemctl start internet-schminternet` → survives reboot, LEDs glow on GPIO18

---

## Decisions & Scope

- **In scope:** Five monitors, SQLite, FastAPI dashboard, WS2812B LEDs, SMTP alerts, systemd, configurable thresholds.
- **Out of scope (v1):** Grafana/Prometheus export, push notifications, dashboard auth, automated threshold tuning.
- **Root:** Service runs as `root` — required for `rpi_ws281x` DMA. Disable onboard audio if GPIO18 conflict (`dtparam=audio=off` in `/boot/config.txt`).
- **LED fallback:** Init failure logs warning and no-ops silently — safe for dev machines (Windows etc.).
- **Secrets:** `config.yaml` in `.gitignore`; only `config.example.yaml` committed.
- **Speedtest:** async wrapper calls `run_in_executor` internally; registered as normal asyncio scheduler job.
