"""Tests for alerts/email_alert.py — send behaviour and cooldown logic."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
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


# ---------------------------------------------------------------------------
# Degraded alerts
# ---------------------------------------------------------------------------

async def test_send_degraded_alert_sends_email():
    """A degraded alert is sent, sets the cooldown, and uses the DEGRADED subject."""
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    smtp = _mock_smtp_success()
    with patch("smtplib.SMTP", return_value=smtp) as mock_smtp:
        await alerter.send_degraded_alert("ping", "high latency", ts, "12m", ts)
    mock_smtp.assert_called_once()
    assert "ping" in alerter._cooldowns
    sent_message = smtp.sendmail.call_args.args[2]
    assert "Subject: [Schminternet] PING is DEGRADED" in sent_message


async def test_degraded_alert_respects_cooldown():
    """A monitor already in cooldown gets no degraded email and nothing queued."""
    alerter = EmailAlerter(_cfg())
    alerter._cooldowns["ping"] = datetime.now(timezone.utc)
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP") as mock_smtp:
        await alerter.send_degraded_alert("ping", "high latency", ts, "12m", ts)
    mock_smtp.assert_not_called()
    assert await db.get_pending_alerts() == []


async def test_degraded_alert_enqueues_on_failure():
    """A failed degraded send is queued and must not set the cooldown."""
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
        await alerter.send_degraded_alert("ping", "high latency", ts, "12m", ts)
    rows = await db.get_pending_alerts()
    assert len(rows) == 1
    assert rows[0]["subject"] == "[Schminternet] PING is DEGRADED"
    assert "ping" not in alerter._cooldowns


async def test_send_degraded_alert_disabled():
    """A disabled alerter neither sends nor queues a degraded alert."""
    alerter = EmailAlerter(_cfg(enabled=False))
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP") as mock_smtp:
        await alerter.send_degraded_alert("ping", "high latency", ts, "12m", ts)
    mock_smtp.assert_not_called()
    assert await db.get_pending_alerts() == []


# ---------------------------------------------------------------------------
# Degraded alerts — return value contract
# ---------------------------------------------------------------------------

async def test_send_degraded_alert_returns_true_on_success():
    """Returns True when the email was actually sent."""
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", return_value=_mock_smtp_success()):
        result = await alerter.send_degraded_alert("ping", "high latency", ts, "12m", ts)
    assert result is True


async def test_send_degraded_alert_returns_true_on_enqueue():
    """Returns True when the send failed but the alert was queued."""
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
        result = await alerter.send_degraded_alert("ping", "high latency", ts, "12m", ts)
    assert result is True


async def test_send_degraded_alert_returns_false_when_in_cooldown():
    """Returns False when suppressed by an active cooldown."""
    alerter = EmailAlerter(_cfg())
    alerter._cooldowns["ping"] = datetime.now(timezone.utc)
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP") as mock_smtp:
        result = await alerter.send_degraded_alert("ping", "high latency", ts, "12m", ts)
    mock_smtp.assert_not_called()
    assert result is False


async def test_send_degraded_alert_returns_false_when_disabled():
    """Returns False when alerting is disabled."""
    alerter = EmailAlerter(_cfg(enabled=False))
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP") as mock_smtp:
        result = await alerter.send_degraded_alert("ping", "high latency", ts, "12m", ts)
    mock_smtp.assert_not_called()
    assert result is False


# ---------------------------------------------------------------------------
# Degraded alerts — queue de-duplication (episode-scoped)
# ---------------------------------------------------------------------------

async def test_degraded_alert_dedups_within_same_episode():
    """Two consecutive failed sends carrying the SAME episode onset leave
    exactly one row in alert_queue, and the second call still returns True."""
    alerter = EmailAlerter(_cfg())
    onset = datetime.now(timezone.utc).isoformat()
    ts1 = onset
    ts2 = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
        first = await alerter.send_degraded_alert("ping", "high latency", ts1, "12m", onset)
        second = await alerter.send_degraded_alert("ping", "high latency", ts2, "22m", onset)
    assert first is True
    assert second is True
    rows = await db.get_pending_alerts()
    assert len(rows) == 1
    assert {r["subject"] for r in rows} == {"[Schminternet] PING is DEGRADED"}


async def test_degraded_alert_cross_episode_not_deduped():
    """A pending row from an OLDER episode (its created_at predates the new
    episode's onset) must NOT suppress the new enqueue — the new alert is
    queued as its own row, leaving two rows with the same subject."""
    alerter = EmailAlerter(_cfg())
    onset1 = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
        await alerter.send_degraded_alert("ping", "high latency", onset1, "12m", onset1)
    rows = await db.get_pending_alerts()
    assert len(rows) == 1
    first_created_at = rows[0]["created_at"]

    # Episode 2's onset is strictly later than episode 1's queued row —
    # these are all datetime.now(timezone.utc).isoformat() strings in a
    # single fixed format, so a later onset sorts as a greater string too.
    onset2 = (datetime.fromisoformat(first_created_at) + timedelta(days=1)).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
        second = await alerter.send_degraded_alert("ping", "high latency", onset2, "5m", onset2)
    assert second is True
    rows = await db.get_pending_alerts()
    assert len(rows) == 2
    assert {r["subject"] for r in rows} == {"[Schminternet] PING is DEGRADED"}


async def test_degraded_alert_dedup_keyed_on_subject_not_any_pending():
    """A pending row for a different monitor (different subject) must not
    suppress the enqueue for this monitor."""
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
        await alerter.send_degraded_alert("dns", "high latency", ts, "12m", ts)
        await alerter.send_degraded_alert("ping", "high latency", ts, "12m", ts)
    rows = await db.get_pending_alerts()
    assert len(rows) == 2
    assert {r["subject"] for r in rows} == {
        "[Schminternet] DNS is DEGRADED",
        "[Schminternet] PING is DEGRADED",
    }


async def test_degraded_alert_not_suppressed_by_pending_down_alert():
    """A pending DOWN-subject row for the same monitor (different subject)
    must not suppress a degraded enqueue."""
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
        await alerter.send_alert("ping", "went down", "down", "ok", ts)
    rows = await db.get_pending_alerts()
    assert {r["subject"] for r in rows} == {"[Schminternet] PING is DOWN"}

    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
        result = await alerter.send_degraded_alert("ping", "high latency", ts, "12m", ts)
    assert result is True
    rows = await db.get_pending_alerts()
    assert len(rows) == 2
    assert {r["subject"] for r in rows} == {
        "[Schminternet] PING is DOWN",
        "[Schminternet] PING is DEGRADED",
    }


async def test_send_alert_never_dedups_across_calls():
    """The de-dup added to send_degraded_alert must not leak into the down
    path: two consecutive failed send_alert calls still leave two rows."""
    alerter = EmailAlerter(_cfg())
    ts = datetime.now(timezone.utc).isoformat()
    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
        await alerter.send_alert("ping", "went down", "down", "ok", ts)
        await alerter.send_alert("ping", "still down", "down", "ok", ts)
    rows = await db.get_pending_alerts()
    assert len(rows) == 2
    assert {r["subject"] for r in rows} == {"[Schminternet] PING is DOWN"}
