"""Config loader — deep-merges config.yaml onto built-in defaults."""

from __future__ import annotations

import os
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "monitors": {
        "ping": {
            "targets": ["8.8.8.8", "1.1.1.1"],
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
            "servers": ["8.8.8.8", "1.1.1.1"],
            "test_domain": "google.com",
            "interval_seconds": 60,
            "degraded_alert_minutes": None,
            "degraded_alert_cycles": None,
            "thresholds": {"degraded_ms": 200, "down_ms": 1000},
        },
        "speedtest": {
            "interval_seconds": 1800,
            "expected_download_mbps": 0,
            "expected_upload_mbps": 0,
            "degraded_alert_minutes": None,
            "degraded_alert_cycles": None,
            "thresholds": {"degraded_ratio": 0.5},
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
        "segments": {
            "ping": [0, 3],
            "dns": [4, 6],
            "http": [7, 9],
            "speedtest": [10, 12],
            "ip": [13, 13],
            "overall": [14, 15],
        },
    },
    "alerts": {"email": {"enabled": False}},
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
