"""Tests for monitors/speedtest.py — the Cloudflare measurement — using
httpx.MockTransport so no network is touched. The handler records every
request so tests can assert stream counts and byte sizes."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from monitors import speedtest

PARAMS = {"level": 2, "name": "alert", "interval_seconds": 120,
          "download_bytes": 1_000, "upload_bytes": 500}


def _config(streams: int = 3, expected_dl: float = 0, expected_ul: float = 0,
            timeout_seconds: float = 5) -> dict:
    return {"monitors": {"speedtest": {
        "streams": streams,
        "timeout_seconds": timeout_seconds,
        "expected_download_mbps": expected_dl,
        "expected_upload_mbps": expected_ul,
        "thresholds": {"degraded_ratio": 0.5},
    }}}


class Recorder:
    """MockTransport handler that serves __down/__up and logs each call."""

    def __init__(self, fail_path: str | None = None):
        self.downloads: list[int] = []   # bytes requested per GET
        self.uploads: list[int] = []     # bytes received per POST
        self.fail_path = fail_path

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail_path and request.url.path == self.fail_path:
            return httpx.Response(500, text="boom")
        if request.url.path == "/__down":
            n = int(request.url.params.get("bytes", "0"))
            self.downloads.append(n)
            return httpx.Response(200, content=b"0" * n)
        if request.url.path == "/__up":
            self.uploads.append(len(request.content))
            return httpx.Response(200, text="ok")
        return httpx.Response(404)


class FlakyZeroByte:
    """Fails the first `fail_n` zero-byte GETs, serves everything else.

    The 5 latency probes are the first zero-byte requests the monitor makes
    (the pool-warming ones come after), so `fail_n` selects how many probes
    fail.
    """

    def __init__(self, fail_n: int):
        self.fail_n = fail_n
        self.zero_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/__down":
            n = int(request.url.params.get("bytes", "0"))
            if n == 0:
                self.zero_calls += 1
                if self.zero_calls <= self.fail_n:
                    return httpx.Response(503, text="probe failed")
            return httpx.Response(200, content=b"0" * n)
        if request.url.path == "/__up":
            return httpx.Response(200, text="ok")
        return httpx.Response(404)


class FlakyUpload:
    """Fails the first POST only; every later one succeeds."""

    def __init__(self):
        self.posts = 0
        self.uploads: list[int] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/__down":
            n = int(request.url.params.get("bytes", "0"))
            return httpx.Response(200, content=b"0" * n)
        if request.url.path == "/__up":
            self.posts += 1
            if self.posts == 1:
                return httpx.Response(500, text="boom")
            self.uploads.append(len(request.content))
            return httpx.Response(200, text="ok")
        return httpx.Response(404)


class SlowStream(httpx.AsyncByteStream):
    """Trickles chunks with a sleep between them, so a phase deadline trips."""

    def __init__(self, chunk: bytes, count: int, delay: float):
        self._chunk, self._count, self._delay = chunk, count, delay
        self._gen = None

    def __aiter__(self):
        self._gen = self._produce()
        return self._gen

    async def _produce(self):
        for _ in range(self._count):
            await asyncio.sleep(self._delay)
            yield self._chunk

    async def aclose(self) -> None:
        if self._gen is not None:
            await self._gen.aclose()


SLOW_CHUNKS = 100
SLOW_DELAY = 0.02          # a full download stream takes 2.0 s


def slow_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/__down":
        n = int(request.url.params.get("bytes", "0"))
        if n == 0:
            return httpx.Response(200, content=b"")
        return httpx.Response(200, stream=SlowStream(b"0" * 1_000, SLOW_CHUNKS, SLOW_DELAY))
    if request.url.path == "/__up":
        return httpx.Response(200, text="ok")
    return httpx.Response(404)


def _rows(results):
    return {r.metric: r for r in results}


async def test_download_uses_configured_streams_and_size():
    rec = Recorder()
    results = await speedtest.run(_config(streams=3), PARAMS, transport=httpx.MockTransport(rec))
    rows = _rows(results)
    assert rows["download_mbps"].value > 0
    # 5 latency probes + 3 pool-warming requests ask for 0 bytes; the 3
    # download streams ask for 1_000 each.
    assert sorted(rec.downloads) == [0] * 8 + [1_000, 1_000, 1_000]


async def test_download_warmup_count_tracks_streams():
    rec = Recorder()
    await speedtest.run(_config(streams=2), PARAMS, transport=httpx.MockTransport(rec))
    # 5 latency probes + 2 pool-warming requests == 7 zero-byte downloads.
    assert rec.downloads.count(0) == 7


async def test_upload_posts_configured_size_per_stream():
    rec = Recorder()
    results = await speedtest.run(_config(streams=3), PARAMS, transport=httpx.MockTransport(rec))
    rows = _rows(results)
    assert rows["upload_mbps"].value > 0
    assert rec.uploads == [500, 500, 500]


async def test_result_shape_and_label():
    rec = Recorder()
    results = await speedtest.run(_config(streams=3), PARAMS, transport=httpx.MockTransport(rec))
    assert {r.metric for r in results} == {"download_mbps", "upload_mbps", "ping_ms"}
    assert speedtest.TARGET == "cloudflare"
    assert all(r.monitor == "speedtest" and r.target == speedtest.TARGET for r in results)
    assert all(r.message == "level=2 alert 3x0MB/3x0MB" for r in results)
    assert len({r.timestamp for r in results}) == 1
    rows = _rows(results)
    assert rows["ping_ms"].value >= 0
    assert rows["ping_ms"].status == "ok"


async def test_status_uses_thresholds():
    rec = Recorder()
    # The mock moves only a few KB, so measured Mbps is small but positive;
    # a tiny expectation keeps the threshold path exercised without flakiness.
    results = await speedtest.run(_config(streams=2, expected_dl=0.0001, expected_ul=0.0001), PARAMS,
                                  transport=httpx.MockTransport(rec))
    rows = _rows(results)
    assert rows["download_mbps"].status == "ok"
    assert rows["upload_mbps"].status == "ok"


async def test_failed_stream_yields_single_down_row():
    rec = Recorder(fail_path="/__up")
    results = await speedtest.run(_config(streams=2), PARAMS, transport=httpx.MockTransport(rec))
    assert len(results) == 1
    row = results[0]
    assert row.metric == "download_mbps"
    assert row.value == -1.0
    assert row.status == "down"
    assert "500" in row.message


async def test_latency_tolerates_two_failed_probes():
    rec = FlakyZeroByte(fail_n=2)
    results = await speedtest.run(_config(streams=2), PARAMS, transport=httpx.MockTransport(rec))
    rows = _rows(results)
    # 3 of 5 probes succeeded, which is the minimum: the run still reports.
    assert "ping_ms" in rows
    assert rows["ping_ms"].value >= 0
    assert rows["ping_ms"].status == "ok"
    assert rows["download_mbps"].status == "ok"


async def test_latency_fails_the_run_when_three_probes_fail():
    rec = FlakyZeroByte(fail_n=3)
    results = await speedtest.run(_config(streams=2), PARAMS, transport=httpx.MockTransport(rec))
    assert len(results) == 1
    row = results[0]
    assert row.metric == "download_mbps"
    assert row.value == -1.0
    assert row.status == "down"
    assert "2/5 probes succeeded" in row.message


async def test_transfer_phase_is_retried_once():
    rec = FlakyUpload()
    results = await speedtest.run(_config(streams=2), PARAMS, transport=httpx.MockTransport(rec))
    rows = _rows(results)
    assert len(results) == 3
    assert rows["upload_mbps"].status == "ok"
    assert rows["upload_mbps"].value > 0
    # Attempt 1 issues both streams (one 500s, one succeeds); the retry
    # issues both again, so 2 * streams POSTs and streams + 1 bodies stored.
    assert rec.posts == 4
    assert rec.uploads == [500, 500, 500]


async def test_download_deadline_reports_partial_throughput():
    """A link too slow to finish inside timeout_seconds must read as slow,
    not as down: the phase is cut short and the bytes that did arrive are
    divided by the elapsed time."""
    started = time.perf_counter()
    results = await speedtest.run(_config(streams=2, timeout_seconds=0.4), PARAMS,
                                  transport=httpx.MockTransport(slow_handler))
    elapsed = time.perf_counter() - started
    rows = _rows(results)
    assert len(results) == 3
    assert rows["download_mbps"].value > 0
    assert rows["download_mbps"].status != "down"
    # The full stream would take SLOW_CHUNKS * SLOW_DELAY == 2.0 s per stream.
    assert elapsed < SLOW_CHUNKS * SLOW_DELAY


def test_run_label_formats_megabytes():
    params = {"level": 1, "name": "watch", "download_bytes": 10_000_000, "upload_bytes": 4_000_000}
    assert speedtest._run_label(params, 4) == "level=1 watch 4x10MB/4x4MB"


class TestMbps:
    def test_bytes_and_seconds_to_megabits(self):
        assert speedtest._mbps(1_000_000, 1.0) == pytest.approx(8.0)

    def test_zero_elapsed_does_not_divide_by_zero(self):
        assert speedtest._mbps(1_000_000, 0.0) == 0.0
