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


async def test_flush_delivers_queued_alert():
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError):
        await alerter.send_alert("ping", "down", "down", "ok", ts)
    assert len(await db.get_pending_alerts()) == 1

    with patch("smtplib.SMTP", return_value=_smtp_success()):
        await alerter.flush_alert_queue()
    assert await db.get_pending_alerts() == []


async def test_flush_leaves_row_on_continued_failure():
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError):
        await alerter.send_alert("ping", "down", "down", "ok", ts)
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError):
        await alerter.flush_alert_queue()
    rows = await db.get_pending_alerts()
    assert len(rows) == 1
    assert rows[0]["attempt_count"] == 1


async def test_flush_appends_all_three_timestamps():
    """Delivered email must contain event time, queued-at, and delivered-at."""
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError):
        await alerter.send_alert("ping", "down", "down", "ok", ts)

    captured: list[str] = []

    def capturing_smtp(*_a, **_kw):
        server = MagicMock()
        server.sendmail = lambda *a: captured.append(a[2])
        smtp = MagicMock()
        smtp.__enter__ = MagicMock(return_value=server)
        smtp.__exit__ = MagicMock(return_value=False)
        return smtp

    with patch("smtplib.SMTP", side_effect=capturing_smtp):
        await alerter.flush_alert_queue()

    assert len(captured) == 1
    assert "Event time (UTC)" in captured[0]
    assert "Queued at (UTC)" in captured[0]
    assert "Delivered at (UTC)" in captured[0]


async def test_flush_is_noop_on_empty_queue():
    alerter = EmailAlerter(_cfg())
    with patch("smtplib.SMTP") as mock_smtp:
        await alerter.flush_alert_queue()
    mock_smtp.assert_not_called()


async def test_flush_is_noop_when_disabled():
    alerter = EmailAlerter(_cfg(enabled=False))
    ts = datetime.now(timezone.utc).isoformat()
    await db.enqueue_alert(ts, "subject", "body")
    with patch("smtplib.SMTP") as mock_smtp:
        await alerter.flush_alert_queue()
    mock_smtp.assert_not_called()
    assert len(await db.get_pending_alerts()) == 1


async def test_flush_skips_cooldown_checks():
    """Rows in the queue are delivered regardless of cooldown state."""
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    await db.enqueue_alert(ts, "[Schminternet] PING is DOWN", f"Event time (UTC): {ts}\n")
    alerter._cooldowns["ping"] = datetime.now(timezone.utc)  # artificially in cooldown
    with patch("smtplib.SMTP", return_value=_smtp_success()):
        await alerter.flush_alert_queue()
    assert await db.get_pending_alerts() == []


async def test_flush_idempotent_delete_does_not_raise():
    """delete_queued_alert on an already-deleted row must be silent.

    Simulates the race where flush reads a row, SMTP succeeds, but the row
    was already deleted by a concurrent flush before delete_queued_alert runs.
    """
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    await db.enqueue_alert(ts, "subject", "body")
    stale_rows = await db.get_pending_alerts()
    # Delete the row from the DB now so it's gone by the time flush tries to delete it
    await db.delete_queued_alert(stale_rows[0]["id"])
    # Patch get_pending_alerts to return the stale row so flush actually reaches
    # the delete path, even though the underlying DB row is already gone
    with patch("storage.db.get_pending_alerts", return_value=stale_rows):
        with patch("smtplib.SMTP", return_value=_smtp_success()):
            await alerter.flush_alert_queue()  # must not raise
