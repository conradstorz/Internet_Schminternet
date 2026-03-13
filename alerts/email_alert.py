"""SMTP email alerter with per-monitor cooldown and recovery notifications."""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


class EmailAlerter:
    """Send alert and recovery emails via SMTP with a configurable cooldown."""

    def __init__(self, config: dict) -> None:
        self._cfg = config
        self._cooldowns: dict[str, datetime] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def send_alert(
        self,
        monitor: str,
        description: str,
        new_status: str,
        previous_status: str,
    ) -> None:
        """Emit a 'monitor is DOWN' email, respecting the cooldown window."""
        if not self._enabled():
            return
        if self._in_cooldown(monitor):
            logger.debug("Alert suppressed for %s (in cooldown)", monitor)
            return

        subject = f"[Schminternet] {monitor.upper()} is {new_status.upper()}"
        body = (
            f"Monitor:         {monitor}\n"
            f"Previous status: {previous_status}\n"
            f"Current status:  {new_status}\n"
            f"Details:         {description}\n"
            f"Time (UTC):      {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        )
        self._send(subject, body)
        self._cooldowns[monitor] = datetime.now(timezone.utc)

    def send_recovery(self, monitor: str, description: str) -> None:
        """Emit a 'monitor has RECOVERED' email and clear the cooldown."""
        if not self._enabled():
            return

        subject = f"[Schminternet] {monitor.upper()} has RECOVERED"
        body = (
            f"Monitor:    {monitor}\n"
            f"Status:     OK (recovered)\n"
            f"Details:    {description}\n"
            f"Time (UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        )
        self._send(subject, body)
        self._cooldowns.pop(monitor, None)

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

    def _send(self, subject: str, body: str) -> None:
        cfg = self._cfg
        required = ("smtp_host", "from_addr", "to_addrs", "username", "password")
        for key in required:
            if not cfg.get(key):
                logger.warning("Email alert skipped — missing config key: %s", key)
                return

        msg = MIMEMultipart()
        msg["From"] = cfg["from_addr"]
        msg["To"] = ", ".join(cfg["to_addrs"])
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        try:
            with smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port", 587))) as server:
                if cfg.get("use_tls", True):
                    server.starttls()
                server.login(cfg["username"], cfg["password"])
                server.sendmail(cfg["from_addr"], cfg["to_addrs"], msg.as_string())
            logger.info("Alert email sent: %s", subject)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send alert email: %s", exc)
