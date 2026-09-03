"""DNS resolution timing monitor.

Uses dnspython's async resolver so we can query specific nameservers independently
(router, ISP, Google, Cloudflare) rather than just the system resolver.  This lets
us distinguish "my local DNS is down" from "upstream DNS is slow".
"""

from __future__ import annotations

import asyncio
import random
import string
import time
from datetime import datetime, timezone

import dns.asyncresolver
import dns.resolver

from monitors.base import MonitorResult, Status

_LABEL_CHARS = string.ascii_lowercase + string.digits


def _random_label(length: int = 10) -> str:
    """Generate a short, DNS-legal random label (lowercase letters + digits).

    Used to defeat resolver caching: a fresh random label is never in cache,
    so the resolver must do a real round trip instead of answering from a
    cached ``google.com`` lookup.
    """
    return "".join(random.choices(_LABEL_CHARS, k=length))


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
    servers = cfg.get("servers", ["8.8.8.8", "9.9.9.9"])
    test_domain = cfg.get("test_domain", "google.com")
    randomize_query = cfg.get("randomize_query", True)
    thresholds = cfg.get("thresholds", {})

    async def _check(server: str) -> MonitorResult:
        ts = datetime.now(timezone.utc).isoformat()
        query_name = (
            f"{_random_label()}.{test_domain}" if randomize_query else test_domain
        )
        try:
            resolver = dns.asyncresolver.Resolver(configure=False)
            resolver.nameservers = [server]
            resolver.timeout = 5.0
            resolver.lifetime = 5.0

            start = time.perf_counter()
            try:
                await resolver.resolve(query_name, "A")
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
                # The resolver answered — there's just no such name (or no A
                # record). That's a genuine, successful round trip for timing
                # purposes: this monitor measures resolution latency, not
                # whether the name exists.
                pass
            elapsed_ms = (time.perf_counter() - start) * 1000

            status = _determine_status(elapsed_ms, thresholds)
            return MonitorResult(
                monitor="dns",
                target=server,
                timestamp=ts,
                metric="resolution_ms",
                value=round(elapsed_ms, 2),
                status=status,
                message=f"queried {query_name}",
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
