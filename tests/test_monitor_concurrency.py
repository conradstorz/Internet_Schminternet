"""Tests that ping and DNS monitors query all targets concurrently."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import monitors.dns as dns_monitor
import monitors.ping as ping_monitor


def _ping_cfg(targets: list[str]) -> dict:
    return {"monitors": {"ping": {"targets": targets, "count": 1, "thresholds": {}}}}


def _dns_cfg(servers: list[str]) -> dict:
    return {
        "monitors": {
            "dns": {
                "servers": servers,
                "test_domain": "example.com",
                "thresholds": {},
            }
        }
    }


# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------

async def test_ping_returns_results_for_all_targets():
    """Every configured target must appear in the results."""
    with patch("monitors.ping._ping_sync", return_value=(10.0, 0.0)):
        results = await ping_monitor.run(_ping_cfg(["8.8.8.8", "1.1.1.1"]))
    targets = {r.target for r in results}
    assert "8.8.8.8" in targets
    assert "1.1.1.1" in targets


async def test_ping_targets_run_concurrently():
    """Two targets each taking 0.1 s must finish in ~0.1 s total, not ~0.2 s."""

    def slow_ping(target: str, count: int):
        time.sleep(0.1)
        return (10.0, 0.0)

    with patch("monitors.ping._ping_sync", side_effect=slow_ping):
        start = time.perf_counter()
        await ping_monitor.run(_ping_cfg(["8.8.8.8", "1.1.1.1"]))
        elapsed = time.perf_counter() - start

    assert elapsed < 0.18, (
        f"Ping targets appear to run sequentially (took {elapsed:.3f}s — expected ~0.1s)"
    )


# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------

async def test_dns_returns_results_for_all_servers():
    """Every configured DNS server must appear in the results."""

    async def fake_resolve(*_a, **_kw):
        return MagicMock()

    with patch("dns.asyncresolver.Resolver") as MockResolver:
        inst = MagicMock()
        inst.resolve = fake_resolve
        MockResolver.return_value = inst
        results = await dns_monitor.run(_dns_cfg(["8.8.8.8", "1.1.1.1"]))

    targets = {r.target for r in results}
    assert "8.8.8.8" in targets
    assert "1.1.1.1" in targets


async def test_dns_servers_run_concurrently():
    """Two DNS servers each taking 0.1 s must finish in ~0.1 s total, not ~0.2 s."""

    async def slow_resolve(*_a, **_kw):
        await asyncio.sleep(0.1)
        return MagicMock()

    with patch("dns.asyncresolver.Resolver") as MockResolver:
        inst = MagicMock()
        inst.resolve = slow_resolve
        MockResolver.return_value = inst

        start = time.perf_counter()
        await dns_monitor.run(_dns_cfg(["8.8.8.8", "1.1.1.1"]))
        elapsed = time.perf_counter() - start

    assert elapsed < 0.18, (
        f"DNS servers appear to run sequentially (took {elapsed:.3f}s — expected ~0.1s)"
    )
