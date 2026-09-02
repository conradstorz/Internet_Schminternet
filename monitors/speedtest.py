"""Bandwidth speedtest monitor.

speedtest-cli is synchronous and CPU-intensive for the duration of the test
(~20-40 s on a Pi 3B).  The blocking call is offloaded to a thread executor so
the asyncio event loop is not stalled.

Keep the interval long (≥1800 s) on a Pi 3B — a full test saturates the CPU.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from monitors.base import MonitorResult, Status


def _run_speedtest_sync() -> tuple[float, float, float]:
    """Blocking speedtest — must be called from a thread, not the event loop."""
    import speedtest as st  # imported here so missing package only errors at runtime

    s = st.Speedtest(secure=True)
    s.get_best_server()
    download_mbps = s.download() / 1_000_000   # bits/s → Mbps
    upload_mbps = s.upload() / 1_000_000
    ping_ms = s.results.ping
    return download_mbps, upload_mbps, ping_ms


def _determine_status(value_mbps: float, expected_mbps: float, degraded_ratio: float) -> Status:
    if expected_mbps <= 0:
        return "ok"
    if value_mbps < 0:
        return "down"
    if value_mbps < expected_mbps * degraded_ratio:
        return "degraded"
    return "ok"


async def run(config: dict) -> list[MonitorResult]:
    cfg = config.get("monitors", {}).get("speedtest", {})
    thresholds = cfg.get("thresholds", {})
    degraded_ratio = thresholds.get("degraded_ratio", 0.5)
    expected_dl = cfg.get("expected_download_mbps", 0)
    expected_ul = cfg.get("expected_upload_mbps", 0)

    try:
        loop = asyncio.get_running_loop()
        download, upload, ping = await loop.run_in_executor(None, _run_speedtest_sync)
        ts = datetime.now(timezone.utc).isoformat()

        return [
            MonitorResult(
                monitor="speedtest",
                target="ookla",
                timestamp=ts,
                metric="download_mbps",
                value=round(download, 2),
                status=_determine_status(download, expected_dl, degraded_ratio),
            ),
            MonitorResult(
                monitor="speedtest",
                target="ookla",
                timestamp=ts,
                metric="upload_mbps",
                value=round(upload, 2),
                status=_determine_status(upload, expected_ul, degraded_ratio),
            ),
            MonitorResult(
                monitor="speedtest",
                target="ookla",
                timestamp=ts,
                metric="ping_ms",
                value=round(ping, 2),
                status="ok",
            ),
        ]
    except Exception as exc:
        ts = datetime.now(timezone.utc).isoformat()
        return [
            MonitorResult(
                monitor="speedtest",
                target="ookla",
                timestamp=ts,
                metric="download_mbps",
                value=-1.0,
                status="down",
                message=str(exc),
            )
        ]
