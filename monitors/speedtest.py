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

`timeout_seconds` is a per-phase *total* deadline, not just a per-request
one: the download phase and the upload phase each get that long in whole.
Hitting the deadline does not fail the run as long as some bytes moved — the
in-flight streams are cancelled and the bytes that did move are divided by
the elapsed time, so a link too slow to finish reads as slow rather than as
down. A deadline that fires with zero bytes moved is a failure, not a 0.0
Mbps reading, and propagates out of the phase like any other exception
(there is no second attempt on this path — opening a second deadline window
after the first already expired would double the worst-case time a dead
link ties up the phase). The pool-warming
requests sit outside the deadline on purpose, so handshakes are neither
inside the timed window nor eating into it. Each transfer phase is retried
once before it is allowed to fail the run, and the latency probe tolerates
up to two failures out of five.

The whole measurement is async on httpx — no thread executor is needed.
Cloudflare refuses __down requests above 50 MB (HTTP 403).
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import httpx

from monitors.base import MonitorResult, Status

BASE_URL = "https://speed.cloudflare.com"
TARGET = "cloudflare"
_DOWN = f"{BASE_URL}/__down"
_UP = f"{BASE_URL}/__up"
_LATENCY_SAMPLES = 5
_LATENCY_MIN_OK = 3


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
    """Median round-trip of small requests, in milliseconds.

    A probe that errors or answers non-2xx is dropped rather than failing the
    whole measurement; the median of the survivors is still representative as
    long as most of them came back.
    """
    samples: list[float] = []
    for _ in range(_LATENCY_SAMPLES):
        start = time.perf_counter()
        try:
            resp = await client.get(_DOWN, params={"bytes": 0})
            resp.raise_for_status()
        except httpx.HTTPError:
            continue
        samples.append((time.perf_counter() - start) * 1000)
    if len(samples) < _LATENCY_MIN_OK:
        raise RuntimeError(
            f"latency: only {len(samples)}/{_LATENCY_SAMPLES} probes succeeded"
        )
    return statistics.median(samples)


async def _gather_streams(coros) -> list[int]:
    """Await every stream, then surface the first failure.

    asyncio.gather's default would hand back the first exception while the
    sibling streams were still running, and those stragglers would then bleed
    bytes into the retry's counter and raise "task exception was never
    retrieved" at collection time.
    """
    outcomes = await asyncio.gather(*coros, return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
    return list(outcomes)


async def _run_phase(
    attempt: Callable[[], Awaitable[list[int]]],
    moved: list[int],
    timeout_seconds: float,
    phase: str,
) -> tuple[int, float]:
    """Run one transfer phase under a total deadline; return (bytes, seconds).

    One retry is allowed before a failure is propagated. On the deadline the
    streams are cancelled and whatever `moved` has collected so far is
    reported, so the caller still gets a throughput number — unless nothing
    at all moved, in which case the whole deadline window was spent with no
    evidence of a working link, and that is a failure rather than a 0.0
    reading.
    """
    start = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_seconds):
            try:
                total = sum(await attempt())
            except Exception:
                moved.clear()
                total = sum(await attempt())
            return total, time.perf_counter() - start
    except TimeoutError:
        total = sum(moved)
        if total == 0:
            raise RuntimeError(
                f"{phase}: no bytes transferred within {timeout_seconds}s"
            ) from None
        return total, time.perf_counter() - start


async def _fetch(client: httpx.AsyncClient, nbytes: int, moved: list[int]) -> int:
    """Stream one download and discard the body; return bytes received.

    Every chunk is also appended to `moved` so the total survives the
    cancellation that a phase deadline brings.
    """
    received = 0
    async with client.stream("GET", _DOWN, params={"bytes": nbytes}) as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes():
            received += len(chunk)
            moved.append(len(chunk))
    return received


async def _measure_download(
    client: httpx.AsyncClient, streams: int, nbytes: int, timeout_seconds: float
) -> float:
    # Open `streams` connections before the clock starts so handshakes are
    # neither inside the timed window nor spending the phase deadline; upload
    # then reuses the same warm pool, keeping the two numbers on the same
    # basis.
    await _gather_streams([_fetch(client, 0, []) for _ in range(streams)])
    moved: list[int] = []

    def attempt() -> Awaitable[list[int]]:
        return _gather_streams([_fetch(client, nbytes, moved) for _ in range(streams)])

    total, elapsed = await _run_phase(attempt, moved, timeout_seconds, "download")
    return _mbps(total, elapsed)


async def _push(client: httpx.AsyncClient, payload: bytes, moved: list[int]) -> int:
    resp = await client.post(_UP, content=payload,
                             headers={"Content-Type": "application/octet-stream"})
    resp.raise_for_status()
    moved.append(len(payload))
    return len(payload)


async def _measure_upload(
    client: httpx.AsyncClient, streams: int, nbytes: int, timeout_seconds: float
) -> float:
    payload = b"\0" * nbytes
    moved: list[int] = []

    def attempt() -> Awaitable[list[int]]:
        return _gather_streams([_push(client, payload, moved) for _ in range(streams)])

    total, elapsed = await _run_phase(attempt, moved, timeout_seconds, "upload")
    return _mbps(total, elapsed)


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
        limits = httpx.Limits(max_connections=streams, max_keepalive_connections=streams)
        async with httpx.AsyncClient(timeout=timeout, limits=limits,
                                     transport=transport) as client:
            ping = await _measure_latency(client)
            download = await _measure_download(
                client, streams, int(params["download_bytes"]), timeout
            )
            upload = await _measure_upload(
                client, streams, int(params["upload_bytes"]), timeout
            )
        ts = datetime.now(timezone.utc).isoformat()
        return [
            MonitorResult(
                monitor="speedtest", target=TARGET, timestamp=ts,
                metric="download_mbps", value=round(download, 2),
                status=_determine_status(download, expected_dl, degraded_ratio),
                message=label,
            ),
            MonitorResult(
                monitor="speedtest", target=TARGET, timestamp=ts,
                metric="upload_mbps", value=round(upload, 2),
                status=_determine_status(upload, expected_ul, degraded_ratio),
                message=label,
            ),
            MonitorResult(
                monitor="speedtest", target=TARGET, timestamp=ts,
                metric="ping_ms", value=round(ping, 2), status="ok",
                message=label,
            ),
        ]
    except Exception as exc:
        ts = datetime.now(timezone.utc).isoformat()
        return [
            MonitorResult(
                monitor="speedtest", target=TARGET, timestamp=ts,
                metric="download_mbps", value=-1.0, status="down",
                message=str(exc),
            )
        ]
