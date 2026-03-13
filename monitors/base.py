"""Shared types for all monitors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Status severity order: ok < degraded < down
Status = Literal["ok", "degraded", "down", "unknown"]


@dataclass
class MonitorResult:
    monitor: str       # e.g. "ping", "dns", "http", "speedtest", "ip"
    target: str        # e.g. "8.8.8.8", "https://google.com"
    timestamp: str     # ISO-8601 UTC string
    metric: str        # e.g. "latency_ms", "resolution_ms", "download_mbps"
    value: float       # Numeric measurement; -1.0 = error/unavailable
    status: Status
    message: str = field(default="")  # Optional human-readable detail
