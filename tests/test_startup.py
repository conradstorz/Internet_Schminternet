"""Tests for startup sequence — downtime classification and event/email emission."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import storage.db as db
from main import _startup_sequence


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db.configure(str(tmp_path / "test.db"))


def _alerter(enabled: bool = False) -> MagicMock:
    """Return a mock EmailAlerter. Disabled by default (no real SMTP)."""
    alerter = MagicMock()
    alerter._enabled = MagicMock(return_value=enabled)
    alerter._send = AsyncMock(return_value=True)
    alerter.flush_alert_queue = AsyncMock()
    return alerter


async def test_first_run_writes_event_and_sends_no_email():
    await db.init_db()
    alerter = _alerter(enabled=True)
    await _startup_sequence(alerter)
    events = await db.get_events()
    assert len(events) == 1
    assert events[0]["event_type"] == "startup"
    assert events[0]["description"] == "First run."
    alerter._send.assert_not_called()


async def test_clean_shutdown_calculates_downtime():
    await db.init_db()
    now = datetime(2026, 3, 14, 3, 0, 0, tzinfo=timezone.utc)
    shutdown = now - timedelta(minutes=32, seconds=35)
    await db.set_state("shutdown_at", shutdown.isoformat())
    await db.set_state("last_seen_at", shutdown.isoformat())

    alerter = _alerter(enabled=True)
    await _startup_sequence(alerter, now=now)

    events = await db.get_events()
    assert "Clean" in events[0]["description"]
    assert "32m" in events[0]["description"] or "32 m" in events[0]["description"]


async def test_unclean_shutdown_uses_last_seen_at():
    await db.init_db()
    now = datetime(2026, 3, 14, 3, 0, 0, tzinfo=timezone.utc)
    last_seen = now - timedelta(minutes=15)
    await db.set_state("last_seen_at", last_seen.isoformat())
    # No shutdown_at set

    alerter = _alerter(enabled=True)
    await _startup_sequence(alerter, now=now)

    events = await db.get_events()
    assert "Unclean" in events[0]["description"]
    assert "15m" in events[0]["description"] or "15 m" in events[0]["description"]


async def test_empty_string_shutdown_at_treated_as_unclean():
    await db.init_db()
    now = datetime(2026, 3, 14, 3, 0, 0, tzinfo=timezone.utc)
    last_seen = now - timedelta(minutes=5)
    await db.set_state("shutdown_at", "")   # empty string = not set
    await db.set_state("last_seen_at", last_seen.isoformat())

    alerter = _alerter(enabled=True)
    await _startup_sequence(alerter, now=now)

    events = await db.get_events()
    assert "Unclean" in events[0]["description"]


async def test_shutdown_at_cleared_after_startup():
    await db.init_db()
    now = datetime(2026, 3, 14, 3, 0, 0, tzinfo=timezone.utc)
    shutdown = now - timedelta(minutes=5)
    await db.set_state("shutdown_at", shutdown.isoformat())

    alerter = _alerter()
    await _startup_sequence(alerter, now=now)

    assert not (await db.get_state("shutdown_at"))


async def test_last_seen_at_not_cleared_after_startup():
    await db.init_db()
    now = datetime(2026, 3, 14, 3, 0, 0, tzinfo=timezone.utc)
    last_seen = now - timedelta(minutes=5)
    await db.set_state("shutdown_at", (now - timedelta(minutes=2)).isoformat())
    await db.set_state("last_seen_at", last_seen.isoformat())

    alerter = _alerter()
    await _startup_sequence(alerter, now=now)

    assert await db.get_state("last_seen_at") == last_seen.isoformat()


async def test_startup_email_includes_queued_count():
    await db.init_db()
    now = datetime(2026, 3, 14, 3, 0, 0, tzinfo=timezone.utc)
    shutdown = now - timedelta(minutes=10)
    await db.set_state("shutdown_at", shutdown.isoformat())
    # Pre-queue two alerts
    ts = shutdown.isoformat()
    await db.enqueue_alert(ts, "Alert 1", "body1")
    await db.enqueue_alert(ts, "Alert 2", "body2")

    alerter = _alerter(enabled=True)
    await _startup_sequence(alerter, now=now)

    # Verify the startup email body mentions 2 queued alerts
    call_args = alerter._send.call_args
    body = call_args[0][1]  # second positional arg to _send
    assert "2 alert" in body


async def test_startup_email_omits_queued_sentence_when_none():
    await db.init_db()
    now = datetime(2026, 3, 14, 3, 0, 0, tzinfo=timezone.utc)
    shutdown = now - timedelta(minutes=5)
    await db.set_state("shutdown_at", shutdown.isoformat())

    alerter = _alerter(enabled=True)
    await _startup_sequence(alerter, now=now)

    call_args = alerter._send.call_args
    body = call_args[0][1]
    assert "alert" not in body.lower()


async def test_flush_called_after_startup_email():
    await db.init_db()
    now = datetime(2026, 3, 14, 3, 0, 0, tzinfo=timezone.utc)
    shutdown = now - timedelta(minutes=5)
    await db.set_state("shutdown_at", shutdown.isoformat())

    alerter = _alerter(enabled=True)
    await _startup_sequence(alerter, now=now)

    alerter.flush_alert_queue.assert_called_once()
