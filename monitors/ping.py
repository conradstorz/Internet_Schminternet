"""Ping / latency / packet-loss monitor.

Uses the OS ``ping`` binary (no root required — it has the setuid bit set on
Raspberry Pi OS). Handles both Linux and Windows output formats so the service
can be developed on either platform.
"""

from __future__ import annotations

import asyncio
import platform
import re
import subprocess
from datetime import datetime, timezone
from typing import Optional

from monitors.base import MonitorResult, Status


def _determine_status(
    latency_ms: Optional[float],
    loss_pct: float,
    thresholds: dict,
) -> Status:
    degraded_ms = thresholds.get("degraded_ms", 100)
    down_ms = thresholds.get("down_ms", 500)
    loss_degraded = thresholds.get("loss_degraded_pct", 10)
    loss_down = thresholds.get("loss_down_pct", 50)

    if latency_ms is None or loss_pct >= loss_down:
        return "down"
    if latency_ms >= down_ms:
        return "down"
    if loss_pct >= loss_degraded or latency_ms >= degraded_ms:
        return "degraded"
    return "ok"


def _ping_sync(target: str, count: int) -> tuple[Optional[float], float]:
    """Run system ping and return (avg_rtt_ms, loss_pct).  Thread-safe (no event loop)."""
    is_windows = platform.system() == "Windows"
    cmd = (
        ["ping", "-n", str(count), target]
        if is_windows
        else ["ping", "-c", str(count), "-W", "2", target]
    )
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=count * 3 + 5,
        )
        output = result.stdout

        if is_windows:
            loss_match = re.search(r"\((\d+)%\s*loss\)", output)
            loss_pct = float(loss_match.group(1)) if loss_match else 100.0
            rtt_match = re.search(r"Average\s*=\s*(\d+)ms", output)
            latency_ms = float(rtt_match.group(1)) if rtt_match else None
        else:
            loss_match = re.search(r"(\d+(?:\.\d+)?)\% packet loss", output)
            loss_pct = float(loss_match.group(1)) if loss_match else 100.0
            rtt_match = re.search(
                r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/", output
            )
            latency_ms = float(rtt_match.group(1)) if rtt_match else None

        return latency_ms, loss_pct
    except (subprocess.TimeoutExpired, OSError):
        return None, 100.0


async def run(config: dict) -> list[MonitorResult]:
    cfg = config.get("monitors", {}).get("ping", {})
    targets = cfg.get("targets", ["1.1.1.1", "wikipedia.org", "kernel.org"])
    count = cfg.get("count", 5)
    thresholds = cfg.get("thresholds", {})

    loop = asyncio.get_running_loop()

    async def _check(target: str) -> list[MonitorResult]:
        latency_ms, loss_pct = await loop.run_in_executor(
            None, _ping_sync, target, count
        )
        ts = datetime.now(timezone.utc).isoformat()
        status = _determine_status(latency_ms, loss_pct, thresholds)
        results: list[MonitorResult] = []
        if latency_ms is not None:
            results.append(
                MonitorResult(
                    monitor="ping",
                    target=target,
                    timestamp=ts,
                    metric="latency_ms",
                    value=round(latency_ms, 2),
                    status=status,
                )
            )
        results.append(
            MonitorResult(
                monitor="ping",
                target=target,
                timestamp=ts,
                metric="packet_loss_pct",
                value=loss_pct,
                status=status,
            )
        )
        return results

    nested = await asyncio.gather(*[_check(t) for t in targets])
    return [r for sublist in nested for r in sublist]
