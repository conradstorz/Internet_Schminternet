# Cloudflare Speedtest with Adaptive Intensity — Design

**Date:** 2026-10-09
**Status:** Approved, pending implementation

## Problem

The speedtest monitor uses the `speedtest-cli` library (Ookla's legacy
API). On 2026-10-09 the server list it receives for this location
contained ten servers, all in southern Brazil, 8,400 km away at ~174 ms
RTT. Every reading for weeks has measured a transoceanic path, not the
Spectrum link: erratic 8–470 Mbps while the ISP delivers ~380 Mbps
(verified from the host with four parallel HTTP streams). The ISP is
right and the monitor is wrong.

A second problem is cost. A full-size test every 30 minutes regardless of
conditions is both too coarse to catch a short outage and too expensive to
run more often.

## Goals

1. Measure the link against Cloudflare's speed endpoint
   (`speed.cloudflare.com/__down`, `/__up`), which terminates at the
   nearest Cloudflare edge (CLE for this host, ~25 ms RTT).
2. Replace Ookla outright. `speedtest-cli` leaves the dependency list.
3. Throttle cadence and size by situational awareness: good results make
   testing lighter and rarer, poor results make it heavier and more
   frequent until the problem clears.

Non-goals: comparing providers, persisting the intensity level across
restarts, partial credit for a run in which some streams *fail* (a phase that
runs out of time is handled separately — see Errors).

## Measurement (`monitors/speedtest.py`)

Fully async on `httpx.AsyncClient`. No thread executor; the old
"CPU-intensive, offload to a thread" rationale was specific to
`speedtest-cli`.

- **Latency** — median of 5 round-trips to `__down?bytes=0`. Up to two
  probes may fail (error or non-2xx) and are simply dropped from the
  median; fewer than three survivors fails the run.
- **Download** — `streams` concurrent GETs of `__down?bytes=<download_bytes>`,
  bodies streamed and discarded. Mbps = total bytes × 8 / wall time from
  first request start to last stream end / 1e6.
- **Upload** — `streams` concurrent POSTs of `upload_bytes` zero bytes
  each to `__up`, same arithmetic.
- **Concurrency is the point.** From this host one stream reached 146 Mbps
  and four streams 383 Mbps; a single TCP window cannot fill the link.
  Every intensity level uses the same stream count so that readings are
  comparable across levels — streams are free, bytes are not.
- Cloudflare serves at most 50 MB per `__down` request; sizes above that
  return 403. The ladder below stays well under it.
- Each transfer phase (download, upload) is retried once before it is
  allowed to fail the run, so one rejected stream is not an outage.

Results keep the current shape: three `MonitorResult` rows, `target` is
now `"cloudflare"`, metrics `download_mbps`, `upload_mbps`, `ping_ms`.
`_determine_status()` is unchanged. Each row's `message` records the
intensity level and sizes that produced it, e.g. `level=2 4x25MB/4x10MB`,
so logs and the dashboard show which tier a number came from.

**Errors.** An exception that survives the phase retry fails the run: one
`download_mbps` row with `value = -1.0`, `status = "down"`, and the
exception text, exactly as today. `timeout_seconds` is a per-phase *total*
deadline — the download phase gets that long in whole, and the upload phase
again — rather than a per-request bound. Reaching the deadline does **not**
fail the run: the in-flight streams are cancelled and the phase reports the
bytes actually transferred divided by the elapsed time, so a link too slow
to finish the configured transfer reads as slow (and the policy's verdict
turns "poor", escalating the ladder) instead of as down. The pool-warming
requests sit outside the deadline, so handshakes neither land in the timed
window nor consume it.

## Intensity ladder

One scheduler job, one in-memory `level`, starting at level 1 on every
start. The ladder is a list in config so intervals, sizes, or the number
of levels can be retuned without code:

| level | name | interval | per-stream down / up | data per run |
|---|---|---|---|---|
| 0 | calm | 30 min | 10 MB / 4 MB | 56 MB |
| 1 | watch (start) | 5 min | 10 MB / 4 MB | 56 MB |
| 2 | alert | 2 min | 25 MB / 10 MB | 140 MB |
| 3 | investigate | 1 min | 25 MB / 10 MB | 140 MB |

Held all day these cost roughly 2.7, 16, 100 and 200 GB respectively.
Level 3 is meant to be transient; it persists only while the link stays
poor, which is the information the user wants during an incident.

## Verdict and transitions (`monitors/speedtest_policy.py`)

Pure functions, no I/O, tested directly.

`verdict(download_mbps, expected_mbps, degraded_ratio, history, avg_ratio,
min_samples) -> "good" | "poor"`:

- A failed run (`download_mbps < 0`) is poor.
- Below `expected_mbps × degraded_ratio` is poor (the existing degraded
  threshold; skipped when `expected_mbps <= 0`, as `_determine_status`
  already does).
- Below `avg_ratio × mean(history)` is poor, where `history` is the list
  of successful download readings from the last 24 h. This rule applies
  only when `len(history) >= min_samples`; with fewer samples only the
  fixed threshold applies.
- Otherwise good.

`next_level(level, verdict, good_streak, calm_after, max_level) ->
(level, good_streak)`:

- Poor: level rises by one (capped at `max_level`), streak resets to 0.
- Good: streak increments; when it reaches `calm_after` the level drops
  by one (floored at 0) and the streak resets to 0.

Defaults: `avg_ratio = 0.8`, `min_samples = 3`, `calm_after = 2`.

## Wiring (`main.py`, `storage/db.py`)

- `storage/db.py` gains `recent_values(monitor, metric, hours) ->
  list[float]`, returning successful (`value >= 0`) readings for the
  rolling mean. If an equivalent query already exists it is reused.
- The speedtest job closure owns `level` and `good_streak`. Per run:
  build the run parameters from `ladder[level]`, call
  `speedtest_monitor.run(config, level_params)`, hand results to
  `run_monitor()` as today, compute the verdict and next level, and if the
  level changed call `scheduler.reschedule_job("speedtest", trigger=
  "interval", seconds=ladder[level]["interval_seconds"])` and log the
  transition at INFO.
- Startup behaviour is unchanged: the speedtest still does not fire
  immediately; the first run is one level-1 interval after start.

## Configuration

`DEFAULT_CONFIG["monitors"]["speedtest"]` replaces `interval_seconds` with:

```yaml
speedtest:
  expected_download_mbps: 0
  expected_upload_mbps: 0
  thresholds: { degraded_ratio: 0.5 }
  streams: 4
  timeout_seconds: 60
  adaptive:
    avg_ratio: 0.8
    min_samples: 3
    calm_after: 2
    ladder:
      - { name: calm,        interval_seconds: 1800, download_bytes: 10000000, upload_bytes: 4000000 }
      - { name: watch,       interval_seconds: 300,  download_bytes: 10000000, upload_bytes: 4000000 }
      - { name: alert,       interval_seconds: 120,  download_bytes: 25000000, upload_bytes: 10000000 }
      - { name: investigate, interval_seconds: 60,   download_bytes: 25000000, upload_bytes: 10000000 }
```

`degraded_alert_minutes` / `degraded_alert_cycles` stay as they are. A
user `config.yaml` that still sets `interval_seconds` is ignored for
speedtest (the ladder governs cadence); `config.example.yaml` documents
the new block and the per-level data cost.

## Dependency removal

`speedtest-cli` is removed from `pyproject.toml`, `requirements.txt`, and
`uv.lock` (`uv sync`). CLAUDE.md's reference to `speedtest-cli` as
executor-bound work is updated; the LED weight comment that speedtest
"only runs every 30 min" now says "runs on an adaptive interval".

## Testing

- `tests/test_thresholds.py` — unchanged; `_determine_status` is kept.
- `tests/test_speedtest_policy.py` — `verdict` (fixed threshold, rolling
  mean, min-samples gate, failed run) and `next_level` (rise, cap, streak
  to calm, floor).
- `tests/test_speedtest_monitor.py` — `httpx.MockTransport` handlers that
  count requests and bytes: download aggregates bytes across `streams`
  requests of the configured size; upload POSTs the configured size per
  stream; a raising handler yields the single `down` sentinel row;
  `ping_ms` is the median of the latency probes; `message` carries the
  level label.
- Scheduler wiring is exercised by a test that drives the job closure with
  a stubbed `speedtest_monitor.run` and a fake scheduler, asserting
  `reschedule_job` is called with the ladder interval on a level change
  and not called otherwise.

## Deployment

Rebuild the image and restart the `schminternet` container on hpz440.
Verify with `GET /api/metrics/speedtest` that `target` is `cloudflare` and
that the first readings sit near the ISP's figure.
