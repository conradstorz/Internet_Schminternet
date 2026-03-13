"""HTTP reachability monitor.

Checks each configured URL with an async httpx client, measuring response time
and validating HTTP status codes.  All configured URLs are checked concurrently
within each run.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx

from monitors.base import MonitorResult, Status


def _determine_status(
    response_ms: float, status_code: int, thresholds: dict
) -> Status:
    degraded_ms = thresholds.get("degraded_ms", 1000)
    if response_ms < 0 or status_code >= 500:
        return "down"
    if status_code >= 400:
        return "down"
    if response_ms >= degraded_ms:
        return "degraded"
    return "ok"


async def _check_url(
    client: httpx.AsyncClient, url: str, thresholds: dict
) -> MonitorResult:
    ts = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    try:
        response = await client.get(url)
        elapsed_ms = (time.perf_counter() - start) * 1000
        status = _determine_status(elapsed_ms, response.status_code, thresholds)
        return MonitorResult(
            monitor="http",
            target=url,
            timestamp=ts,
            metric="response_ms",
            value=round(elapsed_ms, 2),
            status=status,
            message=str(response.status_code),
        )
    except httpx.RequestError as exc:
        return MonitorResult(
            monitor="http",
            target=url,
            timestamp=ts,
            metric="response_ms",
            value=-1.0,
            status="down",
            message=str(exc),
        )


async def run(config: dict) -> list[MonitorResult]:
    cfg = config.get("monitors", {}).get("http", {})
    targets = cfg.get("targets", [{"url": "https://www.google.com"}])
    timeout_s = cfg.get("timeout_seconds", 10)
    thresholds = cfg.get("thresholds", {})

    async with httpx.AsyncClient(
        timeout=timeout_s,
        follow_redirects=True,
    ) as client:
        tasks = [_check_url(client, t["url"], thresholds) for t in targets]
        results = await asyncio.gather(*tasks)

    return list(results)
