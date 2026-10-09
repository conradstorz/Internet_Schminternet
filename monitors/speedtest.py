"""Bandwidth speedtest monitor against Cloudflare's speed endpoint.

Why Cloudflare: Ookla's legacy server list (speedtest-cli) returned only
far-away servers for this location, so every reading measured a
transoceanic path. speed.cloudflare.com terminates at the nearest Cloudflare
edge.

Why concurrent streams: a single TCP window cannot fill a fast link. From
the deployment host one stream measured 146 Mbps and four measured 383 Mbps.
`streams` is held constant across intensity rungs so readings stay
comparable; only the byte sizes and the cadence change (see
monitors/speedtest_policy.py and the `adaptive` config block).

The whole measurement is async on httpx — no thread executor is needed.
Cloudflare refuses __down requests above 50 MB (HTTP 403).
"""

from __future__ import annotations

import asyncio
import statistics
import time
from datetime import datetime, timezone

import httpx

from monitors.base import MonitorResult, Status

BASE_URL = "https://speed.cloudflare.com"
_DOWN = f"{BASE_URL}/__down"
_UP = f"{BASE_URL}/__up"
_LATENCY_SAMPLES = 5


def _determine_status(value_mbps: float, expected_mbps: float, degraded_ratio: float) -> Status:
    if expected_mbps <= 0:
        return "ok"
    if value_mbps < 0:
        return "down"
    if value_mbps < expected_mbps * degraded_ratio:
        return "degraded"
    return "ok"


def _mbps(total_bytes: int, elapsed_s: float) -> float:
    if elapsed_s <= 0:
        return 0.0
    return total_bytes * 8 / elapsed_s / 1_000_000


def _run_label(params: dict, streams: int) -> str:
    dl = params["download_bytes"] // 1_000_000
    ul = params["upload_bytes"] // 1_000_000
    return f"level={params['level']} {params['name']} {streams}x{dl}MB/{streams}x{ul}MB"


async def _measure_latency(client: httpx.AsyncClient) -> float:
    """Median round-trip of small requests, in milliseconds."""
    samples: list[float] = []
    for _ in range(_LATENCY_SAMPLES):
        start = time.perf_counter()
        resp = await client.get(_DOWN, params={"bytes": 0})
        resp.raise_for_status()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


async def _fetch(client: httpx.AsyncClient, nbytes: int) -> int:
    """Stream one download and discard the body; return bytes received."""
    received = 0
    async with client.stream("GET", _DOWN, params={"bytes": nbytes}) as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes():
            received += len(chunk)
    return received


async def _measure_download(client: httpx.AsyncClient, streams: int, nbytes: int) -> float:
    start = time.perf_counter()
    sizes = await asyncio.gather(*(_fetch(client, nbytes) for _ in range(streams)))
    return _mbps(sum(sizes), time.perf_counter() - start)


async def _push(client: httpx.AsyncClient, payload: bytes) -> int:
    resp = await client.post(_UP, content=payload,
                             headers={"Content-Type": "application/octet-stream"})
    resp.raise_for_status()
    return len(payload)


async def _measure_upload(client: httpx.AsyncClient, streams: int, nbytes: int) -> float:
    payload = b"\0" * nbytes
    start = time.perf_counter()
    sizes = await asyncio.gather(*(_push(client, payload) for _ in range(streams)))
    return _mbps(sum(sizes), time.perf_counter() - start)


async def run(
    config: dict,
    params: dict,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[MonitorResult]:
    """Run one measurement at the intensity described by `params`.

    `params` is one rung of monitors.speedtest.adaptive.ladder plus a
    `level` key (its index). `transport` is a test seam for
    httpx.MockTransport.
    """
    cfg = config.get("monitors", {}).get("speedtest", {})
    thresholds = cfg.get("thresholds", {})
    degraded_ratio = thresholds.get("degraded_ratio", 0.5)
    expected_dl = cfg.get("expected_download_mbps", 0)
    expected_ul = cfg.get("expected_upload_mbps", 0)
    streams = int(cfg.get("streams", 4))
    timeout = float(cfg.get("timeout_seconds", 60))
    label = _run_label(params, streams)

    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            ping = await _measure_latency(client)
            download = await _measure_download(client, streams, int(params["download_bytes"]))
            upload = await _measure_upload(client, streams, int(params["upload_bytes"]))
        ts = datetime.now(timezone.utc).isoformat()
        return [
            MonitorResult(
                monitor="speedtest", target="cloudflare", timestamp=ts,
                metric="download_mbps", value=round(download, 2),
                status=_determine_status(download, expected_dl, degraded_ratio),
                message=label,
            ),
            MonitorResult(
                monitor="speedtest", target="cloudflare", timestamp=ts,
                metric="upload_mbps", value=round(upload, 2),
                status=_determine_status(upload, expected_ul, degraded_ratio),
                message=label,
            ),
            MonitorResult(
                monitor="speedtest", target="cloudflare", timestamp=ts,
                metric="ping_ms", value=round(ping, 2), status="ok",
                message=label,
            ),
        ]
    except Exception as exc:
        ts = datetime.now(timezone.utc).isoformat()
        return [
            MonitorResult(
                monitor="speedtest", target="cloudflare", timestamp=ts,
                metric="download_mbps", value=-1.0, status="down",
                message=str(exc),
            )
        ]
