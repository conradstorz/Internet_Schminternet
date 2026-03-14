"""Tests for alert queuing — EmailAlerter enqueue-on-failure behaviour."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import storage.db as db
from alerts.email_alert import EmailAlerter


@pytest.fixture(autouse=True)
async def isolated_db(tmp_path):
    db.configure(str(tmp_path / "test.db"))
    await db.init_db()


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
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError):
        await alerter.send_alert("ping", "went down", "down", "ok", ts)
    rows = await db.get_pending_alerts()
    assert len(rows) == 1
    assert rows[0]["created_at"] == ts
    assert "PING" in rows[0]["subject"]


async def test_failed_send_recovery_enqueues_row():
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError):
        await alerter.send_recovery("ping", "back up", ts)
    rows = await db.get_pending_alerts()
    assert len(rows) == 1
    assert "PING" in rows[0]["subject"]


async def test_successful_send_does_not_enqueue():
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", return_value=_smtp_success()):
        await alerter.send_alert("ping", "went down", "down", "ok", ts)
    assert await db.get_pending_alerts() == []


async def test_body_includes_event_time():
    """The rendered email body must include the original event timestamp."""
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError):
        await alerter.send_alert("ping", "went down", "down", "ok", ts)
    rows = await db.get_pending_alerts()
    assert ts in rows[0]["body"]
