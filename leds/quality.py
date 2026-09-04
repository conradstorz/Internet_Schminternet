"""Quality scoring and colour mapping for the rank-sorted LED strip.

Pure functions only — no hardware, no I/O — so they run identically whether
or not `rpi_ws281x` is installed, and are unit-testable on any machine.

Score model: every monitor maps its own configured thresholds onto a
0.0 (unusable) - 1.0 (perfect) scale via a shared piecewise-linear curve:
0 -> 1.0, the monitor's "degraded" knee -> 0.6, its "down" knee -> 0.2, and
anything past "down" -> 0.0 flat. `overall_score` then takes a configurable
weighted mean across whichever monitors have reported so far.
"""

from __future__ import annotations

import colorsys

from monitors.base import MonitorResult

# The error sentinel every monitor uses for a failed/unavailable measurement.
ERROR_VALUE = -1.0

# Defaults mirrored in config.py's DEFAULT_CONFIG (leds.weights) — kept here
# too so callers (and tests) that only import this module have a sane weight
# set without needing config.py. speedtest is weighted low: it only runs
# every 30 minutes, so a stale or failed run should tint the overall colour
# rather than dominate it.
DEFAULT_WEIGHTS: dict[str, float] = {
    "ping": 1.0,
    "dns": 1.0,
    "http": 1.0,
    "speedtest": 0.25,
}

# Monitors this module knows how to score. "ip" is deliberately excluded —
# an address change is an event, not a quality measurement.
SCORED_MONITORS: tuple[str, ...] = ("ping", "dns", "http", "speedtest")


def _lerp_knee(value: float, x0: float, y0: float, x1: float, y1: float) -> float:
    """Linearly interpolate y at `value` between knees (x0, y0) and (x1, y1),
    clamped to the [y0, y1] segment. Degenerate (x1 <= x0) knees fall back to
    a step at x1 rather than dividing by zero."""
    if x1 <= x0:
        return y1 if value >= x1 else y0
    frac = (value - x0) / (x1 - x0)
    frac = max(0.0, min(1.0, frac))
    return y0 + (y1 - y0) * frac


def _piecewise_score(value: float, degraded: float, down: float) -> float:
    """Shared "lower is better" curve: 0 -> 1.0, degraded -> 0.6, down -> 0.2,
    beyond down -> 0.0 flat."""
    if value <= 0:
        return 1.0
    if value <= degraded:
        return _lerp_knee(value, 0.0, 1.0, degraded, 0.6)
    if value <= down:
        return _lerp_knee(value, degraded, 0.6, down, 0.2)
    return 0.0


def _score_ping(results: list[MonitorResult], cfg: dict) -> float:
    thresholds = cfg.get("thresholds", {})
    degraded_ms = thresholds.get("degraded_ms", 100)
    down_ms = thresholds.get("down_ms", 500)
    loss_degraded = thresholds.get("loss_degraded_pct", 10)
    loss_down = thresholds.get("loss_down_pct", 50)

    by_target: dict[str, dict[str, float]] = {}
    for r in results:
        by_target.setdefault(r.target, {})[r.metric] = r.value

    if not by_target:
        return 0.0

    target_scores = []
    for metrics in by_target.values():
        latency = metrics.get("latency_ms")
        loss = metrics.get("packet_loss_pct")

        if latency is None or latency == ERROR_VALUE:
            # No reply at all for this target's latency leg.
            latency_score = 0.0
        else:
            latency_score = _piecewise_score(latency, degraded_ms, down_ms)

        if loss is None:
            loss_score = 1.0  # not measured — don't penalise for it
        elif loss == ERROR_VALUE:
            loss_score = 0.0
        else:
            loss_score = _piecewise_score(loss, loss_degraded, loss_down)

        target_scores.append(min(latency_score, loss_score))

    return sum(target_scores) / len(target_scores)


def _score_dns(results: list[MonitorResult], cfg: dict) -> float:
    thresholds = cfg.get("thresholds", {})
    degraded_ms = thresholds.get("degraded_ms", 200)
    down_ms = thresholds.get("down_ms", 1000)

    if not results:
        return 0.0

    scores = [
        0.0 if r.value < 0 else _piecewise_score(r.value, degraded_ms, down_ms)
        for r in results
    ]
    return sum(scores) / len(scores)


def _score_http(results: list[MonitorResult], cfg: dict) -> float:
    thresholds = cfg.get("thresholds", {})
    degraded_ms = thresholds.get("degraded_ms", 1000)
    down_ms = degraded_ms * 2  # http config has no down_ms of its own

    if not results:
        return 0.0

    scores = [
        0.0 if r.value < 0 else _piecewise_score(r.value, degraded_ms, down_ms)
        for r in results
    ]
    return sum(scores) / len(scores)


def _score_speedtest(results: list[MonitorResult], cfg: dict) -> float:
    expected = cfg.get("expected_download_mbps", 0)

    download = None
    for r in results:
        if r.metric == "download_mbps":
            download = r.value
            break

    if download is None or download < 0:
        return 0.0
    if expected <= 0:
        return 1.0
    return max(0.0, min(1.0, download / expected))


_SCORERS = {
    "ping": _score_ping,
    "dns": _score_dns,
    "http": _score_http,
    "speedtest": _score_speedtest,
}


def score_monitor(monitor: str, results: list[MonitorResult], cfg: dict) -> float:
    """Score one monitor's latest results on a 0.0 (unusable) - 1.0 (perfect)
    scale, using that monitor's own configured thresholds. `cfg` is the
    monitor's own config sub-dict (e.g. `config["monitors"]["ping"]`), not
    the whole app config.

    The "ip" monitor (and any other monitor this module doesn't know how to
    score) always scores 0.0 — callers should exclude it from the strip
    entirely rather than rely on this fallback for ranking purposes.
    """
    if not results:
        return 0.0
    scorer = _SCORERS.get(monitor)
    if scorer is None:
        return 0.0
    return scorer(results, cfg)


def overall_score(scores: dict[str, float], weights: dict[str, float]) -> float:
    """Weighted mean of whichever monitors are present in `scores`. A
    monitor absent from `scores` (not yet reported) is skipped entirely,
    not counted as a zero."""
    if not scores:
        return 0.0
    total_weight = 0.0
    weighted_sum = 0.0
    for monitor, score in scores.items():
        weight = weights.get(monitor, 1.0)
        weighted_sum += score * weight
        total_weight += weight
    if total_weight <= 0:
        return 0.0
    return weighted_sum / total_weight


def quality_color(score: float) -> tuple[int, int, int]:
    """Map a 0.0-1.0 score to an 8-bit RGB colour: 1.0 -> green, 0.0 -> pure
    red (255, 0, 0) — the "remote access is gone" signal — interpolating hue
    (120 deg -> 0 deg) rather than RGB directly, which would pass through a
    muddy brown."""
    score = max(0.0, min(1.0, score))
    hue = (120.0 * score) / 360.0
    r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
    return (round(r * 255), round(g * 255), round(b * 255))


def quality_color_hex(score: float) -> str:
    """`quality_color` rendered as a `#rrggbb` string, for JSON/API use."""
    r, g, b = quality_color(score)
    return f"#{r:02x}{g:02x}{b:02x}"
