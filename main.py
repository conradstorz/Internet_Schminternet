"""Internet Schminternet — entry point.

Starts:
  • APScheduler AsyncIOScheduler with all five monitor jobs
  • uvicorn serving the FastAPI dashboard — co-hosted in the same event loop
  • LED controller (no-op on non-Pi hardware)
  • SMTP email alerter

Shutdown is graceful: SIGINT / SIGTERM → scheduler stops, LEDs black out, uvicorn exits.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone
from typing import Optional

import uvicorn
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import storage.db as db
from alerts.email_alert import EmailAlerter
from config import load_config
from leds.controller import LEDController
from monitors import dns as dns_monitor
from monitors import http_check, ip_tracker, ping
from monitors import speedtest as speedtest_monitor
from monitors.base import MonitorResult
from web.app import app, broadcast_status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global shared state
# ---------------------------------------------------------------------------

_monitor_status: dict[str, str] = {}
_led: Optional[LEDController] = None
_alerter: Optional[EmailAlerter] = None


# ---------------------------------------------------------------------------
# Monitor runner — called by every scheduler job
# ---------------------------------------------------------------------------

async def run_monitor(monitor_name: str, results: list[MonitorResult]) -> None:
    """Persist results, update LEDs, fire alerts, and broadcast via SSE."""
    if not results:
        return

    await db.insert_metrics(results)

    # Derive overall status for this monitor (worst wins)
    status_priority = {"down": 3, "degraded": 2, "ok": 1, "unknown": 0}
    new_status = max(
        (r.status for r in results),
        key=lambda s: status_priority.get(s, 0),
        default="unknown",
    )

    previous = _monitor_status.get(monitor_name, "unknown")
    _monitor_status[monitor_name] = new_status

    # LED update
    if _led:
        await _led.update_segment(monitor_name, new_status)

    # Alert on state change
    if _alerter and previous != new_status:
        ts = results[0].timestamp
        if new_status == "down" and previous != "down":
            desc = "; ".join(r.message for r in results if r.message) or "No detail"
            _alerter.send_alert(monitor_name, desc, new_status, previous)
            await db.insert_event(
                {
                    "timestamp": ts,
                    "event_type": "status_change",
                    "monitor": monitor_name,
                    "description": f"Status changed {previous} → {new_status}. {desc}",
                    "previous_status": previous,
                    "new_status": new_status,
                }
            )
        elif new_status == "ok" and previous == "down":
            _alerter.send_recovery(monitor_name, "Connection restored")
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

    # SSE broadcast
    broadcast_status(
        {
            "monitor": monitor_name,
            "status": new_status,
            "results": [
                {"metric": r.metric, "value": r.value, "target": r.target, "message": r.message}
                for r in results
            ],
        }
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    global _led, _alerter

    config = load_config()

    # DB
    db_path = config.get("database", {}).get("path", "data/schminternet.db")
    db.configure(db_path)
    await db.init_db()
    logger.info("Database ready at %s", db_path)

    # LEDs
    _led = LEDController(config.get("leds", {}))

    # Alerter
    _alerter = EmailAlerter(config.get("alerts", {}).get("email", {}))

    # Pull monitor config sections
    ping_cfg     = config["monitors"]["ping"]
    dns_cfg      = config["monitors"]["dns"]
    speed_cfg    = config["monitors"]["speedtest"]
    http_cfg     = config["monitors"]["http"]
    ip_cfg       = config["monitors"]["ip"]
    retention    = config["database"]["retention_days"]

    # ---------------------------------------------------------------------------
    # Scheduler — closure-wrapped jobs so each async job has access to config
    # but doesn't need to pass it as an APScheduler job argument.
    # ---------------------------------------------------------------------------

    executors = {
        "default":    "asyncio",
        "threadpool": ThreadPoolExecutor(max_workers=2),
    }
    scheduler = AsyncIOScheduler(executors=executors)

    async def job_ping() -> None:
        await run_monitor("ping", await ping.run(config))

    async def job_dns() -> None:
        await run_monitor("dns", await dns_monitor.run(config))

    async def job_speedtest() -> None:
        await run_monitor("speedtest", await speedtest_monitor.run(config))

    async def job_http() -> None:
        await run_monitor("http", await http_check.run(config))

    async def job_ip() -> None:
        await run_monitor("ip", await ip_tracker.run(config))

    async def job_cleanup() -> None:
        await db.cleanup_old(retention)
        logger.info("Retention cleanup complete (kept last %d days)", retention)

    scheduler.add_job(job_ping,      "interval", seconds=ping_cfg.get("interval_seconds", 30),    id="ping",      misfire_grace_time=15)
    scheduler.add_job(job_dns,       "interval", seconds=dns_cfg.get("interval_seconds", 60),     id="dns",       misfire_grace_time=30)
    scheduler.add_job(job_speedtest, "interval", seconds=speed_cfg.get("interval_seconds", 1800), id="speedtest", misfire_grace_time=60)
    scheduler.add_job(job_http,      "interval", seconds=http_cfg.get("interval_seconds", 120),   id="http",      misfire_grace_time=30)
    scheduler.add_job(job_ip,        "interval", seconds=ip_cfg.get("interval_seconds", 300),     id="ip",        misfire_grace_time=60)
    scheduler.add_job(job_cleanup,   "cron",     hour=3,                                           id="cleanup")

    scheduler.start()
    logger.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    # Run each monitor once immediately on startup
    for job_fn in (job_ping, job_dns, job_http, job_ip):
        asyncio.create_task(job_fn())

    # ---------------------------------------------------------------------------
    # uvicorn — same event loop
    # ---------------------------------------------------------------------------

    web_cfg   = config.get("web", {})
    uv_config = uvicorn.Config(
        app,
        host=web_cfg.get("host", "0.0.0.0"),
        port=int(web_cfg.get("port", 8080)),
        log_level="warning",
    )
    server = uvicorn.Server(uv_config)
    logger.info(
        "Dashboard: http://%s:%d",
        web_cfg.get("host", "0.0.0.0"),
        web_cfg.get("port", 8080),
    )

    # ---------------------------------------------------------------------------
    # Graceful shutdown
    # ---------------------------------------------------------------------------

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()

    def _handle_signal(*_: object) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    server_task = asyncio.create_task(server.serve())

    await stop_event.wait()

    logger.info("Shutting down…")
    scheduler.shutdown(wait=False)
    server.should_exit = True
    await server_task
    if _led:
        await _led.blackout()
    logger.info("Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
