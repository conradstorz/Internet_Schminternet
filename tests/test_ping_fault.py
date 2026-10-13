"""Tests for the ping monitor's LAN-gateway leg and fault-location messages.

Gateway detection is stubbed via ``monitors.gateway.resolve`` and ``_ping_sync``
is stubbed per target, so nothing here shells out.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import monitors.ping as ping_monitor
from monitors.ping import _fault_location

GW = "192.168.86.1"
BEYOND = "LAN gateway 192.168.86.1 ok (0% loss) — fault is beyond your router (modem/ISP)"


class TestFaultLocation:
    def test_gateway_ok_internet_bad(self):
        assert _fault_location((GW, "ok", 0.0), ["ok", "degraded"], 40.0) == BEYOND

    def test_gateway_bad(self):
        assert _fault_location((GW, "down", 60.0), ["down"], 60.0) == (
            "LAN gateway 192.168.86.1 60% loss — fault is on the local network or router"
        )

    def test_gateway_degraded_counts_as_bad(self):
        msg = _fault_location((GW, "degraded", 12.5), ["degraded"], 20.0)
        assert msg == "LAN gateway 192.168.86.1 12.5% loss — fault is on the local network or router"

    def test_no_gateway_leg_short_loss_message(self):
        assert _fault_location(None, ["degraded"], 40.0) == "40% loss"
        assert _fault_location(None, ["degraded"], 12.5) == "12.5% loss"

    def test_one_line(self):
        for msg in (
            _fault_location((GW, "ok", 0.0), ["down"], 100.0),
            _fault_location((GW, "down", 100.0), ["down"], 100.0),
            _fault_location(None, ["down"], 100.0),
        ):
            assert "\n" not in msg


def _cfg(targets=("1.1.1.1", "8.8.8.8")) -> dict:
    return {"monitors": {"ping": {"targets": list(targets), "count": 1, "thresholds": {}}}}


def _fake_ping(table: dict):
    def _ping(target: str, count: int):
        return table[target]
    return _ping


async def test_gateway_rows_and_beyond_router_message():
    table = {GW: (0.4, 0.0), "1.1.1.1": (25.0, 40.0), "8.8.8.8": (20.0, 0.0)}
    with patch("monitors.gateway.resolve", AsyncMock(return_value=GW)), \
         patch("monitors.ping._ping_sync", side_effect=_fake_ping(table)):
        results = await ping_monitor.run(_cfg())

    gw_rows = [r for r in results if r.target == f"gateway {GW}"]
    assert {r.metric for r in gw_rows} == {"latency_ms", "packet_loss_pct"}
    assert all(r.status == "ok" and r.message == "" for r in gw_rows)

    bad = [r for r in results if r.target == "1.1.1.1"]
    assert bad and all(r.status == "degraded" for r in bad)
    assert all(r.message == BEYOND for r in bad)

    good = [r for r in results if r.target == "8.8.8.8"]
    assert all(r.status == "ok" and r.message == "" for r in good)


async def test_dead_gateway_drives_ping_down_and_blames_lan():
    table = {GW: (None, 100.0), "1.1.1.1": (None, 100.0)}
    with patch("monitors.gateway.resolve", AsyncMock(return_value=GW)), \
         patch("monitors.ping._ping_sync", side_effect=_fake_ping(table)):
        results = await ping_monitor.run(_cfg(("1.1.1.1",)))

    gw_rows = [r for r in results if r.target.startswith("gateway")]
    assert gw_rows and all(r.status == "down" for r in gw_rows)
    internet = [r for r in results if r.target == "1.1.1.1"]
    assert all(
        r.message == "LAN gateway 192.168.86.1 100% loss — fault is on the local network or router"
        for r in internet
    )


async def test_no_gateway_leg_still_gets_loss_message():
    table = {"1.1.1.1": (25.0, 40.0), "8.8.8.8": (20.0, 0.0)}
    with patch("monitors.gateway.resolve", AsyncMock(return_value=None)), \
         patch("monitors.ping._ping_sync", side_effect=_fake_ping(table)):
        results = await ping_monitor.run(_cfg())

    assert not any(r.target.startswith("gateway") for r in results)
    assert all(r.message == "40% loss" for r in results if r.target == "1.1.1.1")
    assert all(r.message == "" for r in results if r.target == "8.8.8.8")


async def test_gateway_disabled_in_config_never_detects():
    cfg = _cfg()
    cfg["monitors"]["ping"]["gateway"] = None
    with patch("monitors.gateway._walk_sync", side_effect=AssertionError), \
         patch("monitors.ping._ping_sync", return_value=(10.0, 0.0)):
        results = await ping_monitor.run(cfg)
    assert {r.target for r in results} == {"1.1.1.1", "8.8.8.8"}
