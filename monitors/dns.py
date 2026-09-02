"""DNS resolution timing monitor.

Uses dnspython's async resolver so we can query specific nameservers independently
(router, ISP, Google, Cloudflare) rather than just the system resolver.  This lets
us distinguish "my local DNS is down" from "upstream DNS is slow".
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import dns.asyncresolver

from monitors.base import MonitorResult, Status


def _determine_status(resolution_ms: float, thresholds: dict) -> Status:
    degraded_ms = thresholds.get("degraded_ms", 200)
    down_ms = thresholds.get("down_ms", 1000)
    if resolution_ms < 0:
        return "down"
    if resolution_ms >= down_ms:
        return "down"
    if resolution_ms >= degraded_ms:
        return "degraded"
    return "ok"


async def run(config: dict) -> list[MonitorResult]:
    cfg = config.get("monitors", {}).get("dns", {})
    servers = cfg.get("servers", ["8.8.8.8", "1.1.1.1"])
    test_domain = cfg.get("test_domain", "google.com")
    thresholds = cfg.get("thresholds", {})

    async def _check(server: str) -> MonitorResult:
        ts = datetime.now(timezone.utc).isoformat()
        try:
            resolver = dns.asyncresolver.Resolver(configure=False)
            resolver.nameservers = [server]
            resolver.timeout = 5.0
            resolver.lifetime = 5.0

            start = time.perf_counter()
            await resolver.resolve(test_domain, "A")
            elapsed_ms = (time.perf_counter() - start) * 1000

            status = _determine_status(elapsed_ms, thresholds)
            return MonitorResult(
                monitor="dns",
                target=server,
                timestamp=ts,
                metric="resolution_ms",
                value=round(elapsed_ms, 2),
                status=status,
            )
        except Exception as exc:
            return MonitorResult(
                monitor="dns",
                target=server,
                timestamp=ts,
                metric="resolution_ms",
                value=-1.0,
                status="down",
                message=str(exc),
            )

    return list(await asyncio.gather(*[_check(s) for s in servers]))
