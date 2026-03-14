"""Tests for alerts/email_alert.py — send behaviour and cooldown logic."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import storage.db as db
from alerts.email_alert import EmailAlerter


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


def _mock_smtp_success():
    """Return a context-manager mock that simulates a successful SMTP send."""
    smtp = MagicMock()
    smtp.__enter__ = MagicMock(return_value=smtp)
    smtp.__exit__ = MagicMock(return_value=False)
    return smtp


@pytest.fixture(autouse=True)
async def isolated_db(tmp_path):
    db.configure(str(tmp_path / "test.db"))
    await db.init_db()


# ---------------------------------------------------------------------------
# send_alert must be async (so it can offload SMTP to a thread)
# ---------------------------------------------------------------------------

async def test_send_alert_is_a_coroutine():
    """send_alert must be async so it doesn't block the event loop."""
    alerter = EmailAlerter(_cfg())
    assert inspect.iscoroutinefunction(alerter.send_alert)


async def test_send_recovery_is_a_coroutine():
    """send_recovery must be async so it doesn't block the event loop."""
    alerter = EmailAlerter(_cfg())
    assert inspect.iscoroutinefunction(alerter.send_recovery)


# ---------------------------------------------------------------------------
# Cooldown logic
# ---------------------------------------------------------------------------

async def test_cooldown_not_set_when_send_fails():
    """A failed SMTP send must NOT set the cooldown so the next attempt goes through."""
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
        await alerter.send_alert("ping", "went down", "down", "ok", ts)
    assert "ping" not in alerter._cooldowns


async def test_cooldown_set_when_send_succeeds():
    """A successful send must set the cooldown to suppress duplicate alerts."""
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", return_value=_mock_smtp_success()):
        await alerter.send_alert("ping", "went down", "down", "ok", ts)
    assert "ping" in alerter._cooldowns


async def test_recovery_clears_cooldown_on_success():
    """send_recovery clears the cooldown regardless of prior state."""
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", return_value=_mock_smtp_success()):
        await alerter.send_alert("ping", "went down", "down", "ok", ts)
    assert "ping" in alerter._cooldowns
    with patch("smtplib.SMTP", return_value=_mock_smtp_success()):
        await alerter.send_recovery("ping", "back up", ts)
    assert "ping" not in alerter._cooldowns


async def test_disabled_alerter_sends_nothing():
    """No SMTP call is made when alerts are disabled."""
    alerter = EmailAlerter(_cfg(enabled=False))
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP") as mock_smtp:
        await alerter.send_alert("ping", "down", "down", "ok", ts)
        mock_smtp.assert_not_called()
