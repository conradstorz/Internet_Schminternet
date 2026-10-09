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
import logging.handlers
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import uvicorn
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import storage.db as db
from alerts.email_alert import EmailAlerter
from config import load_config
from leds.controller import LEDController
from leds.quality import overall_score, quality_color_hex, score_monitor
from monitors import dns as dns_monitor
from monitors import http_check, ip_tracker, ping
from monitors import speedtest as speedtest_monitor
from monitors import speedtest_policy
from monitors.base import MonitorResult
from web.app import app, broadcast_status, set_quality_snapshot

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"

# Console-only default so early failures (before config is loaded) are still
# visible. _configure_logging() replaces this once config.yaml is read.
_root_handlers_before_bootstrap = list(logging.getLogger().handlers)
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, datefmt=_LOG_DATEFMT)
logger = logging.getLogger(__name__)

# Handlers this module attached to the root logger — tracked so a later call can
# remove exactly these (and only these) rather than stacking duplicates or
# closing handlers somebody else owns. basicConfig() is a no-op when logging is
# already configured (an embedding app, pytest), so adopt only what it added.
_managed_handlers: list[logging.Handler] = [
    h for h in logging.getLogger().handlers if h not in _root_handlers_before_bootstrap
]

_NOISY_LOGGERS = ("httpx", "apscheduler.scheduler", "apscheduler.executors")

# Monitor-name / status-word column widths for the poll-summary log line.
_NAME_WIDTH = 9   # len("speedtest"), the longest monitor name
_STATUS_WIDTH = 8  # len("degraded"), the longest status word


def _int_setting(cfg: dict, key: str, default: int) -> int:
    """Read an int from config, falling back to *default* on an unusable value."""
    raw = cfg.get(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring non-numeric logging.%s=%r; using %d", key, raw, default)
        return default


def _configure_logging(cfg: dict) -> None:
    """Attach a rotating logfile (and, optionally, a console handler) per config.

    Safe to call more than once: handlers previously attached by this function
    are removed first, so repeated calls never stack duplicates. If the log
    file can't be created (permissions, read-only mount), logs a warning and
    carries on — a missing logfile must never stop the service.
    """
    global _managed_handlers

    # `logging:` with no body deep-merges to None, and any scalar there would
    # be a dict-shaped lookup on a non-dict — neither may take the service down.
    log_cfg = cfg.get("logging") or {}
    if not isinstance(log_cfg, dict):
        logger.warning("Ignoring malformed logging config: %r", log_cfg)
        log_cfg = {}

    path = log_cfg.get("path", "data/schminternet.log")
    level = getattr(logging, str(log_cfg.get("level", "INFO")).upper(), logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    max_bytes = _int_setting(log_cfg, "max_bytes", 10_000_000)
    backup_count = _int_setting(log_cfg, "backup_count", 5)
    console = log_cfg.get("console", True)

    root = logging.getLogger()
    root.setLevel(level)

    for handler in _managed_handlers:
        root.removeHandler(handler)
        handler.close()
    _managed_handlers = []

    formatter = logging.Formatter(fmt=_LOG_FORMAT, datefmt=_LOG_DATEFMT)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)
        _managed_handlers.append(console_handler)

    try:
        log_path = Path(path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            str(log_path), maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        _managed_handlers.append(file_handler)
    except (OSError, TypeError, ValueError) as exc:
        if not any(not isinstance(h, logging.NullHandler) for h in root.handlers):
            # console: false plus an unusable log file would leave the service
            # with nowhere to log at all — including this warning. A NullHandler
            # occupies the list without emitting, so it does not count.
            fallback = logging.StreamHandler()
            fallback.setFormatter(formatter)
            root.addHandler(fallback)
            _managed_handlers.append(fallback)
        logger.warning("Could not attach log file at %s: %s", path, exc)

    for noisy_name in _NOISY_LOGGERS:
        logging.getLogger(noisy_name).setLevel(logging.WARNING)


def _format_metric(r: MonitorResult) -> str:
    """Render one MonitorResult's metric compactly, e.g. "30.8ms" or "down 412.3 Mbps"."""
    if r.value == -1.0:
        rendered = "error"
        if r.message:
            rendered += f" ({r.message})"
        return rendered
    if r.metric in ("latency_ms", "resolution_ms", "response_ms", "ping_ms"):
        return f"{r.value:.1f}ms"
    if r.metric == "packet_loss_pct":
        return f"{int(r.value)}%" if r.value == int(r.value) else f"{r.value:.1f}%"
    if r.metric == "download_mbps":
        return f"down {r.value:.1f} Mbps"
    if r.metric == "upload_mbps":
        return f"up {r.value:.1f} Mbps"
    if r.metric == "ip_changed":
        return r.message
    return f"{r.metric}={r.value:.1f}"


def _format_results(results: list[MonitorResult]) -> str:
    """Format a poll's results as compact per-target detail, e.g.

    "8.8.8.8 30.8ms 0% | 1.1.1.1 26.1ms 0%"

    Groups metrics by target, in the order targets first appear.
    """
    targets: dict[str, list[str]] = {}
    order: list[str] = []
    for r in results:
        if r.target not in targets:
            targets[r.target] = []
            order.append(r.target)
        targets[r.target].append(_format_metric(r))
    return " | ".join(f"{target} {' '.join(targets[target])}" for target in order)

# ---------------------------------------------------------------------------
# Global shared state
# ---------------------------------------------------------------------------

_monitor_status: dict[str, str] = {}
_monitor_configs: dict[str, dict] = {}
_led: Optional[LEDController] = None
_alerter: Optional[EmailAlerter] = None

# Latest results per monitor, kept for the rank-sorted LED strip: every poll
# rescores every monitor seen so far from its own latest results, so a
# monitor on a slow interval (e.g. speedtest) isn't dropped from the strip
# between its own polls just because a faster monitor (e.g. ping) just ran.
_monitor_results: dict[str, list[MonitorResult]] = {}
_led_weights: dict[str, float] = {}


async def _clear_degraded_state(monitor_name: str) -> None:
    """Clear the degraded-episode state keys for a monitor (write "", not delete)."""
    await db.set_state(f"degraded_since:{monitor_name}", "")
    await db.set_state(f"degraded_cycles:{monitor_name}", "")
    await db.set_state(f"degraded_onset:{monitor_name}", "")


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------

def _build_executors() -> dict[str, object]:
    """Executors for the scheduler: async jobs on the loop, blocking work on threads.

    Values must be executor instances (or `{"type": ...}` config dicts) — a bare
    alias string such as `"asyncio"` is rejected by `AsyncIOScheduler.configure()`.
    """
    return {
        "default": AsyncIOExecutor(),
        "threadpool": ThreadPoolExecutor(max_workers=2),
    }


# ---------------------------------------------------------------------------
# Startup sequence
# ---------------------------------------------------------------------------

async def _startup_sequence(
    alerter: "EmailAlerter",
    now: Optional[datetime] = None,
) -> None:
    """Classify startup type, emit event, send startup email, flush queue."""
    if now is None:
        now = datetime.now(timezone.utc)
    startup_ts = now.isoformat(timespec="seconds")

    shutdown_at = await db.get_state("shutdown_at") or ""
    last_seen_at = await db.get_state("last_seen_at") or ""

    # Classify
    if shutdown_at:
        shutdown_type = "clean"
        reference_ts = shutdown_at
    elif last_seen_at:
        shutdown_type = "unclean"
        reference_ts = last_seen_at
    else:
        shutdown_type = "first_run"
        reference_ts = None

    # Format downtime
    downtime_str = ""
    if reference_ts:
        ref_dt = datetime.fromisoformat(reference_ts)
        total_s = max(0, int((now - ref_dt).total_seconds()))
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
    pending = await db.get_pending_alerts()
    queued_count = len(pending)

    # Send startup email (best-effort — not queued on failure)
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
    await alerter.send_best_effort("[Schminternet] Service restarted", body)

    # Flush queued alerts
    await alerter.flush_alert_queue()

    # Clear shutdown_at only (last_seen_at left for heartbeat to overwrite)
    await db.set_state("shutdown_at", "")


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

    # Poll summary — one line per monitor per poll, INFO when ok, WARNING otherwise.
    logger.log(
        logging.INFO if new_status == "ok" else logging.WARNING,
        "%-*s %-*s %s",
        _NAME_WIDTH, monitor_name, _STATUS_WIDTH, new_status, _format_results(results),
    )

    previous = _monitor_status.get(monitor_name, "unknown")
    _monitor_status[monitor_name] = new_status

    # LED update — rank-sorted quality strip. "ip" is excluded: an address
    # change is an event, not a quality measure (leds/quality.py).
    _monitor_results[monitor_name] = results
    if monitor_name != "ip":
        scores = {
            name: score_monitor(name, res, _monitor_configs.get(name, {}))
            for name, res in _monitor_results.items()
            if name != "ip"
        }
        overall = overall_score(scores, _led_weights)
        ranking = sorted(scores.items(), key=lambda kv: -kv[1])

        logger.debug(
            "LED ranking: %s",
            " > ".join(f"{name} {score:.2f}" for name, score in ranking),
        )

        set_quality_snapshot(
            {
                "scores": scores,
                "overall": overall,
                "ranking": [name for name, _score in ranking],
                "colors": {
                    **{name: quality_color_hex(score) for name, score in scores.items()},
                    "overall": quality_color_hex(overall),
                },
            }
        )

        if _led:
            await _led.render_quality(scores, overall)

    # Transition line — logged regardless of whether an alerter is configured
    # (email alerting is opt-in; the operator still needs this in the logfile).
    if previous != new_status:
        logger.log(
            logging.INFO if new_status == "ok" else logging.WARNING,
            "EVENT %s %s -> %s",
            monitor_name, previous, new_status,
        )

    # ── Transition-based alerts ────────────────────────────────────────────
    if _alerter and previous != new_status:
        ts = results[0].timestamp
        if new_status == "down":
            if previous == "degraded":
                # Log that the degraded episode ended (entered down)
                await db.insert_event(
                    {
                        "timestamp": ts,
                        "event_type": "status_change",
                        "monitor": monitor_name,
                        "description": "Status changed: degraded → down",
                        "previous_status": "degraded",
                        "new_status": "down",
                    }
                )
            # Clear any degraded state
            await _clear_degraded_state(monitor_name)

            # Existing down-alert path (previous != "down" is guaranteed by the
            # outer guard, since previous != new_status and new_status == "down")
            desc = "; ".join(r.message for r in results if r.message) or "No detail"
            await _alerter.send_alert(monitor_name, desc, new_status, previous, ts)
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

        elif new_status == "ok" and previous == "degraded":
            # Degraded resolved — clear state, log event, no alert
            await _clear_degraded_state(monitor_name)
            await db.insert_event(
                {
                    "timestamp": ts,
                    "event_type": "status_change",
                    "monitor": monitor_name,
                    "description": "Status recovered: degraded → ok",
                    "previous_status": "degraded",
                    "new_status": "ok",
                }
            )

        elif new_status != "degraded":
            # Catch-all for any transition landing outside "degraded" that
            # the branches above didn't handle (e.g. unknown → ok after a
            # restart mid-episode, or a hypothetical degraded → unknown —
            # no monitor emits "unknown" today, but the invariant should
            # not depend on that): drop stale degraded state left over so
            # a future degraded episode doesn't inherit it. Must stay after
            # the three specific branches above (down, down → ok, and
            # degraded → ok) — they match first since they're narrower and
            # evaluated earlier, so their behavior is unaffected. The
            # degraded-onset branch below it is unaffected too, since
            # new_status != "degraded" never matches when new_status ==
            # "degraded".
            await _clear_degraded_state(monitor_name)

        elif new_status == "degraded" and previous != "degraded":
            # Onset: log the transition event
            await db.insert_event(
                {
                    "timestamp": ts,
                    "event_type": "status_change",
                    "monitor": monitor_name,
                    "description": f"Status changed: {previous} → degraded",
                    "previous_status": previous,
                    "new_status": "degraded",
                }
            )

    # ── Degraded accumulation (runs every poll where new_status == "degraded") ──
    if _alerter and new_status == "degraded":
        ts = results[0].timestamp
        now = datetime.now(timezone.utc)
        monitor_cfg = _monitor_configs.get(monitor_name, {})

        since = await db.get_state(f"degraded_since:{monitor_name}") or ""
        onset = await db.get_state(f"degraded_onset:{monitor_name}") or ""
        cycles = int(await db.get_state(f"degraded_cycles:{monitor_name}") or "0") + 1

        if not since:
            since = ts  # first degraded poll for this episode
        if not onset:
            # True episode onset — written only here, either at episode
            # start (mirrors the `since` seed above) or as a backfill when a
            # DB written before this key existed lands mid-episode (`since`
            # already set, `onset` still empty). Never touched again while
            # the episode continues (unlike degraded_since above, which is
            # deliberately reset to `ts` after each alert fires, since it
            # also doubles as the threshold-window start). Kept as a
            # separate key so the queue de-dup below can be scoped to the
            # whole episode instead of drifting forward on every other
            # alert. Written before degraded_since below on purpose: a crash
            # between the two writes leaves degraded_since empty, and the
            # next poll re-seeds both from scratch — writing them in the
            # reverse order would strand a stale onset paired with an empty
            # since.
            onset = since
            await db.set_state(f"degraded_onset:{monitor_name}", onset)

        await db.set_state(f"degraded_since:{monitor_name}", since)
        await db.set_state(f"degraded_cycles:{monitor_name}", str(cycles))

        threshold_minutes = monitor_cfg.get("degraded_alert_minutes")
        threshold_cycles = monitor_cfg.get("degraded_alert_cycles")

        if threshold_minutes or threshold_cycles:
            elapsed_minutes = (now - datetime.fromisoformat(since)).total_seconds() / 60
            minutes_hit = threshold_minutes and elapsed_minutes >= float(threshold_minutes)
            cycles_hit = threshold_cycles and cycles >= int(threshold_cycles)

            if minutes_hit or cycles_hit:
                duration_str = f"{int(elapsed_minutes)}m ({cycles} polls)"
                desc = "; ".join(r.message for r in results if r.message) or "No detail"
                alert_in_flight = await _alerter.send_degraded_alert(
                    monitor_name, desc, ts, duration_str, onset
                )
                if alert_in_flight:
                    # Reset timer so full threshold must be crossed again after cooldown
                    await db.set_state(f"degraded_since:{monitor_name}", ts)
                    await db.set_state(f"degraded_cycles:{monitor_name}", "1")

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
# Adaptive speedtest job — owns the intensity level, reschedules itself
# ---------------------------------------------------------------------------

class SpeedtestJob:
    """Scheduler callable for the speedtest monitor.

    Holds the current rung of monitors.speedtest.adaptive.ladder. After each
    run it classifies the result (speedtest_policy.verdict), moves one rung
    up on a poor run or one rung down after `calm_after` good runs
    (speedtest_policy.next_level), and reschedules its own APScheduler
    interval when the rung changes. Starts at rung 1 on every process start.
    See docs/superpowers/specs/2026-10-09-cloudflare-adaptive-speedtest-design.md.
    """

    def __init__(self, config: dict, scheduler, job_id: str = "speedtest") -> None:
        self._config = config
        self._scheduler = scheduler
        self._job_id = job_id
        cfg = config["monitors"]["speedtest"]
        self._expected = float(cfg.get("expected_download_mbps", 0))
        self._degraded_ratio = float(cfg.get("thresholds", {}).get("degraded_ratio", 0.5))
        adaptive = cfg["adaptive"]
        self._ladder: list[dict] = adaptive["ladder"]
        self._avg_ratio = float(adaptive.get("avg_ratio", 0.8))
        self._min_samples = int(adaptive.get("min_samples", 3))
        self._calm_after = int(adaptive.get("calm_after", 2))
        self.level = min(1, len(self._ladder) - 1)
        self.good_streak = 0

    @property
    def interval_seconds(self) -> int:
        return int(self._ladder[self.level]["interval_seconds"])

    def _params(self) -> dict:
        return {"level": self.level, **self._ladder[self.level]}

    async def __call__(self) -> None:
        results = await speedtest_monitor.run(self._config, self._params())
        await run_monitor("speedtest", results)

        download = next(
            (r.value for r in results if r.metric == "download_mbps"), -1.0
        )
        history = await db.recent_values("speedtest", "cloudflare", "download_mbps", hours=24)
        outcome = speedtest_policy.verdict(
            download, self._expected, self._degraded_ratio,
            history, self._avg_ratio, self._min_samples,
        )
        previous = self.level
        self.level, self.good_streak = speedtest_policy.next_level(
            self.level, outcome, self.good_streak, self._calm_after, len(self._ladder) - 1
        )
        if self.level != previous:
            self._scheduler.reschedule_job(
                self._job_id, trigger="interval", seconds=self.interval_seconds
            )
            logger.info(
                "speedtest %s run: level %d (%s) -> %d (%s), next in %ds",
                outcome, previous, self._ladder[previous]["name"],
                self.level, self._ladder[self.level]["name"], self.interval_seconds,
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    global _led, _alerter, _monitor_configs, _led_weights

    config = load_config()
    _configure_logging(config)
    _monitor_configs = config.get("monitors", {})

    # DB
    db_path = config.get("database", {}).get("path", "data/schminternet.db")
    db.configure(db_path)
    await db.init_db()
    logger.info("Database ready at %s", db_path)

    # LEDs
    leds_cfg = config.get("leds", {})
    _led = LEDController(leds_cfg)
    _led_weights = leds_cfg.get("weights", {})

    # Alerter
    _alerter = EmailAlerter(config.get("alerts", {}).get("email", {}))

    await _startup_sequence(_alerter)

    # Pull monitor config sections
    ping_cfg     = config["monitors"]["ping"]
    dns_cfg      = config["monitors"]["dns"]
    http_cfg     = config["monitors"]["http"]
    ip_cfg       = config["monitors"]["ip"]
    retention    = config["database"]["retention_days"]

    # ---------------------------------------------------------------------------
    # Scheduler — closure-wrapped jobs so each async job has access to config
    # but doesn't need to pass it as an APScheduler job argument.
    # ---------------------------------------------------------------------------

    scheduler = AsyncIOScheduler(executors=_build_executors())

    job_speedtest = SpeedtestJob(config, scheduler)

    async def job_ping() -> None:
        await run_monitor("ping", await ping.run(config))

    async def job_dns() -> None:
        await run_monitor("dns", await dns_monitor.run(config))

    async def job_http() -> None:
        await run_monitor("http", await http_check.run(config))

    async def job_ip() -> None:
        await run_monitor("ip", await ip_tracker.run(config))

    async def job_cleanup() -> None:
        await db.cleanup_old(retention)
        logger.info("Retention cleanup complete (kept last %d days)", retention)

    async def job_heartbeat() -> None:
        await db.set_state("last_seen_at", datetime.now(timezone.utc).isoformat())

    async def job_flush_alerts() -> None:
        if _alerter:
            await _alerter.flush_alert_queue()

    scheduler.add_job(job_ping,      "interval", seconds=ping_cfg.get("interval_seconds", 30),    id="ping",      misfire_grace_time=15)
    scheduler.add_job(job_dns,       "interval", seconds=dns_cfg.get("interval_seconds", 60),     id="dns",       misfire_grace_time=30)
    # Pass the bound method, not the instance: APScheduler's AsyncIOExecutor uses
    # iscoroutinefunction_partial(job.func) to decide whether to await the job, and
    # that check returns False for an instance's async __call__, silently sending it
    # to a thread where the returned coroutine is never awaited or run.
    scheduler.add_job(job_speedtest.__call__, "interval", seconds=job_speedtest.interval_seconds, id="speedtest", misfire_grace_time=60, max_instances=1, coalesce=True)
    scheduler.add_job(job_http,      "interval", seconds=http_cfg.get("interval_seconds", 120),   id="http",      misfire_grace_time=30)
    scheduler.add_job(job_ip,        "interval", seconds=ip_cfg.get("interval_seconds", 300),     id="ip",        misfire_grace_time=60)
    scheduler.add_job(job_cleanup,   "cron",     hour=3,                                           id="cleanup")
    scheduler.add_job(job_heartbeat,    "interval", seconds=60, id="heartbeat")
    scheduler.add_job(job_flush_alerts, "interval", minutes=5,  id="alert_flush")

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
    loop = asyncio.get_running_loop()

    def _handle_signal(*_: object) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    server_task = asyncio.create_task(server.serve())

    await stop_event.wait()

    await db.set_state("shutdown_at", datetime.now(timezone.utc).isoformat())
    logger.info("Shutting down…")
    scheduler.shutdown(wait=False)
    server.should_exit = True
    await server_task
    if _led:
        await _led.blackout()
    logger.info("Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
