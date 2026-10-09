"""Config loader — deep-merges config.yaml onto built-in defaults."""

from __future__ import annotations

import os
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "monitors": {
        "ping": {
            "targets": ["1.1.1.1", "wikipedia.org", "kernel.org"],
            "interval_seconds": 30,
            "count": 5,
            "degraded_alert_minutes": None,
            "degraded_alert_cycles": None,
            "thresholds": {
                "degraded_ms": 100,
                "down_ms": 500,
                "loss_degraded_pct": 10,
                "loss_down_pct": 50,
            },
        },
        "dns": {
            "servers": ["8.8.8.8", "9.9.9.9"],
            "test_domain": "google.com",
            "randomize_query": True,
            "interval_seconds": 60,
            "degraded_alert_minutes": None,
            "degraded_alert_cycles": None,
            "thresholds": {"degraded_ms": 200, "down_ms": 1000},
        },
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
            # Total deadline for the download phase and again for the upload
            # phase, not a per-request timeout; the latency phase's probes
            # share one deadline the same way. On expiry, a phase that moved
            # at least one byte is cut short and the bytes that did move are
            # reported over the elapsed time, so a link too slow to finish
            # reads as slow, not down — but a phase that moves zero bytes
            # before the deadline fails the run (one down row) and is not
            # retried.
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
        "http": {
            "targets": [
                {"url": "https://www.google.com"},
                {"url": "https://www.cloudflare.com"},
            ],
            "interval_seconds": 120,
            "timeout_seconds": 10,
            "degraded_alert_minutes": None,
            "degraded_alert_cycles": None,
            "thresholds": {"degraded_ms": 1000},
        },
        "ip": {
            "interval_seconds": 300,
            "degraded_alert_minutes": None,
            "degraded_alert_cycles": None,
        },
    },
    "database": {"path": "data/schminternet.db", "retention_days": 30},
    "web": {"host": "0.0.0.0", "port": 8080},
    "leds": {
        "enabled": False,
        "pin": 18,
        "count": 16,
        "brightness": 0.4,
        # Rank-sorted quality strip: no fixed per-monitor segments any more —
        # every scored monitor (ping, dns, http, speedtest; "ip" is excluded,
        # an address change is an event, not a quality measure) gets a slot
        # sized as evenly as `count` allows, re-sorted greenest-first on
        # every poll. See leds/quality.py.
        "weights": {"ping": 1.0, "dns": 1.0, "http": 1.0, "speedtest": 0.25},
        "orientation": "top_down",  # or "bottom_up" — matches physical mounting
        "overall": True,            # show a combined-score slot at the bottom end
        # Gentle "breathe" so the strip visibly moves between polls — a
        # still strip is indistinguishable from a crashed service. It
        # encodes liveness only, never severity: colour already carries
        # quality. See leds/animation.py.
        "animation": {
            "enabled": True,
            "period_seconds": 4.0,   # one full swell-and-dip
            "min_brightness": 0.7,   # dimmest point, as a fraction of `brightness`
            "fps": 25,
        },
    },
    "alerts": {"email": {"enabled": False}},
    "logging": {
        "path": "data/schminternet.log",   # relative to the working directory
        "level": "INFO",
        "max_bytes": 10_000_000,           # rotate at ~10 MB
        "backup_count": 5,                 # keep 5 rotated files
        "console": True,                   # also log to stdout (docker logs / journald)
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    """Load config from *path*, merging onto defaults. Missing file → defaults only."""
    if not os.path.exists(path):
        return _deep_merge({}, DEFAULT_CONFIG)
    with open(path, encoding="utf-8") as fh:
        user_cfg = yaml.safe_load(fh) or {}
    return _deep_merge(DEFAULT_CONFIG, user_cfg)
