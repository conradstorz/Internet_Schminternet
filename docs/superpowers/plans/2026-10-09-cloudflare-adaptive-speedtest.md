# Cloudflare Adaptive Speedtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Ookla `speedtest-cli` monitor with a Cloudflare-based measurement whose cadence and size adapt to recent results.

**Architecture:** `monitors/speedtest.py` becomes an async httpx client against `speed.cloudflare.com`, parameterised by a ladder entry (`download_bytes`, `upload_bytes`). A pure policy module decides good/poor and the next ladder level. A `SpeedtestJob` object in `main.py` owns the level, calls the monitor, feeds `run_monitor()`, and reschedules its own APScheduler interval when the level changes.

**Tech Stack:** Python 3.13, httpx (already a dependency), APScheduler 3.x `AsyncIOScheduler`, aiosqlite, pytest with `asyncio_mode = auto`.

Spec: `docs/superpowers/specs/2026-10-09-cloudflare-adaptive-speedtest-design.md`

## Global Constraints

- Python `>=3.13`; dependencies managed with `uv` only (`uv sync`, `uv run pytest`, `uv remove`). Never `pip install`.
- Never chain shell commands with `&&` or `;`. One command per Bash call.
- All monitors are `async def run(config: dict, ...) -> list[MonitorResult]`; `value = -1.0` is the error sentinel; timestamps are `datetime.now(timezone.utc).isoformat()`.
- `main.py` must stay import-safe: no side effects outside `main()`.
- `pytest.ini` sets `asyncio_mode = auto`: async tests need no decorator.
- Cloudflare's `__down` endpoint refuses requests above 50 MB (HTTP 403). No ladder entry may exceed 50,000,000 bytes.
- `speedtest-cli` must be gone from `pyproject.toml`, `requirements.txt`, and `uv.lock` when the plan is complete.
- Commit messages end with: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`

---

## File Structure

| File | Responsibility |
|---|---|
| `monitors/speedtest_policy.py` (create) | Pure functions `verdict()` and `next_level()`. No I/O. |
| `monitors/speedtest.py` (rewrite) | Cloudflare measurement. `run(config, params, transport=None)`. Keeps `_determine_status`. |
| `storage/db.py` (modify) | Add `recent_values(monitor, target, metric, hours)` for the rolling mean. |
| `config.py` (modify) | New `speedtest` defaults: `streams`, `timeout_seconds`, `adaptive` block with ladder. Drop `interval_seconds`. |
| `config.example.yaml` (modify) | Document the new block and per-level data cost. |
| `main.py` (modify) | `SpeedtestJob` class (module level, import-safe) replacing the `job_speedtest` closure; `add_job` uses the ladder's start-level interval. |
| `pyproject.toml`, `requirements.txt`, `uv.lock` (modify) | Remove `speedtest-cli`. |
| `CLAUDE.md`, `README.md` (modify) | Remove stale Ookla/30-minute statements. |
| `tests/test_speedtest_policy.py` (create) | Policy tests. |
| `tests/test_speedtest_monitor.py` (create) | Monitor tests via `httpx.MockTransport`. |
| `tests/test_speedtest_job.py` (create) | `SpeedtestJob` wiring tests with a fake scheduler. |
| `tests/test_db.py`, `tests/test_config.py` (modify) | One test each for the new query and the new defaults. |

---

### Task 1: Policy module

**Files:**
- Create: `monitors/speedtest_policy.py`
- Test: `tests/test_speedtest_policy.py`

**Interfaces:**
- Produces:
  - `verdict(download_mbps: float, expected_mbps: float, degraded_ratio: float, history: list[float], avg_ratio: float, min_samples: int) -> str` returning `"good"` or `"poor"`.
  - `next_level(level: int, verdict: str, good_streak: int, calm_after: int, max_level: int) -> tuple[int, int]` returning `(new_level, new_good_streak)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_speedtest_policy.py`:

```python
"""Tests for monitors/speedtest_policy.py — the pure good/poor verdict and
the intensity-ladder transition. No I/O, no scheduler."""

from __future__ import annotations

from monitors.speedtest_policy import next_level, verdict


class TestVerdict:
    def test_failed_run_is_poor(self):
        assert verdict(-1.0, 0, 0.5, [], 0.8, 3) == "poor"

    def test_no_expectation_no_history_is_good(self):
        assert verdict(10.0, 0, 0.5, [], 0.8, 3) == "good"

    def test_below_fixed_threshold_is_poor(self):
        # 20 < 50 * 0.5
        assert verdict(20.0, 50, 0.5, [], 0.8, 3) == "poor"

    def test_at_fixed_threshold_is_good(self):
        assert verdict(25.0, 50, 0.5, [], 0.8, 3) == "good"

    def test_below_rolling_mean_ratio_is_poor(self):
        # mean(history) = 300; 0.8 * 300 = 240; 200 < 240
        assert verdict(200.0, 0, 0.5, [300.0, 300.0, 300.0], 0.8, 3) == "poor"

    def test_at_rolling_mean_ratio_is_good(self):
        assert verdict(240.0, 0, 0.5, [300.0, 300.0, 300.0], 0.8, 3) == "good"

    def test_rolling_mean_ignored_below_min_samples(self):
        # Only two samples: the rolling rule must not apply.
        assert verdict(200.0, 0, 0.5, [300.0, 300.0], 0.8, 3) == "good"

    def test_fixed_threshold_still_applies_below_min_samples(self):
        assert verdict(20.0, 50, 0.5, [300.0], 0.8, 3) == "poor"

    def test_either_rule_can_trigger(self):
        # Passes the fixed threshold (100 >= 25) but fails the rolling rule.
        assert verdict(100.0, 50, 0.5, [300.0, 300.0, 300.0], 0.8, 3) == "poor"


class TestNextLevel:
    def test_poor_rises_one_level_and_resets_streak(self):
        assert next_level(1, "poor", 1, 2, 3) == (2, 0)

    def test_poor_is_capped_at_max_level(self):
        assert next_level(3, "poor", 0, 2, 3) == (3, 0)

    def test_good_increments_streak_without_moving(self):
        assert next_level(2, "good", 0, 2, 3) == (2, 1)

    def test_good_streak_reaching_calm_after_drops_one_level(self):
        assert next_level(2, "good", 1, 2, 3) == (1, 0)

    def test_good_at_level_zero_stays_at_zero(self):
        assert next_level(0, "good", 1, 2, 3) == (0, 0)

    def test_calm_after_one_drops_immediately(self):
        assert next_level(3, "good", 0, 1, 3) == (2, 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_speedtest_policy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'monitors.speedtest_policy'`

- [ ] **Step 3: Write the implementation**

Create `monitors/speedtest_policy.py`:

```python
"""Adaptive-intensity policy for the speedtest monitor.

Pure functions, no I/O. The speedtest job in main.py calls these after every
run to decide whether the last result was good or poor and which rung of the
intensity ladder to use next. See
docs/superpowers/specs/2026-10-09-cloudflare-adaptive-speedtest-design.md.
"""

from __future__ import annotations

Verdict = str  # "good" | "poor"


def verdict(
    download_mbps: float,
    expected_mbps: float,
    degraded_ratio: float,
    history: list[float],
    avg_ratio: float,
    min_samples: int,
) -> Verdict:
    """Classify one run.

    poor when any of:
      - the run failed (download_mbps < 0)
      - download is below the configured degraded threshold
        (expected_mbps * degraded_ratio; skipped when expected_mbps <= 0)
      - download is below avg_ratio * mean(history), but only once history
        holds at least min_samples readings
    otherwise good.
    """
    if download_mbps < 0:
        return "poor"
    if expected_mbps > 0 and download_mbps < expected_mbps * degraded_ratio:
        return "poor"
    if len(history) >= min_samples:
        mean = sum(history) / len(history)
        if download_mbps < avg_ratio * mean:
            return "poor"
    return "good"


def next_level(
    level: int,
    verdict: Verdict,
    good_streak: int,
    calm_after: int,
    max_level: int,
) -> tuple[int, int]:
    """Return (new_level, new_good_streak).

    poor -> one rung up (capped), streak reset.
    good -> streak + 1; once it reaches calm_after, one rung down (floored
            at 0) and the streak resets.
    """
    if verdict == "poor":
        return min(level + 1, max_level), 0
    good_streak += 1
    if good_streak >= calm_after:
        return max(level - 1, 0), 0
    return level, good_streak
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_speedtest_policy.py -v`
Expected: 15 passed

- [ ] **Step 5: Commit**

```bash
git add monitors/speedtest_policy.py tests/test_speedtest_policy.py
git commit -m "feat: pure verdict/next_level policy for adaptive speedtest

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Rolling-history query in storage

**Files:**
- Modify: `storage/db.py` (add after `query_recent`, around line 97)
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `async def recent_values(monitor: str, target: str, metric: str, hours: int = 24) -> list[float]` — values of successful rows (`value >= 0`) for one (monitor, target, metric) within the window, oldest first.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_db.py`:

```python
async def test_recent_values_filters_target_metric_and_failures():
    await db.init_db()
    ts = datetime.now(timezone.utc).isoformat()
    await db.insert_metrics([
        MonitorResult("speedtest", "cloudflare", ts, "download_mbps", 300.0, "ok"),
        MonitorResult("speedtest", "cloudflare", ts, "download_mbps", 280.0, "ok"),
        MonitorResult("speedtest", "cloudflare", ts, "download_mbps", -1.0, "down"),   # failed run
        MonitorResult("speedtest", "cloudflare", ts, "upload_mbps", 40.0, "ok"),       # other metric
        MonitorResult("speedtest", "ookla", ts, "download_mbps", 15.0, "ok"),           # other target
    ])
    values = await db.recent_values("speedtest", "cloudflare", "download_mbps", hours=24)
    assert sorted(values) == [280.0, 300.0]


async def test_recent_values_excludes_rows_outside_window():
    await db.init_db()
    old = "2000-01-01T00:00:00+00:00"
    await db.insert_metrics([
        MonitorResult("speedtest", "cloudflare", old, "download_mbps", 300.0, "ok"),
    ])
    assert await db.recent_values("speedtest", "cloudflare", "download_mbps", hours=24) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_db.py -k recent_values -v`
Expected: FAIL with `AttributeError: module 'storage.db' has no attribute 'recent_values'`

- [ ] **Step 3: Write the implementation**

Insert into `storage/db.py` directly after the `query_recent` function:

```python
async def recent_values(monitor: str, target: str, metric: str, hours: int = 24) -> list[float]:
    """Successful readings (value >= 0) for one series inside the window,
    oldest first. Used by the adaptive speedtest policy for its rolling mean;
    the target filter keeps readings from a previous provider out of it."""
    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT value
            FROM   metrics
            WHERE  monitor = ?
              AND  target = ?
              AND  metric = ?
              AND  value >= 0
              AND  timestamp > datetime('now', ? || ' hours')
            ORDER BY timestamp ASC
            """,
            (monitor, target, metric, f"-{hours}"),
        )
        rows = await cursor.fetchall()
        return [float(r[0]) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_db.py -v`
Expected: all pass, including the two new tests

- [ ] **Step 5: Commit**

```bash
git add storage/db.py tests/test_db.py
git commit -m "feat: db.recent_values for the speedtest rolling mean

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Configuration defaults and example

**Files:**
- Modify: `config.py:34-41` (the `"speedtest"` block in `DEFAULT_CONFIG`)
- Modify: `config.example.yaml:52-60` (the `speedtest:` block) and the three comments at lines 105-107 and 121 that say speedtest runs every 30 min
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `config["monitors"]["speedtest"]` with keys `expected_download_mbps`, `expected_upload_mbps`, `degraded_alert_minutes`, `degraded_alert_cycles`, `thresholds.degraded_ratio`, `streams`, `timeout_seconds`, `adaptive.avg_ratio`, `adaptive.min_samples`, `adaptive.calm_after`, `adaptive.ladder` (list of dicts with `name`, `interval_seconds`, `download_bytes`, `upload_bytes`). No `interval_seconds` at the speedtest level.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_speedtest_defaults_have_adaptive_ladder():
    cfg = load_config("/nonexistent/config.yaml")
    speed = cfg["monitors"]["speedtest"]
    assert "interval_seconds" not in speed
    assert speed["streams"] == 4
    assert speed["timeout_seconds"] == 60
    adaptive = speed["adaptive"]
    assert adaptive["avg_ratio"] == 0.8
    assert adaptive["min_samples"] == 3
    assert adaptive["calm_after"] == 2
    ladder = adaptive["ladder"]
    assert [rung["name"] for rung in ladder] == ["calm", "watch", "alert", "investigate"]
    assert [rung["interval_seconds"] for rung in ladder] == [1800, 300, 120, 60]
    assert [rung["download_bytes"] for rung in ladder] == [10_000_000, 10_000_000, 25_000_000, 25_000_000]
    assert [rung["upload_bytes"] for rung in ladder] == [4_000_000, 4_000_000, 10_000_000, 10_000_000]
    assert all(rung["download_bytes"] <= 50_000_000 for rung in ladder)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_speedtest_defaults_have_adaptive_ladder -v`
Expected: FAIL with `AssertionError` on `"interval_seconds" not in speed`

- [ ] **Step 3: Replace the speedtest block in `config.py`**

Replace lines 34-41 (`"speedtest": {...}`) with:

```python
        "speedtest": {
            "expected_download_mbps": 0,
            "expected_upload_mbps": 0,
            "degraded_alert_minutes": None,
            "degraded_alert_cycles": None,
            "thresholds": {"degraded_ratio": 0.5},
            # Cloudflare measurement: `streams` concurrent transfers per
            # direction. Keep streams constant across ladder rungs so readings
            # stay comparable — one TCP window cannot fill a fast link.
            "streams": 4,
            "timeout_seconds": 60,
            # Adaptive cadence. The job starts at rung 1, climbs one rung on
            # a poor run, and descends one rung after `calm_after` consecutive
            # good runs. A run is poor when download is below
            # expected * degraded_ratio, or below avg_ratio * the 24 h mean
            # (once min_samples readings exist). Cloudflare refuses downloads
            # above 50 MB. See monitors/speedtest_policy.py.
            "adaptive": {
                "avg_ratio": 0.8,
                "min_samples": 3,
                "calm_after": 2,
                "ladder": [
                    {"name": "calm",        "interval_seconds": 1800, "download_bytes": 10_000_000, "upload_bytes": 4_000_000},
                    {"name": "watch",       "interval_seconds": 300,  "download_bytes": 10_000_000, "upload_bytes": 4_000_000},
                    {"name": "alert",       "interval_seconds": 120,  "download_bytes": 25_000_000, "upload_bytes": 10_000_000},
                    {"name": "investigate", "interval_seconds": 60,   "download_bytes": 25_000_000, "upload_bytes": 10_000_000},
                ],
            },
        },
```

- [ ] **Step 4: Replace the speedtest block in `config.example.yaml`**

Replace lines 52-60 (`speedtest:` through `degraded_ratio: 0.5    # ...`) with:

```yaml
  speedtest:
    # Measures against speed.cloudflare.com (nearest Cloudflare edge).
    expected_download_mbps: 50
    expected_upload_mbps: 10
    # Degraded alerting (opt-in) — see the ping section above for details.
    # degraded_alert_cycles: 3      # Cycles alone is enough; minutes may stay unset
    thresholds:
      degraded_ratio: 0.5    # Below 50% of expected = degraded
    streams: 4               # concurrent transfers per direction; same at every rung
    timeout_seconds: 60      # per HTTP request
    # Adaptive cadence. Starts at rung 1 ("watch"). A poor run climbs one
    # rung; `calm_after` consecutive good runs descend one. A run is poor
    # when download < expected * degraded_ratio, or < avg_ratio * the mean
    # of the last 24 h (once min_samples readings exist).
    # Data per run = streams * (download_bytes + upload_bytes). Held all day:
    #   calm 2.7 GB, watch 16 GB, alert 100 GB, investigate 200 GB.
    # Cloudflare refuses download_bytes above 50000000. Overriding `ladder`
    # replaces the whole list (lists are not deep-merged).
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

Then edit the LED comments in the same file. At lines 105-107 (the comment inside `leds:` that reads `# deliberately downweighted: it only runs every 30 min (see` / `# monitors.speedtest.interval_seconds), so a stale or failed run should`) change the text to:

```yaml
    # deliberately downweighted: it runs on an adaptive interval (see
    # monitors.speedtest.adaptive.ladder), so a stale or failed run should
```

At line 121 (`# polls. Between a ping poll (30 s) and a speedtest (30 min) the strip`) change `(30 min)` to `(minutes apart)`.

- [ ] **Step 5: Run the config tests**

Run: `uv run pytest tests/test_config.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add config.py config.example.yaml tests/test_config.py
git commit -m "feat: speedtest config gains streams, timeout and adaptive ladder

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Cloudflare measurement monitor

**Files:**
- Rewrite: `monitors/speedtest.py`
- Test: `tests/test_speedtest_monitor.py`
- Keep passing: `tests/test_thresholds.py` (imports `_determine_status` from `monitors.speedtest`)

**Interfaces:**
- Consumes: `config["monitors"]["speedtest"]` keys `streams`, `timeout_seconds`, `expected_download_mbps`, `expected_upload_mbps`, `thresholds.degraded_ratio` (Task 3).
- Produces:
  - `async def run(config: dict, params: dict, transport: httpx.AsyncBaseTransport | None = None) -> list[MonitorResult]` where `params` is one ladder rung plus a `level` key: `{"level": int, "name": str, "download_bytes": int, "upload_bytes": int, "interval_seconds": int}`. `transport` is only for tests.
  - `_determine_status(value_mbps, expected_mbps, degraded_ratio) -> Status` unchanged.
  - `_run_label(params, streams) -> str`, e.g. `"level=2 alert 4x25MB/4x10MB"`.
  - Result rows: `target="cloudflare"`, metrics `download_mbps`, `upload_mbps`, `ping_ms`, each with `message=_run_label(...)`. On any failure: a single `download_mbps` row with `value=-1.0`, `status="down"`, `message=str(exc)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_speedtest_monitor.py`:

```python
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
    # 5 latency probes ask for 0 bytes; the 3 download streams ask for 1_000 each.
    assert sorted(rec.downloads) == [0, 0, 0, 0, 0, 1_000, 1_000, 1_000]


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_speedtest_monitor.py -v`
Expected: FAIL — `run()` takes 1 positional argument / `_run_label` not defined

- [ ] **Step 3: Rewrite `monitors/speedtest.py`**

Replace the entire file with:

```python
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
```

- [ ] **Step 4: Run the monitor and threshold tests**

Run: `uv run pytest tests/test_speedtest_monitor.py tests/test_thresholds.py -v`
Expected: all pass (8 new + existing threshold tests)

- [ ] **Step 5: Commit**

```bash
git add monitors/speedtest.py tests/test_speedtest_monitor.py
git commit -m "feat: measure bandwidth against Cloudflare with concurrent streams

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Adaptive job wiring in main.py

**Files:**
- Modify: `main.py` — add a module-level `SpeedtestJob` class above `main()` (after `run_monitor`, around line 400), replace the `job_speedtest` closure at line 575-576, and the `add_job` line at 597
- Test: `tests/test_speedtest_job.py`

**Interfaces:**
- Consumes: `speedtest_monitor.run(config, params)` (Task 4), `db.recent_values(...)` (Task 2), `speedtest_policy.verdict/next_level` (Task 1), `run_monitor(name, results)` (existing), ladder config (Task 3).
- Produces: `class SpeedtestJob` with `__init__(self, config: dict, scheduler, job_id: str = "speedtest")`, attributes `level: int`, `good_streak: int`, property `interval_seconds -> int` (current rung's interval), and `async def __call__(self) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_speedtest_job.py`:

```python
"""Tests for main.SpeedtestJob — the adaptive-cadence wrapper around the
speedtest monitor. The monitor, the DB history and run_monitor() are
stubbed; a MagicMock stands in for the APScheduler instance so the test can
assert reschedule_job() calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import main
from monitors.base import MonitorResult

LADDER = [
    {"name": "calm",        "interval_seconds": 1800, "download_bytes": 10, "upload_bytes": 4},
    {"name": "watch",       "interval_seconds": 300,  "download_bytes": 10, "upload_bytes": 4},
    {"name": "alert",       "interval_seconds": 120,  "download_bytes": 25, "upload_bytes": 10},
    {"name": "investigate", "interval_seconds": 60,   "download_bytes": 25, "upload_bytes": 10},
]


def _config() -> dict:
    return {"monitors": {"speedtest": {
        "expected_download_mbps": 50,
        "thresholds": {"degraded_ratio": 0.5},
        "adaptive": {"avg_ratio": 0.8, "min_samples": 3, "calm_after": 2, "ladder": LADDER},
    }}}


def _download(value: float) -> list[MonitorResult]:
    status = "down" if value < 0 else "ok"
    return [MonitorResult("speedtest", "cloudflare", "2026-01-01T00:00:00+00:00",
                          "download_mbps", value, status)]


@pytest.fixture
def stubs(monkeypatch):
    run = AsyncMock(return_value=_download(300.0))
    history = AsyncMock(return_value=[])
    record = AsyncMock()
    monkeypatch.setattr(main.speedtest_monitor, "run", run)
    monkeypatch.setattr(main.db, "recent_values", history)
    monkeypatch.setattr(main, "run_monitor", record)
    return run, history, record


async def test_starts_at_level_one_with_its_interval():
    job = main.SpeedtestJob(_config(), MagicMock())
    assert job.level == 1
    assert job.good_streak == 0
    assert job.interval_seconds == 300


async def test_passes_current_rung_and_level_to_monitor(stubs):
    run, _, record = stubs
    scheduler = MagicMock()
    job = main.SpeedtestJob(_config(), scheduler)
    await job()
    _, params = run.call_args.args
    assert params["level"] == 1
    assert params["name"] == "watch"
    assert params["download_bytes"] == 10
    record.assert_awaited_once()
    assert record.call_args.args[0] == "speedtest"


async def test_poor_run_escalates_and_reschedules(stubs):
    run, _, _ = stubs
    run.return_value = _download(10.0)   # below 50 * 0.5
    scheduler = MagicMock()
    job = main.SpeedtestJob(_config(), scheduler)
    await job()
    assert job.level == 2
    scheduler.reschedule_job.assert_called_once_with("speedtest", trigger="interval", seconds=120)


async def test_good_run_without_level_change_does_not_reschedule(stubs):
    scheduler = MagicMock()
    job = main.SpeedtestJob(_config(), scheduler)
    await job()                      # streak 1 of 2
    assert job.level == 1
    assert job.good_streak == 1
    scheduler.reschedule_job.assert_not_called()


async def test_two_good_runs_calm_down(stubs):
    scheduler = MagicMock()
    job = main.SpeedtestJob(_config(), scheduler)
    await job()
    await job()
    assert job.level == 0
    scheduler.reschedule_job.assert_called_once_with("speedtest", trigger="interval", seconds=1800)


async def test_rolling_mean_from_db_feeds_verdict(stubs):
    run, history, _ = stubs
    history.return_value = [300.0, 300.0, 300.0]
    run.return_value = _download(100.0)  # passes 25 Mbps floor, fails 0.8 * 300
    scheduler = MagicMock()
    job = main.SpeedtestJob(_config(), scheduler)
    await job()
    assert job.level == 2
    history.assert_awaited_once_with("speedtest", "cloudflare", "download_mbps", hours=24)


async def test_failed_run_escalates(stubs):
    run, _, _ = stubs
    run.return_value = _download(-1.0)
    job = main.SpeedtestJob(_config(), MagicMock())
    await job()
    assert job.level == 2


async def test_escalation_caps_at_top_rung(stubs):
    run, _, _ = stubs
    run.return_value = _download(-1.0)
    scheduler = MagicMock()
    job = main.SpeedtestJob(_config(), scheduler)
    for _ in range(5):
        await job()
    assert job.level == 3
    # Rescheduled on 1->2 and 2->3 only.
    assert scheduler.reschedule_job.call_count == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_speedtest_job.py -v`
Expected: FAIL with `AttributeError: module 'main' has no attribute 'SpeedtestJob'`

- [ ] **Step 3: Add `SpeedtestJob` to `main.py`**

First add the import near the other monitor imports (after `from monitors import speedtest as speedtest_monitor`):

```python
from monitors import speedtest_policy
```

Then add this class at module level, after `run_monitor()` and before `main()`:

```python
# ---------------------------------------------------------------------------
# Adaptive speedtest job — owns the intensity level, reschedules itself
# ---------------------------------------------------------------------------

class SpeedtestJob:
    """Scheduler callable for the speedtest monitor.

    Holds the current rung of monitors.speedtest.adaptive.ladder. After each
    run it classifies the result (speedtest_policy.verdict), moves one rung
    up on a poor run or one rung down after `calm_after` good runs
    (speedtest_policy.next_level), and reschedules its own APScheduler
    interval when the rung changes. Starts at rung 1 on every process start.
    See docs/superpowers/specs/2026-10-09-cloudflare-adaptive-speedtest-design.md.
    """

    def __init__(self, config: dict, scheduler, job_id: str = "speedtest") -> None:
        self._config = config
        self._scheduler = scheduler
        self._job_id = job_id
        cfg = config["monitors"]["speedtest"]
        self._expected = float(cfg.get("expected_download_mbps", 0))
        self._degraded_ratio = float(cfg.get("thresholds", {}).get("degraded_ratio", 0.5))
        adaptive = cfg["adaptive"]
        self._ladder: list[dict] = adaptive["ladder"]
        self._avg_ratio = float(adaptive.get("avg_ratio", 0.8))
        self._min_samples = int(adaptive.get("min_samples", 3))
        self._calm_after = int(adaptive.get("calm_after", 2))
        self.level = min(1, len(self._ladder) - 1)
        self.good_streak = 0

    @property
    def interval_seconds(self) -> int:
        return int(self._ladder[self.level]["interval_seconds"])

    def _params(self) -> dict:
        return {"level": self.level, **self._ladder[self.level]}

    async def __call__(self) -> None:
        results = await speedtest_monitor.run(self._config, self._params())
        await run_monitor("speedtest", results)

        download = next(
            (r.value for r in results if r.metric == "download_mbps"), -1.0
        )
        history = await db.recent_values("speedtest", "cloudflare", "download_mbps", hours=24)
        outcome = speedtest_policy.verdict(
            download, self._expected, self._degraded_ratio,
            history, self._avg_ratio, self._min_samples,
        )
        previous = self.level
        self.level, self.good_streak = speedtest_policy.next_level(
            self.level, outcome, self.good_streak, self._calm_after, len(self._ladder) - 1
        )
        if self.level != previous:
            self._scheduler.reschedule_job(
                self._job_id, trigger="interval", seconds=self.interval_seconds
            )
            logger.info(
                "speedtest %s run: level %d (%s) -> %d (%s), next in %ds",
                outcome, previous, self._ladder[previous]["name"],
                self.level, self._ladder[self.level]["name"], self.interval_seconds,
            )
```

Note: `run_monitor` must be looked up at call time (as written above, a plain module-level name reference inside the method), so the test's `monkeypatch.setattr(main, "run_monitor", ...)` takes effect.

- [ ] **Step 4: Replace the closure and the add_job line in `main()`**

Delete these lines inside `main()`:

```python
    async def job_speedtest() -> None:
        await run_monitor("speedtest", await speedtest_monitor.run(config))
```

Replace them with:

```python
    job_speedtest = SpeedtestJob(config, scheduler)
```

This line must come after `scheduler = AsyncIOScheduler(executors=_build_executors())`.

Replace the `add_job` line for speedtest:

```python
    scheduler.add_job(job_speedtest, "interval", seconds=speed_cfg.get("interval_seconds", 1800), id="speedtest", misfire_grace_time=60)
```

with:

```python
    # Bound method, not the instance: APScheduler's coroutine detection
    # (iscoroutinefunction) does not see an instance's async __call__ and
    # would run it in a thread, silently dropping the coroutine.
    scheduler.add_job(job_speedtest.__call__, "interval", seconds=job_speedtest.interval_seconds, id="speedtest", misfire_grace_time=60, max_instances=1, coalesce=True)
```

`speed_cfg` is no longer used after this change; delete the line `speed_cfg    = config["monitors"]["speedtest"]`.

- [ ] **Step 5: Run the job tests and the full suite**

Run: `uv run pytest tests/test_speedtest_job.py -v`
Expected: 8 passed

Run: `uv run pytest tests/`
Expected: all pass

- [ ] **Step 6: Smoke-start the app to prove the scheduler accepts a callable object**

Run: `uv run python -c "import main; import asyncio; from apscheduler.schedulers.asyncio import AsyncIOScheduler; from config import load_config; s = AsyncIOScheduler(); j = main.SpeedtestJob(load_config('/nonexistent'), s); s.add_job(j.__call__, 'interval', seconds=j.interval_seconds, id='speedtest'); print('ok', j.interval_seconds)"`
Expected output: `ok 300`

- [ ] **Step 7: Commit**

```bash
git add main.py tests/test_speedtest_job.py
git commit -m "feat: adaptive speedtest cadence via self-rescheduling SpeedtestJob

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Remove speedtest-cli and update docs

**Files:**
- Modify: `pyproject.toml:17`, `requirements.txt:12`, `uv.lock` (via `uv remove`)
- Modify: `CLAUDE.md:42` and `CLAUDE.md:46`
- Modify: `README.md:15` and `README.md:177`

- [ ] **Step 1: Remove the dependency**

Run: `uv remove speedtest-cli`
Expected: `pyproject.toml` no longer lists `speedtest-cli`; `uv.lock` updated.

Edit `requirements.txt`: delete the line `speedtest-cli>=2.1.3`.

- [ ] **Step 2: Prove nothing imports it**

Run: `uv run python -c "import main, monitors.speedtest; print('imports ok')"`
Expected: `imports ok`

Run: `grep -rn "speedtest as st\|import speedtest\b\|speedtest-cli" --include=*.py --include=*.toml --include=*.txt --include=*.md . | grep -v docs/superpowers | grep -v .venv`
Expected: no output

- [ ] **Step 3: Update CLAUDE.md**

Line 42, change `Blocking work (ping subprocess, `speedtest-cli`, LED `show()`)` to `Blocking work (ping subprocess, LED `show()`)`, and change `would let a 30-minute speedtest holding the executor stutter the frame timing` to `would let any long executor job stutter the frame timing`.

Line 46, change `(speedtest weighted low — it only runs every 30 min)` to `(speedtest weighted low — it runs on an adaptive interval of minutes, not seconds)`.

Add a new bullet to the **Conventions that matter** list, after the one about `_determine_status`:

```markdown
- The speedtest monitor measures against `speed.cloudflare.com` with `monitors.speedtest.streams` concurrent transfers (a single TCP stream under-reads a fast link by more than half). Its cadence is adaptive: `main.SpeedtestJob` holds a rung of `monitors.speedtest.adaptive.ladder`, `monitors/speedtest_policy.py` classifies each run good/poor (below `expected × degraded_ratio`, or below `avg_ratio` × the 24 h mean from `db.recent_values`) and moves one rung up on poor or one rung down after `calm_after` good runs, and the job calls `scheduler.reschedule_job` when the rung changes. Rung 1 on every start. Each result row's `message` names the rung that produced it. Design spec: `docs/superpowers/specs/2026-10-09-cloudflare-adaptive-speedtest-design.md`.
```

- [ ] **Step 4: Update README.md**

Line 15, change `| **Speedtest** | Download / upload Mbps via Speedtest.net | 30 min |` to `| **Speedtest** | Download / upload Mbps via speed.cloudflare.com | adaptive, 1–30 min |`.

Line 177, replace `Keep the interval ≥ 30 minutes on a Pi 3B — the test saturates the CPU briefly.` with `Cadence is adaptive (see `monitors.speedtest.adaptive` in config.example.yaml): good results slow it to every 30 minutes, poor results speed it up to every minute with larger transfers until the link recovers.`

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest tests/`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock requirements.txt CLAUDE.md README.md
git commit -m "chore: drop speedtest-cli; document Cloudflare adaptive speedtest

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Deploy to hpz440 and verify

**Files:** none modified. Uses `docker-compose.yml` with the default `hpz440` Docker context.

This task changes a running service on Conrad's server. It was approved as part of the design.

- [ ] **Step 1: Confirm the Docker context**

Run: `docker context show`
Expected: `hpz440`

- [ ] **Step 2: Build the image**

Run: `docker compose build`
Expected: ends with the image `internet-schminternet:latest` built; the `uv sync --frozen` layer re-runs because the lockfile changed.

- [ ] **Step 3: Restart the container**

Run: `docker compose up -d`
Expected: `Container schminternet  Started` (or Recreated).

- [ ] **Step 4: Watch the first run**

The first speedtest fires one rung-1 interval (5 min) after start. Wait, then:

Run: `docker logs --since 7m schminternet`
Expected: a line like `speedtest ok  down 3xx.x Mbps up xx.x Mbps` and no traceback.

- [ ] **Step 5: Verify via the API**

Run: `docker exec schminternet python -c "import urllib.request, json; rows = json.load(urllib.request.urlopen('http://localhost:8080/api/metrics/speedtest?hours=1')); [print(r['timestamp'], r['target'], r['metric'], r['value'], r['status']) for r in rows]"`
Expected: rows with `target == cloudflare`, download in the hundreds of Mbps (the host measured 383 Mbps with four raw streams on 2026-10-09).

- [ ] **Step 6: Report**

State the first measured download and upload, and whether they sit near the ISP's figure. No commit.

---

## Self-review

**Spec coverage.** Measurement with concurrent streams, latency median, error sentinel → Task 4. Ladder, verdict, transitions, defaults → Tasks 1, 3, 5. `recent_values` → Task 2. `reschedule_job` on change, level 1 on start, no immediate startup run (the startup loop at `main.py:608` already omits speedtest; Task 5 does not add it) → Task 5. Message carries level → Task 4 (`_run_label`). Dependency removal and docs → Task 6. Deployment and verification → Task 7.

**Placeholders.** None.

**Type consistency.** `speedtest_monitor.run(config, params, transport=None)` is called as `run(self._config, self._params())` in Task 5 and stubbed with `AsyncMock` taking `(config, params)` in its tests. `db.recent_values(monitor, target, metric, hours=24)` matches between Task 2 and Task 5's call and assertion. `verdict`/`next_level` signatures match between Task 1 and Task 5. Ladder key names (`name`, `interval_seconds`, `download_bytes`, `upload_bytes`) match across Tasks 3, 4, 5.
