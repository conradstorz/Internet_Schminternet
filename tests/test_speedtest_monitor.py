"""Tests for monitors/speedtest.py — the Cloudflare measurement — using
httpx.MockTransport so no network is touched. The handler records every
request so tests can assert stream counts and byte sizes."""

from __future__ import annotations

import httpx
import pytest

from monitors import speedtest

PARAMS = {"level": 2, "name": "alert", "interval_seconds": 120,
          "download_bytes": 1_000, "upload_bytes": 500}


def _config(streams: int = 3, expected_dl: float = 0, expected_ul: float = 0) -> dict:
    return {"monitors": {"speedtest": {
        "streams": streams,
        "timeout_seconds": 5,
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
    assert all(r.monitor == "speedtest" and r.target == "cloudflare" for r in results)
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


def test_run_label_formats_megabytes():
    params = {"level": 1, "name": "watch", "download_bytes": 10_000_000, "upload_bytes": 4_000_000}
    assert speedtest._run_label(params, 4) == "level=1 watch 4x10MB/4x4MB"


class TestMbps:
    def test_bytes_and_seconds_to_megabits(self):
        assert speedtest._mbps(1_000_000, 1.0) == pytest.approx(8.0)

    def test_zero_elapsed_does_not_divide_by_zero(self):
        assert speedtest._mbps(1_000_000, 0.0) == 0.0
