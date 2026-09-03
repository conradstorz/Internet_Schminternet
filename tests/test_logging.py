"""Tests for the operational logfile: config-driven setup, per-poll summary/transition
lines, and the `_format_results` detail-string helper.

Drives `main._configure_logging` and `main.run_monitor` directly, following the
isolated-DB pattern established in tests/test_degraded_alerting.py.
"""

from __future__ import annotations

import logging
import logging.handlers
from datetime import datetime, timezone

import pytest

import main
import storage.db as db
from main import _configure_logging, _format_results
from monitors.base import MonitorResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOISY_LOGGERS = ("httpx", "apscheduler.scheduler", "apscheduler.executors")


@pytest.fixture(autouse=True)
def _reset_logging():
    """Snapshot/restore root + noisy-logger state so tests never bleed into each other."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_noisy_levels = {name: logging.getLogger(name).level for name in _NOISY_LOGGERS}

    yield

    for h in list(root.handlers):
        if h not in original_handlers:
            root.removeHandler(h)
            h.close()
    root.handlers[:] = original_handlers
    root.setLevel(original_level)
    for name, level in original_noisy_levels.items():
        logging.getLogger(name).setLevel(level)


def _cfg(tmp_path, **overrides) -> dict:
    log_cfg = {
        "path": str(tmp_path / "schminternet.log"),
        "level": "INFO",
        "max_bytes": 10_000_000,
        "backup_count": 5,
        "console": True,
    }
    log_cfg.update(overrides)
    return {"logging": log_cfg}


@pytest.fixture
async def env(tmp_path):
    """Isolated DB + reset module globals, matching tests/test_degraded_alerting.py."""
    db.configure(str(tmp_path / "test.db"))
    await db.init_db()
    main._monitor_status = {}
    main._monitor_configs = {}
    main._led = None
    main._alerter = None
    yield
    main._alerter = None
    main._monitor_status = {}
    main._monitor_configs = {}


def _mr(monitor="ping", target="8.8.8.8", metric="latency_ms", value=30.8, status="ok", message="") -> MonitorResult:
    return MonitorResult(
        monitor=monitor,
        target=target,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metric=metric,
        value=value,
        status=status,
        message=message,
    )


# ---------------------------------------------------------------------------
# 1. _configure_logging creates the parent dir + file, and the file receives
#    a subsequent log record.
# ---------------------------------------------------------------------------

def test_configure_logging_creates_parent_dir_and_file(tmp_path):
    log_path = tmp_path / "sub" / "dir" / "schminternet.log"
    _configure_logging(_cfg(tmp_path, path=str(log_path)))

    assert log_path.parent.is_dir()
    assert log_path.exists()

    logging.getLogger("test_configure_logging").info("hello operator")

    content = log_path.read_text(encoding="utf-8")
    assert "hello operator" in content


# ---------------------------------------------------------------------------
# 2. Attached handler is a RotatingFileHandler carrying configured maxBytes
#    and backupCount.
# ---------------------------------------------------------------------------

def test_file_handler_has_configured_rotation_settings(tmp_path):
    _configure_logging(_cfg(tmp_path, max_bytes=123456, backup_count=3))

    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(file_handlers) == 1
    assert file_handlers[0].maxBytes == 123456
    assert file_handlers[0].backupCount == 3


# ---------------------------------------------------------------------------
# 3. Calling _configure_logging twice does not duplicate handlers.
# ---------------------------------------------------------------------------

def test_configure_logging_twice_does_not_duplicate_handlers(tmp_path):
    cfg = _cfg(tmp_path)
    _configure_logging(cfg)
    _configure_logging(cfg)

    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    console_handlers = [h for h in root.handlers if type(h) is logging.StreamHandler]
    assert len(file_handlers) == 1
    assert len(console_handlers) == 1


# ---------------------------------------------------------------------------
# 4. console: false removes the stdout handler; the default keeps it.
# ---------------------------------------------------------------------------

def test_console_false_removes_stream_handler(tmp_path):
    _configure_logging(_cfg(tmp_path, console=False))
    root = logging.getLogger()
    assert [h for h in root.handlers if type(h) is logging.StreamHandler] == []


def test_console_default_keeps_stream_handler(tmp_path):
    _configure_logging(_cfg(tmp_path))
    root = logging.getLogger()
    assert [h for h in root.handlers if type(h) is logging.StreamHandler] != []


# ---------------------------------------------------------------------------
# 5. An unwritable path logs a warning and does not raise.
# ---------------------------------------------------------------------------

def test_unwritable_path_logs_warning_and_does_not_raise(tmp_path, caplog):
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("blocking file", encoding="utf-8")
    bad_path = blocker / "sub" / "schminternet.log"

    with caplog.at_level(logging.WARNING, logger="main"):
        _configure_logging(_cfg(tmp_path, path=str(bad_path)))  # must not raise

    assert "Could not" in caplog.text
    assert str(bad_path) in caplog.text


# ---------------------------------------------------------------------------
# 6. httpx and apscheduler loggers end up at WARNING.
# ---------------------------------------------------------------------------

def test_third_party_loggers_set_to_warning(tmp_path):
    _configure_logging(_cfg(tmp_path))
    for name in _NOISY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


# ---------------------------------------------------------------------------
# 7. _format_results table-driven cases.
# ---------------------------------------------------------------------------

def test_format_results_ping_one_target_two_metrics():
    results = [
        _mr(monitor="ping", target="8.8.8.8", metric="latency_ms", value=30.8),
        _mr(monitor="ping", target="8.8.8.8", metric="packet_loss_pct", value=0),
    ]
    assert _format_results(results) == "8.8.8.8 30.8ms 0%"


def test_format_results_ping_fractional_packet_loss():
    results = [
        _mr(monitor="ping", target="1.1.1.1", metric="packet_loss_pct", value=12.5),
    ]
    assert _format_results(results) == "1.1.1.1 12.5%"


def test_format_results_dns_two_targets():
    results = [
        _mr(monitor="dns", target="8.8.8.8", metric="resolution_ms", value=12.3),
        _mr(monitor="dns", target="1.1.1.1", metric="resolution_ms", value=15.0),
    ]
    assert _format_results(results) == "8.8.8.8 12.3ms | 1.1.1.1 15.0ms"


def test_format_results_http():
    results = [
        _mr(monitor="http", target="https://www.cloudflare.com", metric="response_ms", value=1421.3),
    ]
    assert _format_results(results) == "https://www.cloudflare.com 1421.3ms"


def test_format_results_speedtest_download_and_upload():
    results = [
        _mr(monitor="speedtest", target="speedtest", metric="download_mbps", value=412.3),
        _mr(monitor="speedtest", target="speedtest", metric="upload_mbps", value=35.2),
    ]
    assert _format_results(results) == "speedtest down 412.3 Mbps up 35.2 Mbps"


def test_format_results_ip_change():
    results = [
        _mr(monitor="ip", target="external", metric="ip_changed", value=1.0, status="degraded", message="71.62.10.4"),
    ]
    assert _format_results(results) == "external 71.62.10.4"


def test_format_results_error_sentinel_with_message():
    results = [
        _mr(monitor="ping", target="8.8.8.8", metric="latency_ms", value=-1.0, status="down", message="timed out"),
    ]
    assert _format_results(results) == "8.8.8.8 error (timed out)"


def test_format_results_error_sentinel_without_message():
    results = [
        _mr(monitor="ping", target="8.8.8.8", metric="latency_ms", value=-1.0, status="down", message=""),
    ]
    assert _format_results(results) == "8.8.8.8 error"


def test_format_results_unknown_metric():
    results = [
        _mr(monitor="speedtest", target="speedtest", metric="ping_ms", value=1.5),
    ]
    assert _format_results(results) == "speedtest ping_ms=1.5"


# ---------------------------------------------------------------------------
# 8. run_monitor logs exactly one summary line per call: INFO for ok,
#    WARNING for degraded/down.
# ---------------------------------------------------------------------------

def _summary_records(records):
    return [r for r in records if r.name == "main" and "ping" in r.getMessage() and "EVENT" not in r.getMessage()]


async def test_run_monitor_logs_one_summary_line_ok(env, caplog):
    with caplog.at_level(logging.INFO, logger="main"):
        await main.run_monitor("ping", [_mr(status="ok")])

    summaries = _summary_records(caplog.records)
    assert len(summaries) == 1
    assert summaries[0].levelno == logging.INFO


async def test_run_monitor_logs_one_summary_line_degraded(env, caplog):
    with caplog.at_level(logging.INFO, logger="main"):
        await main.run_monitor("ping", [_mr(status="degraded")])

    summaries = _summary_records(caplog.records)
    assert len(summaries) == 1
    assert summaries[0].levelno == logging.WARNING


async def test_run_monitor_logs_one_summary_line_down(env, caplog):
    with caplog.at_level(logging.INFO, logger="main"):
        await main.run_monitor("ping", [_mr(status="down")])

    summaries = _summary_records(caplog.records)
    assert len(summaries) == 1
    assert summaries[0].levelno == logging.WARNING


# ---------------------------------------------------------------------------
# 9. run_monitor logs the transition line when main._alerter is None.
# ---------------------------------------------------------------------------

async def test_run_monitor_logs_transition_line_without_alerter(env, caplog):
    main._monitor_status["ping"] = "ok"
    assert main._alerter is None

    with caplog.at_level(logging.INFO, logger="main"):
        await main.run_monitor("ping", [_mr(status="degraded")])

    transitions = [r for r in caplog.records if "EVENT" in r.getMessage()]
    assert len(transitions) == 1
    assert "ping" in transitions[0].getMessage()
    assert "ok -> degraded" in transitions[0].getMessage()
    assert transitions[0].levelno == logging.WARNING
