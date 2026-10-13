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

from monitors import gateway
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


def _fmt_pct(value: float) -> str:
    return f"{int(value)}%" if value == int(value) else f"{value:.1f}%"


def _fault_location(
    gateway: Optional[tuple[str, Status, float]],
    internet_statuses: list[Status],
    target_loss_pct: float,
) -> str:
    """One-line hint at *where* a fault sits, for a non-ok internet-target row.

    ``gateway`` is ``(address, status, loss_pct)`` for the LAN gateway leg, or
    None when that leg is off. ``target_loss_pct`` is the row's own target loss,
    used for the short message when there is no gateway leg.
    """
    if gateway is None:
        return f"{_fmt_pct(target_loss_pct)} loss"
    addr, gw_status, gw_loss = gateway
    if gw_status != "ok":
        return f"LAN gateway {addr} {_fmt_pct(gw_loss)} loss — fault is on the local network or router"
    if any(s != "ok" for s in internet_statuses):
        return f"LAN gateway {addr} ok ({_fmt_pct(gw_loss)} loss) — fault is beyond your router (modem/ISP)"
    return ""


def _rows(target: str, ts: str, latency_ms: Optional[float], loss_pct: float,
          status: Status, message: str = "") -> list[MonitorResult]:
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
                message=message,
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
            message=message,
        )
    )
    return results


async def run(config: dict) -> list[MonitorResult]:
    cfg = config.get("monitors", {}).get("ping", {})
    targets = cfg.get("targets", ["1.1.1.1", "wikipedia.org", "kernel.org"])
    count = cfg.get("count", 5)
    thresholds = cfg.get("thresholds", {})

    # LAN gateway leg: auto-detected once (Linux), explicit, or off. Resolved
    # through the module attribute so tests can stub it.
    gateway_addr = await gateway.resolve(config)

    loop = asyncio.get_running_loop()

    async def _measure(target: str) -> tuple[str, Optional[float], float, Status, str]:
        latency_ms, loss_pct = await loop.run_in_executor(
            None, _ping_sync, target, count
        )
        ts = datetime.now(timezone.utc).isoformat()
        return target, latency_ms, loss_pct, _determine_status(latency_ms, loss_pct, thresholds), ts

    # The gateway rides in the same gather as the internet targets, so it
    # adds no wall-clock to the poll.
    probes = list(targets) + ([gateway_addr] if gateway_addr else [])
    measured = await asyncio.gather(*[_measure(t) for t in probes])

    internet = measured[: len(targets)]
    gw_reading = None
    results: list[MonitorResult] = []
    if gateway_addr:
        _addr, gw_latency, gw_loss, gw_status, gw_ts = measured[-1]
        gw_reading = (gateway_addr, gw_status, gw_loss)

    internet_statuses = [m[3] for m in internet]
    for target, latency_ms, loss_pct, status, ts in internet:
        message = (
            _fault_location(gw_reading, internet_statuses, loss_pct) if status != "ok" else ""
        )
        results.extend(_rows(target, ts, latency_ms, loss_pct, status, message))

    if gateway_addr:
        results.extend(_rows(f"gateway {gateway_addr}", gw_ts, gw_latency, gw_loss, gw_status))

    return results
