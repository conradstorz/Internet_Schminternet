"""SMTP email alerter with per-monitor cooldown and recovery notifications."""

from __future__ import annotations

import asyncio
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import storage.db as db

logger = logging.getLogger(__name__)


class EmailAlerter:
    """Send alert and recovery emails via SMTP with a configurable cooldown."""

    def __init__(self, config: dict) -> None:
        self._cfg = config
        self._cooldowns: dict[str, datetime] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

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

    async def send_best_effort(self, subject: str, body: str) -> bool:
        """Send an email without cooldown or queueing. Returns True on success."""
        if not self._enabled():
            return False
        return await self._send(subject, body)

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
                + f"Queued at (UTC):    {row['queued_at']}\n"
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

    async def send_degraded_alert(
        self,
        monitor: str,
        description: str,
        created_at: str,
        duration_str: str,
        episode_since: str,
    ) -> bool:
        """Emit a 'monitor is DEGRADED' email, respecting the cooldown window.

        ``episode_since`` is the true onset timestamp of the CURRENT degraded
        episode — ``run_monitor()``'s ``degraded_onset:{monitor}`` state key,
        written once when the episode begins and never touched again while it
        continues (unlike ``degraded_since:{monitor}``, which doubles as the
        threshold-window start and is deliberately reset to the current event
        timestamp after every alert that fires). Passing the stable onset
        here — rather than the threshold-window start — is what keeps this
        value identical across every crossing within one episode. It scopes
        the queue de-dup below to this episode only, so a pending row left
        over from an older, already-ended episode can never suppress the
        alert for a new one.

        Returns True when an alert for this episode is now in flight — the
        email was sent, was queued for later delivery, or a pending row
        already covers THIS episode (same subject, and that row's
        ``created_at`` is at or after ``episode_since``), so nothing new is
        enqueued. Returns False when nothing was done: alerting is
        disabled, or the send was suppressed by the cooldown. Callers use
        the return value to decide whether to reset the degraded-episode
        timer.
        """
        if not self._enabled():
            return False
        if self._in_cooldown(monitor):
            logger.debug(
                "Degraded alert suppressed for %s (in cooldown); episode timer not reset",
                monitor,
            )
            return False

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
            return True

        # All these timestamps are datetime.now(timezone.utc).isoformat()
        # strings in a single fixed format, so a plain string comparison
        # orders them correctly (no need to parse back to datetime).
        pending = await db.get_pending_alerts()
        if any(
            row["subject"] == subject and row["created_at"] >= episode_since
            for row in pending
        ):
            logger.debug(
                "Degraded alert for %s already pending for this episode; skipping enqueue",
                monitor,
            )
            return True

        await db.enqueue_alert(created_at, subject, body)
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _enabled(self) -> bool:
        return bool(self._cfg.get("enabled", False))

    def _in_cooldown(self, monitor: str) -> bool:
        last = self._cooldowns.get(monitor)
        if last is None:
            return False
        cooldown = timedelta(minutes=float(self._cfg.get("cooldown_minutes", 15)))
        return datetime.now(timezone.utc) - last < cooldown

    async def _send(self, subject: str, body: str) -> bool:
        """Send an email via SMTP in a thread executor. Returns True on success."""
        cfg = self._cfg
        required = ("smtp_host", "from_addr", "to_addrs", "username", "password")
        for key in required:
            if not cfg.get(key):
                logger.warning("Email alert skipped — missing config key: %s", key)
                return False

        msg = MIMEMultipart()
        msg["From"] = cfg["from_addr"]
        msg["To"] = ", ".join(cfg["to_addrs"])
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        def _smtp_send() -> None:
            with smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port", 587))) as server:
                if cfg.get("use_tls", True):
                    server.starttls()
                server.login(cfg["username"], cfg["password"])
                server.sendmail(cfg["from_addr"], cfg["to_addrs"], msg.as_string())

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _smtp_send)
            logger.info("Alert email sent: %s", subject)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send alert email: %s", exc)
            return False
